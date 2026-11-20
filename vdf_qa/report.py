"""Self-contained HTML export of a run (the "download as HTML" in the dashboard).

Per flag: the top 5 SKUs by magnitude are shown first, then every remaining SKU
in ranked order. Schema and integrity problems are listed the same way.
"""
from __future__ import annotations

import datetime as dt
import html
from typing import Optional

from .issues_store import IssueStore

TOP = 5
_CSS = """
body{font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f6f7f9;color:#1a1a1a}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.5rem;margin:0 0 4px} h2{font-size:1.1rem;margin:28px 0 6px}
.lede{color:#555;margin:0 0 16px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 20px}
.card{background:#fff;border:1px solid #e4e7eb;border-radius:8px;padding:10px 14px;min-width:140px}
.card b{display:block;font-size:1.15rem}
.verdict{display:inline-block;font-weight:700;padding:3px 10px;border-radius:999px;font-size:12px}
.v-bad{background:#fde8ee;color:#b11b3a}.v-ok{background:#d3f3df;color:#0c7a3e}.v-warn{background:#fff4e0;color:#a05b00}
.banner{background:#fff8e6;border:1px solid #f0d28a;border-radius:8px;padding:10px 14px;margin:12px 0}
.banner ul{margin:6px 0 0 18px;padding:0}
.flag{background:#fff;border:1px solid #e4e7eb;border-radius:8px;padding:14px 16px;margin-bottom:16px}
.flag p{margin:4px 0;color:#444}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px}
th,td{text-align:left;vertical-align:top;padding:6px;border-bottom:1px solid #eee}
th{color:#555;font-weight:600;background:#fafbfc}
td.val{font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all;max-width:360px}
tr.top td{background:#fffbea}
.sev{text-transform:uppercase;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;margin-right:6px}
.sev-high{background:#fde8ee;color:#b11b3a}.sev-moderate{background:#fff4e0;color:#a05b00}
.sev-low{background:#e8f2fe;color:#1464c0}.sev-schema{background:#ece6ff;color:#4b2aa6}
details summary{cursor:pointer;color:#1464c0;margin-top:8px}
"""

_PRIORITY_ORDER = {"High": 0, "Schema": 0, "Medium": 1, "Review": 2}


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _sev_class(priority: str) -> str:
    return {"High": "sev-high", "Medium": "sev-moderate", "Review": "sev-low", "Schema": "sev-schema"}.get(
        priority, "sev-moderate")


def _rows_table(rows, *, top: bool) -> str:
    out = ["<table><thead><tr><th>#</th><th>SKU</th><th>Field</th><th>Magnitude</th>"
           "<th>Old value</th><th>New value</th><th>Owner</th></tr></thead><tbody>"]
    for i, r in enumerate(rows, 1):
        cls = ' class="top"' if top else ""
        mag = r.get("magnitude")
        mag_s = "" if mag is None else (f"{mag:.3f}" if mag < 1e8 else "∞")
        out.append(
            f"<tr{cls}><td>{i}</td><td>{_esc(r.get('sku'))}</td><td>{_esc(r.get('field'))}</td>"
            f"<td>{mag_s}</td><td class=\"val\">{_esc(r.get('old_value'))}</td>"
            f"<td class=\"val\">{_esc(r.get('new_value'))}</td><td>{_esc(r.get('owner'))}</td></tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


def render_report(issues: IssueStore, period: str, max_rows_per_code: Optional[int] = None) -> str:
    run = issues.run(period)
    if run is None:
        raise LookupError(f"no run for period {period}")
    summary = run["summary"]
    verdict = run.get("verdict") or "—"
    vclass = {"Rejected": "v-bad", "Review required": "v-warn"}.get(verdict, "v-ok")
    generated = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    parts = [
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        f"<title>VDF QA — {_esc(period)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>VDF QA report: {_esc(period)} "
        f"<span class=\"verdict {vclass}\">{_esc(verdict)}</span></h1>",
        f"<p class=\"lede\">Compared with {_esc(summary.get('previous_period') or 'no previous period')} · "
        f"generated {generated}</p>",
        "<div class=\"cards\">",
    ]
    for label, value in [
        ("Records", summary.get("records")),
        ("Stored SKUs", summary.get("stored")),
        ("Schema-invalid records", summary.get("schema_invalid_records")),
        ("Duplicate SKUs", summary.get("duplicate_skus")),
        ("Missing SKU", summary.get("missing_sku")),
        ("Products compared", summary.get("products_compared")),
        ("New products", summary.get("new_products")),
        ("Lost products", summary.get("lost_products")),
        ("High hits", (summary.get("hits_by_priority") or {}).get("High", 0)),
        ("Medium hits", (summary.get("hits_by_priority") or {}).get("Medium", 0)),
        ("Review cues", (summary.get("hits_by_priority") or {}).get("Review", 0)),
    ]:
        parts.append(f"<div class=\"card\">{_esc(label)}<b>{_esc(value if value is not None else 0)}</b></div>")
    parts.append("</div>")
    if summary.get("reasons"):
        parts.append("<p><b>Reasons:</b> " + _esc("; ".join(summary["reasons"])) + "</p>")

    flags = issues.flag_summary(period)
    flags.sort(key=lambda f: (_PRIORITY_ORDER.get(f["priority"], 3), -f["count"]))

    # A flag that fires on most compared rows usually means the feed changed, not that
    # each row is wrong. Say so at the top, but leave every row in the list.
    compared = summary.get("products_compared") or 0
    systemic = [f for f in flags if f["kind"] == "flag" and compared >= 50 and f["count"] > 0.5 * compared]
    if systemic:
        items = "".join(
            f"<li><b>{_esc(f['code'])}</b>: {f['count']} of {compared} compared rows "
            f"({100 * f['count'] / compared:.0f}%)</li>" for f in systemic)
        parts.append("<div class=\"banner\"><b>Likely feed-wide changes.</b> These flags fire on most "
                     "compared rows. Check the vendor feed format before you review rows one by one."
                     f"<ul>{items}</ul></div>")
    for f in flags:
        code, priority, owner, count = f["code"], f["priority"], f["owner"], f["count"]
        if f["kind"] == "flag":
            heading = f"{code}"
        else:
            heading = f"{code} ({f['kind']})"
        parts.append("<section class=\"flag\">")
        parts.append(
            f"<h2><span class=\"sev {_sev_class(priority)}\">{_esc(priority)}</span>"
            f"{_esc(heading)} · {count} issue(s) · owner: {_esc(owner)}</h2>"
        )
        parts.append(f"<p>{_esc(f['description'])}</p>")
        rows: list[dict] = []
        for idx, row in enumerate(issues.iter_ranked(period, code)):
            rows.append(row)
            if max_rows_per_code and idx + 1 >= max_rows_per_code:
                break
        top_rows, rest_rows = rows[:TOP], rows[TOP:]
        parts.append("<h3 style=\"font-size:.95rem;margin:10px 0 0\">Top 5</h3>")
        parts.append(_rows_table(top_rows, top=True) if top_rows else "<p>No rows.</p>")
        if rest_rows:
            parts.append(f"<details><summary>Remaining {len(rest_rows)} SKU(s) in ranked order</summary>")
            parts.append(_rows_table(rest_rows, top=False))
            parts.append("</details>")
        parts.append("</section>")

    parts.append("</main></body></html>")
    return "".join(parts)


def write_report(issues: IssueStore, period: str, path: str, max_rows_per_code: Optional[int] = None) -> str:
    content = render_report(issues, period, max_rows_per_code)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path
