"""SQLite data store for loaded tables, rejects and dataset versions. All SQL is parameterised."""
import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pandas as pd

from app.config import db_path
from app.data.loader import TABLES, load_csv
from app.data.validate import OPTIONAL, SCHEMA, validate_table


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path(), check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS datasets (
            tbl TEXT PRIMARY KEY, sha256 TEXT, rows_loaded INT, rows_rejected INT, loaded_at TEXT);
        CREATE TABLE IF NOT EXISTS rejects (
            tbl TEXT, file TEXT, row INT, col TEXT, reason TEXT);
    """)
    return con


def _date_cols(table: str) -> list[str]:
    types = {**SCHEMA[table], **OPTIONAL.get(table, {})}
    return [c for c, k in types.items() if k == "date"]


def save_table(table: str, df: pd.DataFrame, rejects: list[dict], sha: str) -> None:
    out = df.copy()
    for c in _date_cols(table):
        if c in out:
            out[c] = out[c].map(lambda d: d.isoformat() if isinstance(d, date) else None)
    with closing(connect()) as con, con:
        out.to_sql(f"t_{table}", con, if_exists="replace", index=False)
        con.execute("DELETE FROM rejects WHERE tbl = ?", (table,))
        con.executemany("INSERT INTO rejects VALUES (?, ?, ?, ?, ?)",
                        [(table, r["file"], r["row"], r["column"], r["reason"]) for r in rejects])
        con.execute("INSERT OR REPLACE INTO datasets VALUES (?, ?, ?, ?, ?)",
                    (table, sha, len(df), len(rejects), datetime.now(UTC).isoformat()))


def read_table(table: str) -> pd.DataFrame | None:
    if table not in TABLES:
        raise ValueError(table)
    with closing(connect()) as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (f"t_{table}",)).fetchone()
        if not exists:
            return None
        df = pd.read_sql_query(f'SELECT * FROM "t_{table}"', con)  # name is from the TABLES allow-list
    for c in _date_cols(table):
        if c in df:
            df[c] = df[c].map(lambda s: date.fromisoformat(s) if isinstance(s, str) and s else None)
    return df


def read_all() -> dict[str, pd.DataFrame]:
    return {t: df for t in TABLES if (df := read_table(t)) is not None}


def datasets() -> dict[str, dict]:
    with closing(connect()) as con:
        rows = con.execute("SELECT tbl, sha256, rows_loaded, rows_rejected, loaded_at FROM datasets").fetchall()
    return {r[0]: {"sha256": r[1][:12], "rows_loaded": r[2], "rows_rejected": r[3], "loaded_at": r[4]}
            for r in rows}


def get_rejects() -> list[dict]:
    with closing(connect()) as con:
        rows = con.execute("SELECT file, row, col, reason FROM rejects ORDER BY tbl, row").fetchall()
    return [{"file": f, "row": r, "column": c, "reason": m} for f, r, c, m in rows]


def ingest_bytes(content: bytes, table: str, filename: str | None = None) -> dict:
    """Load, validate and store one CSV. Shared by POST /ingest and the CLI loader.

    Raises LoadError / FileRejected when nothing in the file is usable.
    """
    df = load_csv(content, table)
    valid, rejects = validate_table(table, df, file=f"{table}.csv")
    if table == "capacity_events":
        plants = read_table("plants")
        if plants is not None:
            known = set(plants["plant"])
            bad = ~valid["plant"].isin(known)
            for i in valid.index[bad]:
                rejects.append({"file": f"{table}.csv", "row": None, "column": "plant",
                                "reason": f"Unknown plant {valid.at[i, 'plant']} (event {valid.at[i, 'event_id']})"})
            valid = valid[~bad].reset_index(drop=True)
    save_table(table, valid, rejects, hashlib.sha256(content).hexdigest())
    status = "loaded" if not rejects else ("partial" if len(valid) else "rejected")
    return {"table": table, "rows_loaded": len(valid), "rows_rejected": len(rejects),
            "status": status, "rejects": rejects[:200]}
