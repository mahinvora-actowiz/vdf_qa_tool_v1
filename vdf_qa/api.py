"""JSON API consumed by the dashboard (frontend is out of scope).

Endpoints
---------
POST /runs                               compare current and previous files (202, runs in background)
GET  /runs                               all runs and their verdicts
GET  /runs/{period}                      run metadata and summary
GET  /runs/{period}/flags                per flag: total count and top 5 SKUs
GET  /runs/{period}/flags/{code}         paginated rows after the top 5 for one flag
GET  /runs/{period}/schema-errors        schema, duplicate and missing-SKU problems
GET  /runs/{period}/report.html          self-contained HTML download
GET  /runs/{period}/report.json          report summary and paginated issue rows

Auth: if VDF_API_KEY is set, every request must send header `X-API-Key`.
Runs are serialised with a process-wide lock to avoid overlapping runs for the same process.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pymongo import MongoClient
from pydantic import BaseModel, Field

from .config import Settings
from .issues_store import IssueStore
from .pipeline import RunOptions, run_period
from datetime import datetime, timezone
from .report import render_report
from .schema_check import SchemaChecker
from .snapshot import SnapshotStore

log = logging.getLogger("vdf_qa.api")
_RUN_LOCK = threading.Lock()


_DATE = r"^\d{4}-\d{2}-\d{2}$"


class RunRequest(BaseModel):
    title: str = Field(min_length=1, description="title for this run")
    current_path: str = Field(description="file name or path inside VDF_INPUT_DIR")
    previous_path: str = Field(description="previous file name or path inside VDF_INPUT_DIR")

    model_config = {"extra": "forbid"}


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="VDF QA API", version="1.1.0")
    input_root = Path(settings.input_dir).resolve()

    def auth(x_api_key: Optional[str] = Header(default=None)) -> None:
        if settings.api_key and x_api_key != settings.api_key:
            raise HTTPException(status_code=401, detail="invalid API key")

    def inside_input_dir(raw: str) -> str:
        """Only files under VDF_INPUT_DIR may be read. Blocks path traversal such as ../../etc/passwd."""
        candidate = Path(raw)
        resolved = (candidate if candidate.is_absolute() else input_root / candidate).resolve()
        if not resolved.is_relative_to(input_root) or not resolved.is_file():
            raise HTTPException(400, "file must exist inside the configured input folder")
        return str(resolved)

    def open_stores():
        client = MongoClient(settings.mongo_uri)
        db = client[settings.mongo_database]
        return client, SnapshotStore(db), IssueStore(db)

    @contextmanager
    def issues_store() -> Iterator[IssueStore]:
        client, _, store = open_stores()
        try:
            yield store
        finally:
            client.close()

    def run_job(opts: RunOptions) -> None:
        with _RUN_LOCK:
            client, snapshots, issues = open_stores()
            try:
                checker = SchemaChecker(settings.schema_dir, settings.entry_schema)
                run_period(opts, snapshots, issues, checker)
            except Exception:  # the failure is already persisted in the runs table
                log.exception("run %s failed", opts.period)
            finally:
                snapshots.close()
                issues.close()
                client.close()

    @app.post("/runs", status_code=202, dependencies=[Depends(auth)])
    def start_run(req: RunRequest):
        current = inside_input_dir(req.current_path)
        previous = inside_input_dir(req.previous_path)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        opts = RunOptions(
            period=run_id,
            title=req.title,
            current_path=current,
            previous_path=previous,
            reject_on_schema_error=settings.reject_on_schema_error,
            max_flag_hits=settings.max_flag_hits,
        )
        client, snapshots, issues = open_stores()
        try:
            issues.queue_run(run_id, req.title, current, previous)
        finally:
            snapshots.close()
            issues.close()
            client.close()
        threading.Thread(target=run_job, args=(opts,), daemon=True, name=f"vdf-run-{run_id}").start()
        return {"run_id": run_id, "status": "queued"}

    @app.get("/runs", dependencies=[Depends(auth)])
    def list_runs():
        with issues_store() as store:
            return store.runs()

    @app.get("/runs/{period}", dependencies=[Depends(auth)])
    def get_run(period: str):
        with issues_store() as store:
            run = store.run(period)
        if run is None:
            raise HTTPException(404, "no run for this period")
        return run

    @app.get("/runs/{period}/flags", dependencies=[Depends(auth)])
    def flags(period: str):
        with issues_store() as store:
            out = []
            for f in store.flag_summary(period):
                out.append({**f, "total_count": f["count"],
                            "top": store.ranked(period, f["code"], 0, 5)})
            return out

    @app.get("/runs/{period}/flags/{code}", dependencies=[Depends(auth)])
    def flag_rows(period: str, code: str,
                  offset: int = Query(0, ge=0),
                  limit: int = Query(200, ge=1, le=5000)):
        with issues_store() as store:
            total = store.count(period, code)
            remaining = max(total - 5, 0)
            return {
                "period": period,
                "code": code,
                "total_count": total,
                "remaining_count": remaining,
                "offset": offset,
                "limit": limit,
                "next_offset": min(offset + limit, remaining),
                "has_more": offset + limit < remaining,
                "rows": store.ranked(period, code, offset + 5, limit),
            }

    @app.get("/runs/{period}/schema-errors", dependencies=[Depends(auth)])
    def schema_errors(period: str, offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=5000)):
        with issues_store() as store:
            return store.schema_errors(period, offset, limit)

    @app.get("/runs/{period}/report.html", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def report(period: str):
        with issues_store() as store:
            try:
                content = render_report(store, period)
            except LookupError:
                raise HTTPException(404, "no run for this period")
        return HTMLResponse(
            content,
            headers={"Content-Disposition": f'attachment; filename="vdf_qa_{period}.html"'},
        )

    @app.get("/runs/{period}/report.json", dependencies=[Depends(auth)])
    def report_json(period: str, offset: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000)):
        with issues_store() as store:
            result = store.report_json(period, offset, limit)
        if result is None:
            raise HTTPException(404, "no run for this period")
        return result

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
