"""Command line: run a month, export the HTML report, or serve the API.

  python -m vdf_qa run --current data_file_2026_10_31.json --previous data_file_2026_09_30.json
  python -m vdf_qa report --period 2026-10-31 --out vdf_2026-10-31.html
  python -m vdf_qa serve --port 8000
"""
from __future__ import annotations

import argparse
import json
import sys
from pymongo import MongoClient
from typing import Optional

from .config import Settings
from .issues_store import IssueStore
from .pipeline import RunOptions, run_period
from datetime import datetime, timezone
from .report import write_report
from .schema_check import SchemaChecker
from .snapshot import SnapshotStore


def _open(settings: Settings) -> tuple[SnapshotStore, IssueStore]:
    client = MongoClient(settings.mongo_uri)
    database = client[settings.mongo_database]
    return SnapshotStore(database), IssueStore(database, client)


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    title = args.title
    if title is None:
        title = input("Run title: ").strip()
    if not title:
        print("error: run title cannot be empty", file=sys.stderr)
        return 2
    checker = None
    if not args.no_schema:
        checker = SchemaChecker(args.schema_dir or settings.schema_dir,
                                args.entry_schema or settings.entry_schema)
    opts = RunOptions(
        period=run_id,
        title=title,
        current_path=args.current,
        previous_path=args.previous,
        reject_on_schema_error=settings.reject_on_schema_error,
        max_flag_hits=settings.max_flag_hits,
    )
    snapshots, issues = _open(settings)
    try:
        summary = run_period(opts, snapshots, issues, checker)
    finally:
        snapshots.close()
        issues.close()
    print(json.dumps({"run_id": summary.period, "verdict": summary.verdict, "reasons": summary.reasons,
                      "records": summary.records, "hits_by_priority": summary.hits_by_priority},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    _, issues = _open(settings)
    try:
        write_report(issues, args.period, args.out)
    finally:
        issues.close()
    print(f"wrote {args.out}")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    try:
        import uvicorn
    except ImportError:
        print("error: uvicorn is not installed (pip install -r requirements.txt)", file=sys.stderr)
        return 2
    from .api import create_app
    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="vdf-qa")
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="validate and compare one monthly data file")
    r.add_argument("--current", required=True)
    r.add_argument("--previous")
    r.add_argument("--title", help="title for this run (prompted when omitted)")
    r.add_argument("--schema-dir")
    r.add_argument("--entry-schema")
    r.add_argument("--no-schema", action="store_true", help="skip schema validation")

    rep = sub.add_parser("report", help="write the self-contained HTML report")
    rep.add_argument("--period", required=True)
    rep.add_argument("--out", required=True)

    srv = sub.add_parser("serve", help="run the JSON API (needs fastapi + uvicorn)")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    handlers = {"run": cmd_run, "report": cmd_report, "serve": cmd_serve}
    return handlers[args.cmd](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
