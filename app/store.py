"""SQLite input snapshots, allocations, and auditable run history."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _json(value):
    return json.dumps(value, sort_keys=True, default=str)


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS source_tables (
                    name TEXT PRIMARY KEY, content TEXT NOT NULL,
                    checksum TEXT NOT NULL, row_count INTEGER NOT NULL,
                    uploaded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_versions (
                    name TEXT NOT NULL, checksum TEXT NOT NULL,
                    content TEXT NOT NULL, uploaded_at TEXT NOT NULL,
                    PRIMARY KEY (name, checksum)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT UNIQUE NOT NULL, request TEXT NOT NULL,
                    answer_type TEXT NOT NULL, result_json TEXT NOT NULL,
                    input_versions_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trace_steps (
                    run_id TEXT NOT NULL, step_number INTEGER NOT NULL,
                    step_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, step_number),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS allocations (
                    run_id TEXT NOT NULL, order_id TEXT NOT NULL,
                    plant TEXT NOT NULL, week INTEGER NOT NULL,
                    cost REAL NOT NULL, carbon_kg REAL NOT NULL,
                    arrival TEXT NOT NULL, on_time INTEGER NOT NULL,
                    PRIMARY KEY (run_id, order_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def put_table(self, name, content, row_count):
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute("""INSERT OR IGNORE INTO source_versions
                (name,checksum,content,uploaded_at) VALUES (?,?,?,?)""",
                (name, checksum, content, now))
            db.execute("""INSERT INTO source_tables (name,content,checksum,row_count,uploaded_at)
                VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                content=excluded.content,checksum=excluded.checksum,
                row_count=excluded.row_count,uploaded_at=excluded.uploaded_at""",
                (name, content, checksum, row_count, now))
        return checksum

    def snapshot(self):
        with self.connection() as db:
            rows = db.execute("SELECT name,content,checksum FROM source_tables").fetchall()
        return ({r["name"]: r["content"] for r in rows},
                {r["name"]: r["checksum"] for r in rows})

    def save_run(self, run_id, request, result, versions, steps):
        with self.connection() as db:
            db.execute("""INSERT INTO runs
                (run_id,request,answer_type,result_json,input_versions_json,created_at)
                VALUES (?,?,?,?,?,?)""", (run_id, request,
                result["answer_type"], _json(result), _json(versions),
                datetime.now(timezone.utc).isoformat()))
            for i, step in enumerate(steps):
                db.execute("INSERT INTO trace_steps VALUES (?,?,?)", (run_id, i, _json(step)))
            for item in result.get("allocation", []):
                db.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?,?)", (
                    run_id, item["order_id"], item["plant"], item["week"],
                    item["cost"], item["carbon_kg"], item["arrival"],
                    int(item["on_time"])))

    def latest_plan(self):
        with self.connection() as db:
            rows = db.execute("""SELECT result_json FROM runs
                WHERE answer_type IN ('allocation','scenario') ORDER BY id DESC LIMIT 20""").fetchall()
        for row in rows:
            result = json.loads(row["result_json"])
            if result.get("validator", {}).get("valid") and result.get("allocation"):
                return result
        return None

    def get_trace(self, run_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            steps = db.execute("SELECT step_json FROM trace_steps WHERE run_id=? ORDER BY step_number",
                               (run_id,)).fetchall()
        result = json.loads(row["result_json"])
        return {"run_id": run_id, "request": row["request"],
                "answer_type": row["answer_type"], "created_at": row["created_at"],
                "input_versions": json.loads(row["input_versions_json"]),
                "steps": [json.loads(s["step_json"]) for s in steps],
                "llm_calls": result.get("llm_calls", 0),
                "tokens": result.get("tokens"),
                "result": result}
