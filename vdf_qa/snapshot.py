"""MongoDB-backed monthly snapshots keyed by period and SKU."""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from pymongo import ASCENDING, UpdateOne
from pymongo.database import Database

from .jsonstream import iter_json_array


@dataclass(frozen=True)
class IngestResult:
    month: str
    sha256: str
    records: int
    stored: int
    duplicates: int
    missing_sku: int
    reused: bool


def file_sha256(path: str, chunk_size: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


class SnapshotStore:
    def __init__(self, database: Database):
        self._db = database
        self._rows = database["snapshot_work"]
        self._meta = database["snapshot_meta"]
        self._meta.create_index("month", unique=True)

    def close(self) -> None:
        pass  # Mongo client is shared by stores and closed by its owner.

    def clear_rows(self) -> None:
        """Compatibility no-op: content-addressed snapshots are retained for reuse."""

    @property
    def database(self) -> Database:
        return self._db

    def meta(self, month: str) -> Optional[dict]:
        row = self._meta.find_one({"_id": month})
        if row is None:
            return None
        row.pop("_id", None)
        return row

    def ingest(self, month: str, path: str,
               on_record: Optional[Callable[[int, object], None]] = None,
               on_duplicate: Optional[Callable[[str], None]] = None,
               on_missing_sku: Optional[Callable[[int], None]] = None,
               on_progress: Optional[Callable[[float], None]] = None,
               sha256: Optional[str] = None,
               batch_size: int = 5000, force: bool = False) -> IngestResult:
        self._rows.create_index([("month", ASCENDING), ("sku", ASCENDING)], unique=True)
        digest = sha256 or file_sha256(path)
        existing = self.meta(month)
        if (existing and existing["sha256"] == digest and not force
                and self._rows.count_documents({"month": month}) == existing["stored"]):
            return IngestResult(month, digest, existing["records"], existing["stored"],
                                existing["duplicates"], existing["missing_sku"], True)

        self._rows.delete_many({"month": month})
        records = stored = duplicates = missing = 0
        batch: dict[str, dict] = {}

        def flush() -> None:
            nonlocal stored, duplicates
            if not batch:
                return
            skus = list(batch)
            result = self._rows.bulk_write([
                UpdateOne({"month": month, "sku": sku},
                          {"$setOnInsert": {"payload": batch[sku]}}, upsert=True)
                for sku in skus
            ], ordered=False)
            inserted = set(result.upserted_ids)
            stored += len(inserted)
            duplicates += len(skus) - len(inserted)
            for i, sku in enumerate(skus):
                if i not in inserted and on_duplicate:
                    on_duplicate(sku)
            batch.clear()
        for index, record in enumerate(iter_json_array(path, on_progress=on_progress)):
            records += 1
            if on_record:
                on_record(index, record)
            sku = record.get("sku") if isinstance(record, dict) else None
            if not isinstance(sku, str) or not sku.strip():
                missing += 1
                if on_missing_sku:
                    on_missing_sku(index)
                continue
            if sku in batch:
                duplicates += 1
                if on_duplicate:
                    on_duplicate(sku)
                continue
            batch[sku] = record
            if len(batch) >= batch_size:
                flush()
        flush()
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self._meta.replace_one({"_id": month}, {"_id": month, "month": month, "source_path": path,
            "sha256": digest, "records": records, "stored": stored, "duplicates": duplicates,
            "missing_sku": missing, "ingested_at": now}, upsert=True)
        return IngestResult(month, digest, records, stored, duplicates, missing, False)

    def iter_rows(self, month: str) -> Iterator[tuple[str, dict]]:
        for row in self._rows.find({"month": month}, {"sku": 1, "payload": 1}).sort("sku", ASCENDING).batch_size(1000):
            yield row["sku"], row["payload"]

    def get(self, month: str, sku: str) -> Optional[dict]:
        row = self._rows.find_one({"month": month, "sku": sku}, {"payload": 1})
        return row["payload"] if row else None

    def row_count(self, month: str) -> int:
        return self._rows.count_documents({"month": month})

    def count_only_in(self, month_a: str, month_b: str) -> int:
        pipeline = [
            {"$match": {"month": month_a}},
            {"$lookup": {"from": "snapshot_work", "let": {"sku": "$sku"}, "pipeline": [
                {"$match": {"month": month_b}}, {"$match": {"$expr": {"$eq": ["$sku", "$$sku"]}}},
                {"$limit": 1}], "as": "match"}},
            {"$match": {"match": {"$size": 0}}}, {"$count": "count"}]
        result = list(self._rows.aggregate(pipeline))
        return result[0]["count"] if result else 0
