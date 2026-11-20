"""Monthly QA run: schema -> snapshot -> distributions -> SKU-wise VDF flags -> verdict.

One run for one data file (period) against the previous period's file:

1. Stream the current file once. Each record is schema-checked, stored as a
   snapshot row, and checked for duplicate or missing SKUs in the same pass.
2. Ingest the previous period's file (skipped if its SHA-256 is unchanged).
3. Build rank statistics (top 5, rare 5%, short/long 5%, lowest/highest 5%)
   from the current period.
4. For every SKU present in both periods, evaluate the VDF flags on changed fields.
5. Persist issues, then compute the verdict.

Memory stays bounded: records are streamed, snapshots live in MongoDB, and
issues are written in batches.
"""
from __future__ import annotations

import datetime as dt
import re
import hashlib
from dataclasses import dataclass, field
from typing import Optional

from .issues_store import IssueStore
from .distribution import Distribution
from .rules import HIGH, Evaluator
from .schema_check import SchemaChecker
from .snapshot import SnapshotStore

BATCH = 5000
_FILE_RE = re.compile(r"data_file_(\d{4})_(\d{2})_(\d{2})\.json$")


@dataclass
class RunOptions:
    period: str                       # generated run id
    current_path: str
    previous_path: Optional[str] = None
    title: str = ""
    previous_period: Optional[str] = None
    reject_on_schema_error: bool = True
    max_flag_hits: Optional[int] = None   # optional cap on High-priority hits; None = no cap


@dataclass
class RunSummary:
    period: str
    previous_period: Optional[str]
    verdict: str
    reasons: list[str]
    records: int = 0
    stored: int = 0
    duplicate_skus: int = 0
    missing_sku: int = 0
    schema_invalid_records: int = 0
    schema_error_count: int = 0
    products_compared: int = 0
    new_products: int = 0
    lost_products: int = 0
    rows_with_flags: int = 0
    hits_total: int = 0
    hits_by_priority: dict = field(default_factory=dict)
    hits_by_code: dict = field(default_factory=dict)


def period_from_filename(path: str) -> Optional[str]:
    m = _FILE_RE.search(path)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def previous_month_end(period: str) -> str:
    d = dt.date.fromisoformat(period)
    first_of_month = d.replace(day=1)
    return (first_of_month - dt.timedelta(days=1)).isoformat()


class _Buffer:
    def __init__(self, issues: IssueStore):
        self._issues = issues
        self._rows: list[tuple] = []

    def add(self, row: tuple) -> None:
        self._rows.append(row)
        if len(self._rows) >= BATCH:
            self.flush()

    def flush(self) -> None:
        if self._rows:
            self._issues.add_issues(self._rows)
            self._rows = []


def run_period(
    opts: RunOptions,
    snapshots: SnapshotStore,
    issues: IssueStore,
    checker: Optional[SchemaChecker] = None,
    evaluator: Optional[Evaluator] = None,
) -> RunSummary:
    evaluator = evaluator or Evaluator()
    cur_period = "snapshot_" + _file_hash(opts.current_path)
    prev_period = "snapshot_" + _file_hash(opts.previous_path) if opts.previous_path else None
    issues.begin_run(opts.period, opts.current_path, opts.previous_path, opts.title)
    buf = _Buffer(issues)
    s = RunSummary(period=opts.period, previous_period=prev_period, verdict="", reasons=[])

    try:
        # 1. Current file: schema, duplicates, missing SKU, snapshot, one pass.
        def on_record(index: int, record) -> None:
            if checker is None:
                return
            errs = checker.check(record)
            if not errs:
                return
            s.schema_invalid_records += 1
            s.schema_error_count += len(errs)
            sku = record.get("sku") if isinstance(record, dict) else None
            for e in errs:
                buf.add((opts.period, "schema", "SCHEMA", e.path, "Schema", sku, 1.0, None, None, e.message))

        def on_duplicate(sku: str) -> None:
            buf.add((opts.period, "duplicate_sku", "DUPLICATE_SKU", "sku", HIGH, sku, 1.0, None, None,
                     "SKU appears more than once; first occurrence kept"))

        def on_missing(index: int) -> None:
            buf.add((opts.period, "missing_sku", "MISSING_SKU", "sku", HIGH, None, 1.0, None, None,
                     f"Record at array index {index} has no usable SKU"))

        cur = snapshots.ingest(cur_period, opts.current_path, on_record=on_record,
                               on_duplicate=on_duplicate, on_missing_sku=on_missing, force=True)
        buf.flush()
        s.records, s.stored = cur.records, cur.stored
        s.duplicate_skus, s.missing_sku = cur.duplicates, cur.missing_sku

        # 2. Previous file (snapshot reused if the SHA-256 matches).
        prev_sha = None
        if opts.previous_path and prev_period:
            prev = snapshots.ingest(prev_period, opts.previous_path)
            prev_sha = prev.sha256

        # 3. Rank statistics from the current period.
        dist = Distribution.build(snapshots.database, opts.period,
                                  (row for _, row in snapshots.iter_rows(cur_period)))

        # 4. SKU-wise comparison and flags.
        by_priority: dict[str, int] = {}
        by_code: dict[str, int] = {}
        if prev_period and snapshots.meta(prev_period):
            for sku, new_row in snapshots.iter_rows(cur_period):
                old_row = snapshots.get(prev_period, sku)
                if old_row is None:
                    s.new_products += 1
                    continue
                s.products_compared += 1
                hits = evaluator.evaluate(sku, old_row, new_row, dist)
                if not hits:
                    continue
                s.rows_with_flags += 1
                for h in hits:
                    s.hits_total += 1
                    by_priority[h.priority] = by_priority.get(h.priority, 0) + 1
                    by_code[h.code] = by_code.get(h.code, 0) + 1
                    buf.add((opts.period, "flag", h.code, h.field, h.priority, sku, h.magnitude,
                             h.old_value, h.new_value, h.description))
            buf.flush()
            s.lost_products = snapshots.count_only_in(prev_period, cur_period)
        s.hits_by_priority, s.hits_by_code = by_priority, by_code
        dist.drop()

        # 5. Verdict. The VDF Review Flags reference says flags are review cues and that
        # their classification is not finding severity. So flags never reject a file by
        # themselves. Rejection comes from hard data failures only. Flags that were hit
        # make the run "Review required", and an optional cap can reject on High hits.
        high_hits = by_priority.get(HIGH, 0)
        if s.schema_invalid_records and opts.reject_on_schema_error:
            s.reasons.append(f"{s.schema_invalid_records} records fail the client schema")
        if s.duplicate_skus:
            s.reasons.append(f"{s.duplicate_skus} duplicate SKUs")
        if s.missing_sku:
            s.reasons.append(f"{s.missing_sku} records without SKU")
        hard_fail = bool(s.reasons)
        if opts.max_flag_hits is not None and high_hits > opts.max_flag_hits:
            s.reasons.append(f"{high_hits} High-priority review cues exceed the configured cap of {opts.max_flag_hits}")
        if hard_fail or (opts.max_flag_hits is not None and high_hits > opts.max_flag_hits):
            s.verdict = "Rejected"
        elif s.hits_total:
            s.verdict = "Review required"
            s.reasons.append(f"{s.hits_total} review cues to check ({high_hits} High, "
                             f"{by_priority.get('Medium', 0)} Medium, {by_priority.get('Review', 0)} Review)")
        else:
            s.verdict = "Accepted"

        summary = _summary_dict(s, cur.sha256, prev_sha)
        issues.finish_run(opts.period, "done", s.verdict, summary,
                          current_sha=cur.sha256, previous_sha=prev_sha)
        return s
    except Exception as exc:  # persist the failure so the dashboard can show it
        buf.flush()
        issues.finish_run(opts.period, "failed", None, {}, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        snapshots.clear_rows()


def _summary_dict(s: RunSummary, cur_sha: str, prev_sha: Optional[str]) -> dict:
    return {
        "period": s.period,
        "previous_period": s.previous_period,
        "verdict": s.verdict,
        "reasons": s.reasons,
        "records": s.records,
        "stored": s.stored,
        "duplicate_skus": s.duplicate_skus,
        "missing_sku": s.missing_sku,
        "schema_invalid_records": s.schema_invalid_records,
        "schema_error_count": s.schema_error_count,
        "products_compared": s.products_compared,
        "new_products": s.new_products,
        "lost_products": s.lost_products,
        "rows_with_flags": s.rows_with_flags,
        "hits_total": s.hits_total,
        "hits_by_priority": s.hits_by_priority,
        "hits_by_code": s.hits_by_code,
        "current_sha256": cur_sha,
        "previous_sha256": prev_sha,
    }
