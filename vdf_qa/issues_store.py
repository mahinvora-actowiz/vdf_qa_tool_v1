"""Run and issue persistence in MongoDB."""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional

from pymongo import DESCENDING, ASCENDING
from pymongo.database import Database

SEVERITY = {"High": "high", "Medium": "moderate", "Review": "low", "Schema": "high"}


def owner_for(kind: str, priority: str) -> str:
    return "qa" if kind == "flag" and priority == "Review" else "crawler"


class IssueStore:
    def __init__(self, database: Database, client=None):
        self._db, self._client = database, client
        self._issues, self._runs = database["issues"], database["runs"]

    def _collection(self, period: str):
        run = self._runs.find_one({"_id": period}, {"issues_collection": 1})
        collection = self._db[run.get("issues_collection", "issues")] if run else self._issues
        collection.create_index([("period", ASCENDING), ("code", ASCENDING), ("magnitude", DESCENDING), ("sku", ASCENDING)])
        collection.create_index([("period", ASCENDING), ("owner", ASCENDING)])
        return collection

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def begin_run(self, period: str, current_path: str, previous_path: Optional[str], title: str = "") -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        daily = "issues_" + dt.date.today().isoformat().replace("-", "")
        self._db[daily].create_index([("period", ASCENDING), ("code", ASCENDING), ("magnitude", DESCENDING), ("sku", ASCENDING)])
        self._db[daily].delete_many({"period": period})
        self._runs.replace_one({"_id": period}, {"_id": period, "period": period, "status": "running",
            "title": title, "issues_collection": daily, "current_path": current_path, "previous_path": previous_path, "started_at": now}, upsert=True)

    def add_issues(self, rows: Iterable[tuple]) -> None:
        docs = []
        for period, kind, code, field, priority, sku, magnitude, old_v, new_v, message in rows:
            docs.append({"period": period, "kind": kind, "code": code, "field": field,
                "priority": priority, "severity": SEVERITY.get(priority, "moderate"),
                "owner": owner_for(kind, priority), "sku": sku, "magnitude": float(magnitude),
                "old_value": old_v, "new_value": new_v, "message": message})
        if docs:
            self._collection(docs[0]["period"]).insert_many(docs, ordered=False)

    def finish_run(self, period: str, status: str, verdict: Optional[str], summary: dict,
                   current_sha: Optional[str] = None, previous_sha: Optional[str] = None,
                   error: Optional[str] = None) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        update = {"status": status, "verdict": verdict, "summary": summary,
                  "finished_at": now, "error": error}
        if current_sha is not None: update["current_sha256"] = current_sha
        if previous_sha is not None: update["previous_sha256"] = previous_sha
        self._runs.update_one({"_id": period}, {"$set": update})

    def run(self, period: str) -> Optional[dict]:
        row = self._runs.find_one({"_id": period}, {"_id": 0})
        return row

    def runs(self) -> list[dict]:
        return list(self._runs.find({}, {"_id": 0, "period": 1, "status": 1, "verdict": 1, "finished_at": 1}).sort("period", DESCENDING))

    def flag_summary(self, period: str) -> list[dict]:
        pipeline = [{"$match": {"period": period}}, {"$group": {"_id": {"code": "$code", "kind": "$kind", "priority": "$priority", "owner": "$owner"}, "description": {"$max": "$message"}, "count": {"$sum": 1}}}]
        rows = []
        rank = {"High": 0, "Schema": 0, "Medium": 1}
        for r in self._collection(period).aggregate(pipeline):
            k = r["_id"]
            rows.append({**k, "description": r.get("description"), "count": r["count"]})
        return sorted(rows, key=lambda r: (rank.get(r["priority"], 2), -r["count"]))

    def ranked(self, period: str, code: str, offset: int = 0, limit: int = 200) -> list[dict]:
        cur = self._collection(period).find({"period": period, "code": code}, {"_id": 0, "sku": 1, "field": 1, "magnitude": 1, "old_value": 1, "new_value": 1, "message": 1, "owner": 1, "priority": 1}).sort([("magnitude", DESCENDING), ("sku", ASCENDING)]).skip(offset).limit(limit)
        return [{**r, "description": r.pop("message", None)} for r in cur]

    def iter_ranked(self, period: str, code: str, chunk: int = 5000):
        offset = 0
        while True:
            rows = self.ranked(period, code, offset, chunk)
            if not rows: return
            yield from rows
            offset += len(rows)

    def count(self, period: str, code: str) -> int:
        return self._collection(period).count_documents({"period": period, "code": code})

    def schema_errors(self, period: str, offset: int = 0, limit: int = 200) -> list[dict]:
        cur = self._collection(period).find({"period": period, "kind": "schema"}, {"_id": 0, "sku": 1, "field": 1, "old_value": 1, "message": 1}).sort("_id", ASCENDING).skip(offset).limit(limit)
        return [{"sku": r.get("sku"), "path": r.get("field"), "record_hint": r.get("old_value"), "message": r.get("message")} for r in cur]

    def report_json(self, period: str) -> Optional[dict]:
        """Return the complete run report grouped by kind, code, priority, and owner."""
        run = self.run(period)
        if run is None:
            return None
        groups = {}
        order = {"High": 0, "Schema": 0, "Medium": 1, "Review": 2}
        for issue in self._collection(period).find({"period": period}, {"_id": 0}).sort(
                [("magnitude", DESCENDING), ("sku", ASCENDING)]).batch_size(1000):
            key = (issue.get("kind"), issue.get("code"), issue.get("priority"), issue.get("owner"))
            group = groups.setdefault(key, {"kind": key[0], "code": key[1], "priority": key[2],
                "owner": key[3], "description": issue.get("message"), "count": 0, "issues": []})
            group["count"] += 1
            group["issues"].append({"sku": issue.get("sku"), "field": issue.get("field"),
                "magnitude": issue.get("magnitude"), "old_value": issue.get("old_value"),
                "new_value": issue.get("new_value"), "description": issue.get("message"),
                "owner": issue.get("owner"), "priority": issue.get("priority")})
        return {"period": period, "status": run.get("status"), "verdict": run.get("verdict"),
                "summary": run.get("summary", {}), "groups": sorted(groups.values(),
                key=lambda g: (order.get(g["priority"], 3), -g["count"], g["code"] or ""))}
