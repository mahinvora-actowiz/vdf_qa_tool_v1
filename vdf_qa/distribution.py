"""Disk-backed occurrence statistics for the rank-based VDF flags.

Counts live in MongoDB (collections dist_str and dist_num, keyed by period), not in
Python dicts. A 2M-row file would otherwise need several GB of RAM for the
per-value counters. Records are aggregated per batch before each upsert, so
the write volume stays manageable.

What is kept in memory is small: top-5 key sets, rare thresholds, length
histograms (bounded by the longest value), and numeric percentile thresholds.
"""
from __future__ import annotations

from pymongo import ASCENDING, DESCENDING, UpdateOne
from pymongo.database import Database
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from .rules import TAIL, TOP_N, _is_num, _weighted_percentiles, distribution_entries, extract, str_key

def _rare_from_histogram(hist: Counter) -> Optional[int]:
    """Same rule as rules._rare_threshold, computed from {occurrence_count: number_of_values}.

    Largest occurrence count c such that values occurring <= c times hold at most
    TAIL of all occurrences. None when no group fits.
    """
    total = sum(c * n for c, n in hist.items())
    if total == 0:
        return None
    threshold = None
    cum = 0
    for c in sorted(hist):
        occurrences = c * hist[c]
        if cum + occurrences > TAIL * total:
            break
        cum += occurrences
        threshold = c
    return threshold


class Distribution:
    def __init__(self, db: Database, period: str):
        self.db = db
        self.strings = db["dist_str"]
        self.numbers = db["dist_num"]
        self.period = period
        self.lengths: dict[str, Counter] = defaultdict(Counter)
        self.numeric_ids: set[str] = set()
        self._top_str: dict[str, set] = {}
        self._top_num: dict[str, set] = {}
        self._rare: dict[str, Optional[int]] = {}
        self._len: dict[str, tuple] = {}
        self._num: dict[str, tuple] = {}
        self._final = False

    # ------------------------------------------------------------ build
    @classmethod
    def build(cls, conn: Database, period: str, rows: Iterable[dict],
              batch: int = 5000) -> "Distribution":
        conn["dist_str"].create_index([("period", ASCENDING), ("dist_id", ASCENDING), ("h", ASCENDING)], unique=True)
        conn["dist_num"].create_index([("period", ASCENDING), ("dist_id", ASCENDING), ("v", ASCENDING)], unique=True)
        conn["dist_str"].delete_many({"period": period})
        conn["dist_num"].delete_many({"period": period})
        dist = cls(conn, period)
        pend_str: dict[tuple[str, int], int] = defaultdict(int)
        pend_num: dict[tuple[str, float], int] = defaultdict(int)
        n = 0
        for row in rows:
            for dist_id, value in distribution_entries(extract(row)):
                if value is None or isinstance(value, bool):
                    continue
                if _is_num(value):
                    dist.numeric_ids.add(dist_id)
                    pend_num[(dist_id, float(value))] += 1
                else:
                    s = str(value)
                    dist.lengths[dist_id][len(s)] += 1
                    pend_str[(dist_id, str_key(s))] += 1
            n += 1
            if n % batch == 0:
                dist._flush(pend_str, pend_num)
                pend_str.clear()
                pend_num.clear()
        dist._flush(pend_str, pend_num)
        dist._finalize()
        return dist

    def _flush(self, pend_str: dict, pend_num: dict) -> None:
        if pend_str:
            self.strings.bulk_write([UpdateOne({"period": self.period, "dist_id": d, "h": h}, {"$inc": {"cnt": c}}, upsert=True) for (d, h), c in pend_str.items()], ordered=False)
        if pend_num:
            self.numbers.bulk_write([UpdateOne({"period": self.period, "dist_id": d, "v": v}, {"$inc": {"cnt": c}}, upsert=True) for (d, v), c in pend_num.items()], ordered=False)

    def _dist_ids(self) -> set[str]:
        ids = set(self.strings.distinct("dist_id", {"period": self.period}))
        ids |= set(self.numbers.distinct("dist_id", {"period": self.period}))
        return ids

    def _finalize(self) -> None:
        for dist_id in self._dist_ids():
            # Top 5 most common values. Values seen only once are excluded, so unique
            # fields (names, SKUs) do not produce a "common value" for every row.
            self._top_str[dist_id] = {r["h"] for r in self.strings.find({"period": self.period, "dist_id": dist_id, "cnt": {"$gte": 2}}, {"h": 1}).sort([("cnt", DESCENDING), ("h", ASCENDING)]).limit(TOP_N)}
            self._top_num[dist_id] = {r["v"] for r in self.numbers.find({"period": self.period, "dist_id": dist_id, "cnt": {"$gte": 2}}, {"v": 1}).sort([("cnt", DESCENDING), ("v", ASCENDING)]).limit(TOP_N)}

            # Rare threshold: histogram of occurrence counts over string and numeric values.
            hist: Counter = Counter()
            for table in (self.strings, self.numbers):
                for r in table.aggregate([{"$match": {"period": self.period, "dist_id": dist_id}}, {"$group": {"_id": "$cnt", "n": {"$sum": 1}}}]):
                    hist[r["_id"]] += r["n"]
            self._rare[dist_id] = _rare_from_histogram(hist)

            self._len[dist_id] = _weighted_percentiles(self.lengths.get(dist_id, Counter()))
            if dist_id in self.numeric_ids:
                self._num[dist_id] = self._numeric_thresholds(dist_id)
        self._final = True

    def _numeric_thresholds(self, dist_id: str) -> tuple[Optional[float], Optional[float]]:
        totals = list(self.numbers.aggregate([{"$match": {"period": self.period, "dist_id": dist_id}}, {"$group": {"_id": None, "total": {"$sum": "$cnt"}}}]))
        total = totals[0]["total"] if totals else 0
        if total == 0:
            return None, None
        target = TAIL * total
        low = high = None
        cum = 0
        for r in self.numbers.find({"period": self.period, "dist_id": dist_id}).sort("v", ASCENDING):
            v, c = r["v"], r["cnt"]
            cum += c
            if cum >= target:
                low = v
                break
        cum = 0
        for r in self.numbers.find({"period": self.period, "dist_id": dist_id}).sort("v", DESCENDING):
            v, c = r["v"], r["cnt"]
            cum += c
            if cum >= target:
                high = v
                break
        return low, high

    def drop(self) -> None:
        """Free the counters of this period once the run is done."""
        self.strings.delete_many({"period": self.period})
        self.numbers.delete_many({"period": self.period})

    # ------------------------------------------------------------ queries used by the predicates
    def _check(self) -> None:
        if not self._final:
            raise RuntimeError("Distribution.build() must complete before evaluation")

    def _count(self, dist_id: str, value: Any) -> int:
        if _is_num(value):
            row = self.numbers.find_one({"period": self.period, "dist_id": dist_id, "v": float(value)}, {"cnt": 1})
        else:
            row = self.strings.find_one({"period": self.period, "dist_id": dist_id, "h": str_key(str(value))}, {"cnt": 1})
        return row["cnt"] if row else 0

    def top5(self, dist_id: str, value: Any) -> bool:
        self._check()
        if _is_num(value):
            return float(value) in self._top_num.get(dist_id, set())
        return str_key(str(value)) in self._top_str.get(dist_id, set())

    def rare(self, dist_id: str, value: Any) -> bool:
        self._check()
        thr = self._rare.get(dist_id)
        if thr is None:
            return False
        c = self._count(dist_id, value)
        return 0 < c <= thr

    def short(self, dist_id: str, value: Any) -> bool:
        self._check()
        low, _ = self._len.get(dist_id, (None, None))
        return low is not None and len(str(value)) <= low

    def long(self, dist_id: str, value: Any) -> bool:
        self._check()
        _, high = self._len.get(dist_id, (None, None))
        return high is not None and len(str(value)) >= high

    def lowest(self, dist_id: str, value: Any) -> bool:
        self._check()
        low, _ = self._num.get(dist_id, (None, None))
        return low is not None and value <= low

    def highest(self, dist_id: str, value: Any) -> bool:
        self._check()
        _, high = self._num.get(dist_id, (None, None))
        return high is not None and value >= high
