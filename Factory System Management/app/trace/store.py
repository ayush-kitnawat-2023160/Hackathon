"""Run history and trace steps in SQLite (local only; nothing leaves the machine)."""
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

from app.config import db_path


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(db_path(), check_same_thread=False)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, created_at TEXT, request TEXT, answer_type TEXT, plan_valid INT,
            llm_calls INT, tokens INT, latency_ms INT, parent_run_id TEXT, inputs TEXT, response TEXT,
            plan TEXT);
        CREATE TABLE IF NOT EXISTS trace_steps (
            run_id TEXT, seq INT, agent TEXT, tool TEXT, input TEXT, output TEXT, ms INT);
    """)
    return con


def _json(v) -> str:
    return json.dumps(v, default=lambda o: o.isoformat() if isinstance(o, (date, datetime)) else str(o))


def new_run_id() -> str:
    return "al-" + secrets.token_hex(3)


def save_trace(run_id: str, request: str, response: dict, steps: list[dict], llm_calls: int, tokens: int,
               latency_ms: int, inputs: dict, plan: dict | None = None, parent_run_id: str | None = None) -> str:
    valid = (response.get("validator") or {}).get("valid")
    with closing(_con()) as con, con:
        con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, datetime.now(UTC).isoformat(timespec="seconds"), request,
                     response.get("answer_type"), None if valid is None else int(valid), llm_calls, tokens,
                     latency_ms, parent_run_id, _json(inputs), _json(response), _json(plan) if plan else None))
        con.executemany("INSERT INTO trace_steps VALUES (?,?,?,?,?,?,?)",
                        [(run_id, i, s.get("agent"), s.get("tool"), _json(s.get("input")), _json(s.get("output")),
                          s.get("ms")) for i, s in enumerate(steps)])
    return run_id


def get_trace(run_id: str) -> dict | None:
    with closing(_con()) as con:
        r = con.execute("SELECT run_id, created_at, request, answer_type, llm_calls, tokens, latency_ms, "
                        "parent_run_id, inputs FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not r:
            return None
        steps = con.execute("SELECT agent, tool, input, output, ms FROM trace_steps WHERE run_id = ? ORDER BY seq",
                            (run_id,)).fetchall()
    return {"run_id": r[0], "created_at": r[1], "request": r[2], "answer_type": r[3],
            "steps": [{"agent": a, "tool": t, "input": json.loads(i), "output": json.loads(o), "ms": ms}
                      for a, t, i, o, ms in steps],
            "llm_calls": r[4], "tokens": r[5], "latency_ms": r[6], "parent_run_id": r[7],
            "data_used": json.loads(r[8])}


def list_runs(limit: int = 50) -> list[dict]:
    with closing(_con()) as con:
        rows = con.execute("SELECT run_id, created_at, request, answer_type, plan_valid, llm_calls, tokens, latency_ms "
                           "FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    keys = ("run_id", "created_at", "request", "answer_type", "plan_valid", "llm_calls", "tokens", "latency_ms")
    return [dict(zip(keys, r, strict=False)) for r in rows]


def get_run(run_id: str) -> dict | None:
    with closing(_con()) as con:
        r = con.execute("SELECT response, plan FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not r:
        return None
    return {"response": json.loads(r[0]), "plan": json.loads(r[1]) if r[1] else None}


def latest_run(answer_types: tuple[str, ...], with_plan: bool = True) -> tuple[str, dict] | None:
    q = ",".join("?" * len(answer_types))
    with closing(_con()) as con:
        r = con.execute(f"SELECT run_id FROM runs WHERE answer_type IN ({q}) {'AND plan IS NOT NULL' if with_plan else ''} "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1", answer_types).fetchone()
    return (r[0], get_run(r[0])) if r else None
