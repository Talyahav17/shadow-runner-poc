"""SQLite persistence for Shadow Runner comparison results.

Every shadow comparison (legacy COBOL vs modern Python) is recorded here
so the match-rate can be queried after the fact -- e.g. "what % of real
traffic would the modern path get right" -- instead of only being visible
as scrolled-past log lines.
"""

import os
import sqlite3
import time
from typing import Optional

# SHADOW_DB_DIR lets a deployment (e.g. the Docker image) point this at a
# mounted volume so match-rate history survives a container restart; local
# dev defaults to alongside this file, unchanged.
DB_DIR = os.environ.get("SHADOW_DB_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "shadow_results.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    loan_amount REAL NOT NULL,
    interest_rate REAL NOT NULL,
    legacy_result REAL,
    shadow_result REAL,
    diff REAL,
    status TEXT NOT NULL,
    detail TEXT
)
"""


def init_db(db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        # WAL mode is a persistent property of the db file (set once here,
        # not per-connection): it lets shadow-comparison writes and
        # dashboard reads proceed concurrently instead of blocking on the
        # single-writer lock plain SQLite uses by default.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record_result(
    loan_amount: float,
    interest_rate: float,
    legacy_result: Optional[float],
    shadow_result: Optional[float],
    diff: Optional[float],
    status: str,
    detail: str = "",
    db_path: str = DB_PATH,
) -> None:
    """status is one of 'success', 'mismatch', 'error'."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO shadow_results "
            "(ts, loan_amount, interest_rate, legacy_result, shadow_result, diff, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), loan_amount, interest_rate, legacy_result, shadow_result, diff, status, detail),
        )
        conn.commit()
    finally:
        conn.close()


def get_match_stats(db_path: str = DB_PATH) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT status, COUNT(*) FROM shadow_results GROUP BY status")
        counts = {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()

    total = sum(counts.values())
    success = counts.get("success", 0)
    match_rate = (success / total * 100) if total else None

    return {
        "total": total,
        "success": success,
        "mismatch": counts.get("mismatch", 0),
        "error": counts.get("error", 0),
        "match_rate_pct": round(match_rate, 2) if match_rate is not None else None,
    }


def get_recent_results(limit: int = 50, db_path: str = DB_PATH) -> list:
    """Most recent comparisons, newest first -- powers the dashboard's
    trend chart and recent-activity table."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT ts, loan_amount, interest_rate, legacy_result, shadow_result, diff, status, detail "
            "FROM shadow_results ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
    return rows
