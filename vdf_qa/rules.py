"""VDF Review Flags engine.

Implements the flag catalogue from "VDF Review Flags" (last updated 2026-10-01).

Design notes
------------
* A flag is evaluated only when its field changed between the previous and the
  current row. Unchanged values are not review cues, which keeps a monthly run
  close to linear in the number of changed rows.
* Priorities: High, Medium (as in the PDF) and Review (Gained/Lost, the shared
  base filters, which the PDF does not rank).
* Rank-based flags (top 5 common, rare 5%, shortest/longest 5%, lowest/highest
  5%) are computed once from the current file by Distribution.build().
* Every hit has a magnitude. The dashboard ranks hits per flag by magnitude,
  shows the top 5, and lists the rest underneath.

Assumptions that still need confirmation against the SOP are listed in the README.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qsl, urlsplit

HIGH = "High"
MEDIUM = "Medium"
REVIEW = "Review"

TIME_COMPONENTS = ("time_to_stock", "time_to_ship", "time_to_arrive")
SPECIFIC_MARK = "/"
TOP_N = 5
TAIL = 0.05


# ---------------------------------------------------------------- helpers

def _canon(x: Any) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _nonempty(x: Any) -> bool:
    return isinstance(x, str) and x.strip() != ""


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def str_key(s: str) -> int:
    """Signed 63-bit hash of a string, stored as a MongoDB numeric key."""
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big") & ((1 << 63) - 1)


def bounded_levenshtein(a: str, b: str, k: int) -> int:
    """Exact Levenshtein distance if it is <= k, otherwise k + 1.

    Banded dynamic programming: O(k * min(len(a), len(b))) instead of the full
    O(len(a) * len(b)) table, so long descriptions stay cheap.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > k:
        return k + 1
    if len(a) < len(b):
        a, b = b, a
    la, lb = len(a), len(b)
    prev = [j if j <= k else k + 1 for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [k + 1] * (lb + 1)
        cur[0] = i if i <= k else k + 1
        row_min = cur[0]
        ai = a[i - 1]
        for j in range(max(1, i - k), min(lb, i + k) + 1):
            cost = 0 if ai == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if v > k + 1:
                v = k + 1
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > k:
            return k + 1
        prev = cur
    return min(prev[lb], k + 1)


def _rare_threshold(counter: Counter) -> Optional[int]:
    """Largest occurrence count c such that values occurring <= c times hold at most TAIL of all occurrences.

    Reading of "least common 5% of values": the rarest values that together make up
    the bottom 5% of occurrences. Returns None when no value group fits (for example
    when most values are unique), so rare flags do not fire on every singleton.
    """
    total = sum(counter.values())
    if total == 0:
        return None
    by_count = Counter(counter.values())  # occurrence count -> number of distinct values with it
    cum = 0
    threshold = None
    for c in sorted(by_count):
        occurrences = c * by_count[c]
        if cum + occurrences > TAIL * total:
            break
        cum += occurrences
        threshold = c
    return threshold


def _weighted_percentiles(counter: Counter) -> tuple[Optional[Any], Optional[Any]]:
    """(low, high) thresholds. low is the TAIL quantile from below, high the TAIL quantile from above."""
    total = sum(counter.values())
    if total == 0:
        return None, None
    items = sorted(counter.items())
    low = high = None
    cum = 0
    for value, c in items:
        cum += c
        if cum >= TAIL * total:
            low = value
            break
    cum = 0
    for value, c in reversed(items):
        cum += c
        if cum >= TAIL * total:
            high = value
            break
    return low, high


# ---------------------------------------------------------------- features

def _text(x: Any) -> dict:
    v = x if _nonempty(x) else None
    return {"v": v, "empty": v is None}


def _num(x: Any) -> dict:
    v = x if _is_num(x) else None
    return {"v": v, "empty": v is None}


def _bool(x: Any) -> dict:
    v = x if isinstance(x, bool) else None
    return {"v": v, "empty": v is None}


def _url(x: Any) -> dict:
    v = x if _nonempty(x) else None
    if v is None:
        return {"v": None, "empty": True, "domain": None, "path": None, "query": None}
    parts = urlsplit(v)
    return {
        "v": v,
        "empty": False,
        "domain": parts.netloc.lower(),
        "path": parts.path,
        "query": frozenset(parse_qsl(parts.query, keep_blank_values=True)),
    }


def _category(x: Any) -> dict:
    if isinstance(x, list) and x and all(_nonempty(s) for s in x):
        return {"v": " > ".join(x), "empty": False, "steps": len(x)}
    return {"v": None, "empty": True, "steps": 0}


def _price(x: Any) -> dict:
    tiers = []
    for t in x if isinstance(x, list) else []:
        if not isinstance(t, dict):
            continue
        mq = t.get("min_qty")
        price = t.get("price") if _is_num(t.get("price")) else None
        ps = t.get("price_string") if _nonempty(t.get("price_string")) else None
        tiers.append({"min_qty": mq if _is_num(mq) else None, "price": price, "price_string": ps})
    return {
        "v": _canon(tiers) if tiers else None,
        "empty": not tiers,
        "tiers": tiers,
        "nonnum": sum(1 for t in tiers if t["price"] is None),
        "base": tiers[0]["price"] if tiers else None,
        "min_qtys": [t["min_qty"] for t in tiers],
    }


def _lead_time(x: Any) -> dict:
    tiers = []
    raws: list[str] = []
    for t in x if isinstance(x, list) else []:
        if not isinstance(t, dict):
            continue
        comps = {}
        for name in TIME_COMPONENTS:
            c = t.get(name)
            if isinstance(c, dict):
                raw = c.get("raw_value") if _nonempty(c.get("raw_value")) else None
                comps[name] = {"raw": raw, "parsed": c.get("parsed_value")}
                if raw is not None:
                    raws.append(raw)
        mq = t.get("min_qty")
        tiers.append({"min_qty": mq if _is_num(mq) else None, "comps": comps})
    return {"v": _canon(tiers) if tiers else None, "empty": not tiers, "tiers": tiers, "raws": raws}


def _assets(x: Any) -> dict:
    items = [a for a in x if isinstance(a, dict)] if isinstance(x, list) else []
    return {"v": _canon(items) if items else None, "empty": not items, "list": items}


def _main_image(x: Any) -> dict:
    if not isinstance(x, dict) or not x:
        return {"v": None, "empty": True, "name": None, "source": None, "type": None,
                "sha256": None, "media_type": None, "domain": None}
    src = x.get("source") if _nonempty(x.get("source")) else None
    return {
        "v": _canon(x),
        "empty": False,
        "name": x.get("name") if _nonempty(x.get("name")) else None,
        "source": src,
        "type": x.get("type") if _nonempty(x.get("type")) else None,
        "sha256": x.get("sha256") if _nonempty(x.get("sha256")) else None,
        "media_type": x.get("media_type") if _nonempty(x.get("media_type")) else None,
        "domain": urlsplit(src).netloc.lower() if src else None,
    }


def _attributes(x: Any) -> dict:
    items = []
    for a in (x if isinstance(x, list) else []):
        if isinstance(a, dict):
            items.append({"name": a.get("name"), "value": a.get("value"), "group": a.get("group")})
    return {"v": _canon(items) if items else None, "empty": not items, "list": items}


def _features(x: Any) -> dict:
    items = []
    for f_ in x if isinstance(x, list) else []:
        if isinstance(f_, dict):
            items.append({"text": f_.get("text"), "group": f_.get("group")})
    return {"v": _canon(items) if items else None, "empty": not items, "list": items}


_EXTRACTORS: dict[str, Callable[[Any], dict]] = {
    "name": _text, "description": _text, "description_html": _text,
    "manufacturer": _text, "mpn": _text, "pack_label": _text, "sku_unit": _text,
    "pdp_url": _url, "category": _category,
    "sku_quantity": _num, "quantity_increment": _num, "in_stock_quantity": _num,
    "in_stock": _bool, "available_to_checkout": _bool,
    "price": _price, "lead_time": _lead_time,
    "assets": _assets, "main_image": _main_image,
    "attributes": _attributes, "features": _features,
}

DATA_FIELDS = tuple(_EXTRACTORS)


def extract(row: dict) -> dict[str, dict]:
    return {name: fn(row.get(name)) for name, fn in _EXTRACTORS.items()}


# ---------------------------------------------------------------- distributions

def distribution_entries(feats: dict[str, dict]) -> Iterable[tuple[str, Any]]:
    """(dist_id, value) pairs contributed by one current row."""
    for name, f in feats.items():
        if f.get("v") is not None:
            yield f"{name}.v", f["v"]
    if feats["pdp_url"]["domain"]:
        yield "pdp_url.domain", feats["pdp_url"]["domain"]
    if feats["price"]["base"] is not None:
        yield "price.base", feats["price"]["base"]
    for raw in feats["lead_time"]["raws"]:
        yield "lead_time.raw", raw
    main = feats["main_image"]
    for k in ("domain", "media_type"):
        if main[k]:
            yield f"main_image.{k}", main[k]
    for a in feats["assets"]["list"]:
        if a.get("media_type"):
            yield "assets.media_type", a["media_type"]
        if isinstance(a.get("group"), str):
            yield "assets.group", a["group"]
    for a in feats["attributes"]["list"]:
        if a.get("name") is not None:
            yield "attributes.name", str(a["name"])
        if a.get("value") is not None:
            yield "attributes.value", str(a["value"])
        if isinstance(a.get("group"), str):
            yield "attributes.group", a["group"]
    for f_ in feats["features"]["list"]:
        if f_.get("text") is not None:
            yield "features.text", str(f_["text"])
        if isinstance(f_.get("group"), str):
            yield "features.group", f_["group"]


# ---------------------------------------------------------------- predicates
# Each predicate returns None (no hit) or a float magnitude used for ranking.

@dataclass
class Ctx:
    old: dict[str, dict]
    new: dict[str, dict]
    dist: Any  # vdf_qa.distribution.Distribution


def _p_gained(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    return 1.0 if ctx.old[field]["empty"] and not ctx.new[field]["empty"] else None


def _p_lost(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    return 1.0 if not ctx.old[field]["empty"] and ctx.new[field]["empty"] else None


def _p_ed(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    key = p.get("key", "v")
    a, b = ctx.old[field].get(key), ctx.new[field].get(key)
    if not (isinstance(a, str) and isinstance(b, str)) or a == b:
        return None
    need = max(1, math.ceil(p["t"] * len(b)))
    d = bounded_levenshtein(a, b, need)
    return d / len(b) if d >= need else None


def _p_length(sign: int) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        key = p.get("key", "v")
        a, b = ctx.old[field].get(key), ctx.new[field].get(key)
        if not (isinstance(a, str) and isinstance(b, str)):
            return None
        if sign < 0 and len(b) < len(a):
            return (len(a) - len(b)) / len(a)
        if sign > 0 and len(b) > len(a):
            return (len(b) - len(a)) / len(a)
        return None
    return fn


def _p_steps(sign: int) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        a, b = ctx.old[field]["steps"], ctx.new[field]["steps"]
        if a == 0 or b == 0:
            return None
        if sign < 0 and b < a or sign > 0 and b > a:
            return float(abs(b - a))
        return None
    return fn


def _p_changed(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    key = p["key"]
    a, b = ctx.old[field].get(key), ctx.new[field].get(key)
    return 1.0 if a is not None and b is not None and a != b else None


def _p_rank(kind: str) -> Callable:
    """Top5 / rare / short / long / lowest / highest on the field's own value (or p['key'])."""
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        key = p.get("key", "v")
        value = ctx.new[field].get(key)
        if value is None:
            return None
        dist_id = p.get("dist", f"{field}.{key}")
        test = {
            "top5": ctx.dist.top5, "rare": ctx.dist.rare, "short": ctx.dist.short,
            "long": ctx.dist.long, "lowest": ctx.dist.lowest, "highest": ctx.dist.highest,
        }[kind]
        return 1.0 if test(dist_id, value) else None
    return fn


def _p_item_rank(kind: str) -> Callable:
    """Any list item satisfies the rank rule, e.g. any attribute value is among the shortest 5%."""
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        test = {"rare": ctx.dist.rare, "short": ctx.dist.short, "long": ctx.dist.long}[kind]
        hits = 0
        for item in ctx.new[field]["list"]:
            value = item.get(p["item"])
            if value is not None and test(p["dist"], str(value)):
                hits += 1
        return float(hits) if hits else None
    return fn


def _p_count_delta(sign: int, lo: int, hi: Optional[int]) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        delta = (len(ctx.new[field]["list"]) - len(ctx.old[field]["list"])) * sign
        if delta >= lo and (hi is None or delta <= hi):
            return float(delta)
        return None
    return fn


def _names_by(items: list[dict], key: str) -> set[str]:
    return {str(i.get(key)) for i in items if i.get(key) is not None}


def _p_set_delta(sign: int, lo: int, hi: Optional[int], key: str) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        old = _names_by(ctx.old[field]["list"], key)
        new = _names_by(ctx.new[field]["list"], key)
        n = len(old - new) if sign < 0 else len(new - old)
        if n >= lo and (hi is None or n <= hi):
            return float(n)
        return None
    return fn


def _p_group_delta(sign: int) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        old = _names_by(ctx.old[field]["list"], "group")
        new = _names_by(ctx.new[field]["list"], "group")
        n = len(old - new) if sign < 0 else len(new - old)
        return float(n) if n else None
    return fn


def _matched_by_source(ctx: Ctx, field: str) -> list[tuple[dict, dict]]:
    old = {a.get("source"): a for a in ctx.old[field]["list"] if a.get("source")}
    return [(old[a["source"]], a) for a in ctx.new[field]["list"]
            if a.get("source") and a["source"] in old]


def _specific(t: Any) -> bool:
    return isinstance(t, str) and SPECIFIC_MARK in t


def _p_asset_spec(direction: str) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        n = 0
        for o, c in _matched_by_source(ctx, field):
            if direction == "less" and _specific(o.get("type")) and not _specific(c.get("type")):
                n += 1
            elif direction == "more" and not _specific(o.get("type")) and _specific(c.get("type")):
                n += 1
        return float(n) if n else None
    return fn


def _p_asset_type_changed(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    n = 0
    for o, c in _matched_by_source(ctx, field):
        if o.get("type") and c.get("type") and o["type"] != c["type"] \
                and _specific(o["type"]) == _specific(c["type"]):
            n += 1
    return float(n) if n else None


def _p_asset_media_changed(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    n = sum(1 for o, c in _matched_by_source(ctx, field)
            if o.get("media_type") and c.get("media_type") and o["media_type"] != c["media_type"])
    return float(n) if n else None


def _p_type_set_delta(sign: int) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        old = _names_by(ctx.old[field]["list"], "type")
        new = _names_by(ctx.new[field]["list"], "type")
        n = len(new - old) if sign > 0 else len(old - new)
        return float(n) if n else None
    return fn


def _p_has_type(t: str) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        n = sum(1 for a in ctx.new[field]["list"] if a.get("type") == t)
        return float(n) if n else None
    return fn


def _p_missing_sha(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    n = sum(1 for a in ctx.new[field]["list"] if not a.get("sha256"))
    return float(n) if n else None


def _p_main_not_product(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    new = ctx.new[field]
    return 1.0 if not new["empty"] and new["type"] != "image/product" else None


def _p_main_missing_sha(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    new = ctx.new[field]
    return 1.0 if not new["empty"] and not new["sha256"] else None


def _p_changed_count(lo: int, hi: Optional[int]) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        old = {str(a.get("name")): a.get("value") for a in ctx.old[field]["list"]}
        n = sum(1 for a in ctx.new[field]["list"]
                if str(a.get("name")) in old and old[str(a.get("name"))] != a.get("value"))
        if n >= lo and (hi is None or n <= hi):
            return float(n)
        return None
    return fn


def _p_attr_value_ed(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    old = {str(a.get("name")): a.get("value") for a in ctx.old[field]["list"]}
    worst: Optional[float] = None
    for a in ctx.new[field]["list"]:
        name, nv = str(a.get("name")), a.get("value")
        ov = old.get(name)
        if not (isinstance(ov, str) and isinstance(nv, str)) or ov == nv:
            continue
        need = max(1, math.ceil(p["t"] * len(nv)))
        d = bounded_levenshtein(ov, nv, need)
        if d >= need:
            ratio = d / len(nv)
            worst = ratio if worst is None else max(worst, ratio)
    return worst


def _p_qty_pct(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    a, b = ctx.old[field]["v"], ctx.new[field]["v"]
    if a is None or b is None or a == b:
        return None
    if a == 0:
        return 1.0  # from zero to any value is a change of at least 100%
    change = abs(b - a) / abs(a)
    return change if change >= p["threshold"] else None


def _p_qty_both_gt1(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    sq, inc = ctx.new["sku_quantity"]["v"], ctx.new["quantity_increment"]["v"]
    return 1.0 if sq is not None and inc is not None and sq > 1 and inc > 1 else None


def _pairs_by_index(ctx: Ctx, field: str) -> list[tuple[dict, dict]]:
    return list(zip(ctx.old[field]["tiers"], ctx.new[field]["tiers"]))


def _p_break_count(sign: int) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        if ctx.old[field]["empty"] or ctx.new[field]["empty"]:
            return None
        diff = len(ctx.new[field]["tiers"]) - len(ctx.old[field]["tiers"])
        if sign < 0 and diff < 0 or sign > 0 and diff > 0:
            return float(abs(diff))
        return None
    return fn


def _p_nonnum(op: str, n: int = 0) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        if ctx.old[field]["empty"] or ctx.new[field]["empty"]:
            return None
        new_n, old_n = ctx.new[field]["nonnum"], ctx.old[field]["nonnum"]
        ok = {"more": new_n > old_n, "less": new_n < old_n, "ge": new_n >= n, "eq": new_n == n}[op]
        return float(new_n) if ok else None
    return fn


def _p_dup_min_qty(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    mqs = [m for m in ctx.new[field]["min_qtys"] if m is not None]
    dups = sum(c - 1 for c in Counter(mqs).values() if c > 1)
    return float(dups) if dups else None


def _p_avg_price_pct(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    pairs = [(o["price"], n["price"]) for o, n in _pairs_by_index(ctx, field)
             if o["price"] is not None and n["price"] is not None and o["price"] > 0]
    if not pairs:
        return None
    avg = sum(abs(n - o) / o for o, n in pairs) / len(pairs)
    return avg if avg >= p["threshold"] else None


def _p_avg_min_qty(kind: str, threshold: float) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        pairs = [(o["min_qty"], n["min_qty"]) for o, n in _pairs_by_index(ctx, field)
                 if o.get("min_qty") is not None and n.get("min_qty") is not None]
        if not pairs:
            return None
        if kind == "abs":
            avg = sum(abs(n - o) for o, n in pairs) / len(pairs)
        else:
            pct = [abs(n - o) / o for o, n in pairs if o > 0]
            if not pct:
                return None
            avg = sum(pct) / len(pct)
        return avg if avg >= threshold else None
    return fn


def _p_base_rank(kind: str) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        f = ctx.new[field]
        if f["empty"] or f["nonnum"] > 0 or f["base"] is None:
            return None
        test = ctx.dist.lowest if kind == "lowest" else ctx.dist.highest
        return 1.0 if test("price.base", f["base"]) else None
    return fn


def _p_lt_multi_comp(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    n = sum(1 for t in ctx.new[field]["tiers"] if len(t["comps"]) > 1)
    return float(n) if n else None


def _p_lt_empty_breaks(k: int) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        n = sum(1 for t in ctx.new[field]["tiers"]
                if any(c["raw"] is None for c in t["comps"].values()))
        return float(n) if n >= k else None
    return fn


def _p_lt_raw_changed(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    n = 0
    for o, nw in _pairs_by_index(ctx, field):
        for name in TIME_COMPONENTS:
            oc, nc = o["comps"].get(name), nw["comps"].get(name)
            if oc is not None and nc is not None and oc["raw"] != nc["raw"]:
                n += 1
    return float(n) if n else None


def _p_lt_more_comp(ctx: Ctx, field: str, p: dict) -> Optional[float]:
    n = sum(1 for o, nw in _pairs_by_index(ctx, field) if len(nw["comps"]) > len(o["comps"]))
    return float(n) if n else None


def _p_lt_parsed(want: bool) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        if ctx.new[field]["empty"]:
            return None
        has = any(c["parsed"] is not None for t in ctx.new[field]["tiers"] for c in t["comps"].values())
        return 1.0 if has == want else None
    return fn


def _p_raw_rank(kind: str) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        test = ctx.dist.short if kind == "short" else ctx.dist.long
        hits = sum(1 for raw in ctx.new[field]["raws"] if test("lead_time.raw", raw))
        return float(hits) if hits else None
    return fn


def _p_domain_rank(dist_id: str) -> Callable:
    def fn(ctx: Ctx, field: str, p: dict) -> Optional[float]:
        d = ctx.new[field]["domain"]
        return 1.0 if d and ctx.dist.rare(dist_id, d) else None
    return fn


PREDICATES: dict[str, Callable[[Ctx, str, dict], Optional[float]]] = {
    "gained": _p_gained,
    "lost": _p_lost,
    "ed": _p_ed,
    "shorter": _p_length(-1),
    "longer": _p_length(+1),
    "steps_less": _p_steps(-1),
    "steps_more": _p_steps(+1),
    "changed": _p_changed,
    "top5": _p_rank("top5"),
    "rare5": _p_rank("rare"),
    "shortest5": _p_rank("short"),
    "longest5": _p_rank("long"),
    "lowest5": _p_rank("lowest"),
    "highest5": _p_rank("highest"),
    "qty_pct": _p_qty_pct,
    "qty_both_gt1": _p_qty_both_gt1,
    "price_breaks_more": _p_break_count(+1),
    "price_breaks_less": _p_break_count(-1),
    "nonnum_more": _p_nonnum("more"),
    "nonnum_less": _p_nonnum("less"),
    "nonnum_ge2": _p_nonnum("ge", 2),
    "nonnum_eq1": _p_nonnum("eq", 1),
    "dup_min_qty": _p_dup_min_qty,
    "avg_price_pct": _p_avg_price_pct,
    "avg_min_qty_abs": _p_avg_min_qty("abs", 1.0),
    "avg_min_qty_pct": _p_avg_min_qty("pct", 0.5),
    "base_lowest5": _p_base_rank("lowest"),
    "base_highest5": _p_base_rank("highest"),
    "lt_count_less": _p_break_count(-1),
    "lt_count_more": _p_break_count(+1),
    "lt_multi_comp": _p_lt_multi_comp,
    "lt_empty_breaks_1": _p_lt_empty_breaks(1),
    "lt_empty_breaks_2": _p_lt_empty_breaks(2),
    "lt_raw_changed": _p_lt_raw_changed,
    "lt_more_comp": _p_lt_more_comp,
    "lt_parsed": _p_lt_parsed(True),
    "lt_no_parsed": _p_lt_parsed(False),
    "raw_shortest5": _p_raw_rank("short"),
    "raw_longest5": _p_raw_rank("long"),
    "count_1_2_less": _p_count_delta(-1, 1, 2),
    "count_3_less": _p_count_delta(-1, 3, None),
    "count_1_2_more": _p_count_delta(+1, 1, 2),
    "count_3_more": _p_count_delta(+1, 3, None),
    "asset_spec_less": _p_asset_spec("less"),
    "asset_spec_more": _p_asset_spec("more"),
    "asset_type_changed": _p_asset_type_changed,
    "asset_media_changed": _p_asset_media_changed,
    "asset_type_added": _p_type_set_delta(+1),
    "asset_type_removed": _p_type_set_delta(-1),
    "asset_has_other": _p_has_type("other"),
    "asset_has_document": _p_has_type("document"),
    "asset_has_image": _p_has_type("image"),
    "asset_missing_sha": _p_missing_sha,
    "item_rare": _p_item_rank("rare"),
    "item_short": _p_item_rank("short"),
    "item_long": _p_item_rank("long"),
    "main_not_product": _p_main_not_product,
    "main_missing_sha": _p_main_missing_sha,
    "domain_rare": _p_domain_rank("pdp_url.domain"),
    "main_domain_rare": _p_domain_rank("main_image.domain"),
    "set_delta_1_2_less": _p_set_delta(-1, 1, 2, "name"),
    "set_delta_3_less": _p_set_delta(-1, 3, None, "name"),
    "set_delta_1_2_more": _p_set_delta(+1, 1, 2, "name"),
    "set_delta_3_more": _p_set_delta(+1, 3, None, "name"),
    "feat_delta_1_2_less": _p_set_delta(-1, 1, 2, "text"),
    "feat_delta_3_less": _p_set_delta(-1, 3, None, "text"),
    "feat_delta_1_2_more": _p_set_delta(+1, 1, 2, "text"),
    "feat_delta_3_more": _p_set_delta(+1, 3, None, "text"),
    "changed_count_1_2": _p_changed_count(1, 2),
    "changed_count_3": _p_changed_count(3, None),
    "attr_value_ed": _p_attr_value_ed,
    "group_added": _p_group_delta(+1),
    "group_removed": _p_group_delta(-1),
}


# ---------------------------------------------------------------- catalogue

@dataclass(frozen=True)
class FlagDef:
    field: str
    code: str
    priority: str
    predicate: str
    params: tuple
    description: str

    def p(self) -> dict:
        return dict(self.params)


def _f(field: str, code: str, priority: str, predicate: str, description: str, **params: Any) -> FlagDef:
    return FlagDef(field, code, priority, predicate, tuple(sorted(params.items())), description)


def _text_flags(field: str, *, rare: bool = False) -> list[FlagDef]:
    """Common string-field pattern: ed50 (High), shorter/longer/ed10/top5/shortest5/longest5 (Medium)."""
    out = [
        _f(field, "ed50", HIGH, "ed", "Edit distance >= 50% of the new value length", t=0.5),
        _f(field, "shorter", MEDIUM, "shorter", "New value is shorter than the old value"),
        _f(field, "longer", MEDIUM, "longer", "New value is longer than the old value"),
        _f(field, "ed10", MEDIUM, "ed", "Edit distance >= 10% of the new value length", t=0.1),
        _f(field, "top5_common", MEDIUM, "top5", "New value is among the 5 most common values"),
    ]
    if rare:
        out.append(_f(field, "rare5", MEDIUM, "rare5", "New value is among the least common 5%"))
    out += [
        _f(field, "shortest5", MEDIUM, "shortest5", "New value is among the shortest 5% by length"),
        _f(field, "longest5", MEDIUM, "longest5", "New value is among the longest 5% by length"),
    ]
    return out


def _qty_flags(field: str, high: list[FlagDef] | None = None) -> list[FlagDef]:
    out = list(high or [])
    out += [
        _f(field, "chg50", MEDIUM, "qty_pct", "Quantity changed by >= 50%", threshold=0.5),
        _f(field, "chg100", MEDIUM, "qty_pct", "Quantity changed by >= 100%", threshold=1.0),
        _f(field, "top5_common", MEDIUM, "top5", "New quantity is among the 5 most common values"),
        _f(field, "lowest5", MEDIUM, "lowest5", "New quantity is in the lowest 5%"),
        _f(field, "highest5", MEDIUM, "highest5", "New quantity is in the highest 5%"),
    ]
    return out


def build_catalog() -> list[FlagDef]:
    cat: list[FlagDef] = []

    # Text fields
    cat += _text_flags("name")
    cat += _text_flags("description")
    cat += _text_flags("description_html")
    cat += _text_flags("manufacturer", rare=True)
    cat += _text_flags("mpn")
    cat += _text_flags("pack_label", rare=True)

    # Category
    cat += [
        _f("category", "more_steps", HIGH, "steps_more", "Category path has more steps"),
        _f("category", "less_steps", HIGH, "steps_less", "Category path has fewer steps"),
        _f("category", "ed50", HIGH, "ed", "Joined category path edit distance >= 50%", t=0.5),
        _f("category", "ed10", MEDIUM, "ed", "Joined category path edit distance >= 10%", t=0.1),
        _f("category", "top5_common", MEDIUM, "top5", "Category path is among the 5 most common"),
        _f("category", "rare5", MEDIUM, "rare5", "Category path is among the least common 5%"),
        _f("category", "shortest5", MEDIUM, "shortest5", "Category path is among the shortest 5%"),
        _f("category", "longest5", MEDIUM, "longest5", "Category path is among the longest 5%"),
    ]

    # PDP URL
    cat += [
        _f("pdp_url", "domain_chg", HIGH, "changed", "URL host changed", key="domain"),
        _f("pdp_url", "ed50", HIGH, "ed", "URL edit distance >= 50%", t=0.5),
        _f("pdp_url", "rare_domain", HIGH, "domain_rare", "URL domain is among the least common"),
        _f("pdp_url", "shorter", MEDIUM, "shorter", "URL is shorter than the old value"),
        _f("pdp_url", "longer", MEDIUM, "longer", "URL is longer than the old value"),
        _f("pdp_url", "ed10", MEDIUM, "ed", "URL edit distance >= 10%", t=0.1),
        _f("pdp_url", "path_chg", MEDIUM, "changed", "URL path changed", key="path"),
        _f("pdp_url", "query_chg", MEDIUM, "changed", "URL query parameters changed", key="query"),
        _f("pdp_url", "top5_common", MEDIUM, "top5", "URL is among the 5 most common"),
        _f("pdp_url", "shortest5", MEDIUM, "shortest5", "URL is among the shortest 5%"),
        _f("pdp_url", "longest5", MEDIUM, "longest5", "URL is among the longest 5%"),
    ]

    # Price
    cat += [
        _f("price", "less_breaks", HIGH, "price_breaks_less", "Fewer price breaks than the old row"),
        _f("price", "more_breaks_nonnum", HIGH, "nonnum_more", "More non-numeric price breaks"),
        _f("price", "chg50", HIGH, "avg_price_pct", "Average price change >= 50%", threshold=0.5),
        _f("price", "multiple_nonnum", HIGH, "nonnum_ge2", "Two or more breaks without a numeric price"),
        _f("price", "dup_min_qty", HIGH, "dup_min_qty", "Duplicate numeric min_qty values"),
        _f("price", "more_breaks", MEDIUM, "price_breaks_more", "More price breaks than the old row"),
        _f("price", "less_breaks_nonnum", MEDIUM, "nonnum_less", "Fewer non-numeric price breaks"),
        _f("price", "chg10", MEDIUM, "avg_price_pct", "Average price change >= 10%", threshold=0.1),
        _f("price", "qty_chg1", MEDIUM, "avg_min_qty_abs", "Average min_qty change >= 1"),
        _f("price", "qty_chg50", MEDIUM, "avg_min_qty_pct", "Average min_qty change >= 50%"),
        _f("price", "one_nonnum", MEDIUM, "nonnum_eq1", "Exactly one break without a numeric price"),
        _f("price", "top5_common", MEDIUM, "top5", "Price value is among the 5 most common"),
        _f("price", "shortest5", MEDIUM, "shortest5", "Price value is among the shortest 5%"),
        _f("price", "longest5", MEDIUM, "longest5", "Price value is among the longest 5%"),
        _f("price", "base_lowest5", MEDIUM, "base_lowest5", "Base price is in the lowest 5%"),
        _f("price", "base_highest5", MEDIUM, "base_highest5", "Base price is in the highest 5%"),
    ]

    # Lead time
    cat += [
        _f("lead_time", "less_lt", HIGH, "lt_count_less", "Fewer lead-time breaks than the old row"),
        _f("lead_time", "multiple_comp", HIGH, "lt_multi_comp", "A break has multiple lead-time components"),
        _f("lead_time", "empty_comp2", HIGH, "lt_empty_breaks_2", "Two or more breaks with a null raw_value"),
        _f("lead_time", "more_lt", MEDIUM, "lt_count_more", "More lead-time breaks than the old row"),
        _f("lead_time", "raw_chg", MEDIUM, "lt_raw_changed", "A lead-time raw_value changed"),
        _f("lead_time", "more_comp", MEDIUM, "lt_more_comp", "A break has more components than before"),
        _f("lead_time", "qty_chg1", MEDIUM, "avg_min_qty_abs", "Average min_qty change >= 1"),
        _f("lead_time", "qty_chg50", MEDIUM, "avg_min_qty_pct", "Average min_qty change >= 50%"),
        _f("lead_time", "empty_comp1", MEDIUM, "lt_empty_breaks_1", "A break has a null raw_value"),
        _f("lead_time", "no_parsed", MEDIUM, "lt_no_parsed", "No break has a parsed lead time"),
        _f("lead_time", "parsed1", MEDIUM, "lt_parsed", "A break has a parsed lead time"),
        _f("lead_time", "top5_common", MEDIUM, "top5", "Lead-time value is among the 5 most common"),
        _f("lead_time", "shortest5", MEDIUM, "shortest5", "Lead-time value is among the shortest 5%"),
        _f("lead_time", "longest5", MEDIUM, "longest5", "Lead-time value is among the longest 5%"),
        _f("lead_time", "raw_shortest5", MEDIUM, "raw_shortest5", "A raw lead-time value is among the shortest 5%"),
        _f("lead_time", "raw_longest5", MEDIUM, "raw_longest5", "A raw lead-time value is among the longest 5%"),
    ]

    # Assets
    cat += [
        _f("assets", "less1", HIGH, "count_1_2_less", "One or two fewer assets"),
        _f("assets", "less3", HIGH, "count_3_less", "Three or more fewer assets"),
        _f("assets", "less_specific", HIGH, "asset_spec_less", "Asset type became less specific"),
        _f("assets", "has_other", HIGH, "asset_has_other", "An asset has the generic type 'other'"),
        _f("assets", "has_document", HIGH, "asset_has_document", "An asset has the generic type 'document'"),
        _f("assets", "has_image", HIGH, "asset_has_image", "An asset has the generic type 'image'"),
        _f("assets", "not_downloaded", HIGH, "asset_missing_sha", "An asset has no SHA-256"),
        _f("assets", "more1", MEDIUM, "count_1_2_more", "One or two more assets"),
        _f("assets", "more3", MEDIUM, "count_3_more", "Three or more more assets"),
        _f("assets", "type_chg", MEDIUM, "asset_type_changed", "Source-matched asset type changed"),
        _f("assets", "more_specific", MEDIUM, "asset_spec_more", "Asset type became more specific"),
        _f("assets", "media_type_chg", MEDIUM, "asset_media_changed", "Source-matched media_type changed"),
        _f("assets", "type_add", MEDIUM, "asset_type_added", "An asset type appears that was not there before"),
        _f("assets", "type_rem", MEDIUM, "asset_type_removed", "An asset type was removed"),
        _f("assets", "top5_common", MEDIUM, "top5", "Assets value is among the 5 most common"),
        _f("assets", "shortest5", MEDIUM, "shortest5", "Assets value is among the shortest 5%"),
        _f("assets", "longest5", MEDIUM, "longest5", "Assets value is among the longest 5%"),
        _f("assets", "rare_media_type", MEDIUM, "item_rare", "An asset media_type is among the least common",
           item="media_type", dist="assets.media_type"),
        _f("assets", "rare_group", MEDIUM, "item_rare", "An asset group is among the least common",
           item="group", dist="assets.group"),
    ]

    # Main image
    cat += [
        _f("main_image", "not_product", HIGH, "main_not_product", "Main image type is not image/product"),
        _f("main_image", "domain_chg", HIGH, "changed", "Main image host changed", key="domain"),
        _f("main_image", "not_downloaded", HIGH, "main_missing_sha", "Main image has no SHA-256"),
        _f("main_image", "rare_domain", HIGH, "main_domain_rare", "Main image domain is among the least common"),
        _f("main_image", "type_chg", MEDIUM, "changed", "Main image type changed", key="type"),
        _f("main_image", "name_ed20", MEDIUM, "ed", "Main image name edit distance >= 20%", key="name", t=0.2),
        _f("main_image", "source_ed20", MEDIUM, "ed", "Main image source edit distance >= 20%",
           key="source", t=0.2),
        _f("main_image", "sha_chg", MEDIUM, "changed", "Main image SHA-256 changed", key="sha256"),
        _f("main_image", "top5_common", MEDIUM, "top5", "Main image value is among the 5 most common"),
        _f("main_image", "rare_media_type", MEDIUM, "rare5", "Main image media_type is among the least common",
           key="media_type", dist="main_image.media_type"),
    ]

    # Attributes
    cat += [
        _f("attributes", "rem1", HIGH, "set_delta_1_2_less", "One or two attributes removed"),
        _f("attributes", "rem3", HIGH, "set_delta_3_less", "Three or more attributes removed"),
        _f("attributes", "chg3", HIGH, "changed_count_3", "Three or more attribute values changed"),
        _f("attributes", "add1", MEDIUM, "set_delta_1_2_more", "One or two attributes added"),
        _f("attributes", "add3", MEDIUM, "set_delta_3_more", "Three or more attributes added"),
        _f("attributes", "chg1", MEDIUM, "changed_count_1_2", "One or two attribute values changed"),
        _f("attributes", "grp_add", MEDIUM, "group_added", "An attribute group was added"),
        _f("attributes", "grp_rem", MEDIUM, "group_removed", "An attribute group was removed"),
        _f("attributes", "val_ed10", MEDIUM, "attr_value_ed", "Attribute value edit distance >= 10%", t=0.1),
        _f("attributes", "top5_common", MEDIUM, "top5", "Attributes value is among the 5 most common"),
        _f("attributes", "shortest5", MEDIUM, "shortest5", "Attributes value is among the shortest 5%"),
        _f("attributes", "longest5", MEDIUM, "longest5", "Attributes value is among the longest 5%"),
        _f("attributes", "value_shortest5", MEDIUM, "item_short", "An attribute value is among the shortest 5%",
           item="value", dist="attributes.value"),
        _f("attributes", "value_longest5", MEDIUM, "item_long", "An attribute value is among the longest 5%",
           item="value", dist="attributes.value"),
        _f("attributes", "name_shortest5", MEDIUM, "item_short", "An attribute name is among the shortest 5%",
           item="name", dist="attributes.name"),
        _f("attributes", "name_longest5", MEDIUM, "item_long", "An attribute name is among the longest 5%",
           item="name", dist="attributes.name"),
        _f("attributes", "rare_group", MEDIUM, "item_rare", "An attribute group is among the least common",
           item="group", dist="attributes.group"),
    ]

    # Features
    cat += [
        _f("features", "rem1", HIGH, "feat_delta_1_2_less", "One or two features removed"),
        _f("features", "rem3", HIGH, "feat_delta_3_less", "Three or more features removed"),
        _f("features", "add1", MEDIUM, "feat_delta_1_2_more", "One or two features added"),
        _f("features", "add3", MEDIUM, "feat_delta_3_more", "Three or more features added"),
        _f("features", "grp_add", MEDIUM, "group_added", "A feature group was added"),
        _f("features", "grp_rem", MEDIUM, "group_removed", "A feature group was removed"),
        _f("features", "top5_common", MEDIUM, "top5", "Features value is among the 5 most common"),
        _f("features", "shortest5", MEDIUM, "shortest5", "Features value is among the shortest 5%"),
        _f("features", "longest5", MEDIUM, "longest5", "Features value is among the longest 5%"),
        _f("features", "value_shortest5", MEDIUM, "item_short", "A feature text is among the shortest 5%",
           item="text", dist="features.text"),
        _f("features", "value_longest5", MEDIUM, "item_long", "A feature text is among the longest 5%",
           item="text", dist="features.text"),
        _f("features", "rare_group", MEDIUM, "item_rare", "A feature group is among the least common",
           item="group", dist="features.group"),
    ]

    # Quantities and units
    cat += _qty_flags("in_stock_quantity")
    cat += _qty_flags("sku_quantity", high=[
        _f("sku_quantity", "qty_and_incr_gt1", HIGH, "qty_both_gt1",
           "SKU quantity and quantity increment are both > 1"),
    ])
    cat += _qty_flags("quantity_increment", high=[
        _f("quantity_increment", "qty_and_incr_gt1", HIGH, "qty_both_gt1",
           "SKU quantity and quantity increment are both > 1"),
    ])
    cat += [
        _f("sku_unit", "top5_common", MEDIUM, "top5", "SKU unit is among the 5 most common"),
        _f("sku_unit", "rare5", MEDIUM, "rare5", "SKU unit is among the least common 5%"),
        _f("sku_unit", "shortest5", MEDIUM, "shortest5", "SKU unit is among the shortest 5%"),
        _f("sku_unit", "longest5", MEDIUM, "longest5", "SKU unit is among the longest 5%"),
    ]

    # Shared base filters: Gained / Lost apply to every data field (Review cues)
    for field in DATA_FIELDS:
        cat.append(_f(field, "gained", REVIEW, "gained", "Old value empty, new value present"))
        cat.append(_f(field, "lost", REVIEW, "lost", "Old value present, new value empty"))

    unknown = {fd.predicate for fd in cat} - set(PREDICATES)
    if unknown:
        raise RuntimeError(f"catalogue references unknown predicates: {sorted(unknown)}")
    return cat


# ---------------------------------------------------------------- evaluation

@dataclass(frozen=True)
class Hit:
    sku: str
    code: str
    field: str
    priority: str
    magnitude: float
    old_value: str
    new_value: str
    description: str


def _short(v: Any, n: int = 300) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else _canon(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _display(feats: dict, field: str) -> str:
    v = feats[field].get("v")
    if v is None:
        return ""
    if _is_num(v) or isinstance(v, bool):
        return str(v)
    return _short(v)


class Evaluator:
    def __init__(self, catalog: Optional[list[FlagDef]] = None):
        self.catalog = catalog or build_catalog()
        self._by_field: dict[str, list[FlagDef]] = defaultdict(list)
        for fd in self.catalog:
            self._by_field[fd.field].append(fd)

    def evaluate(self, sku: str, old_row: dict, new_row: dict, dist: Any) -> list[Hit]:
        old, new = extract(old_row), extract(new_row)
        ctx = Ctx(old=old, new=new, dist=dist)
        hits: list[Hit] = []
        for field, defs in self._by_field.items():
            if old[field].get("v") == new[field].get("v"):
                continue  # only changed values are review cues
            for fd in defs:
                magnitude = PREDICATES[fd.predicate](ctx, field, fd.p())
                if magnitude is None:
                    continue
                hits.append(Hit(
                    sku=sku, code=fd.code, field=field, priority=fd.priority,
                    magnitude=magnitude if math.isfinite(magnitude) else 1e9,
                    old_value=_display(old, field), new_value=_display(new, field),
                    description=fd.description,
                ))
        return hits
