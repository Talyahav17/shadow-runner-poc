"""SQLite persistence for the AI migration pipeline (migrate.py).

Tracks a registry of legacy COBOL programs and the Actor/Critic iteration
history for each one, so migration progress can be queried after the fact
-- "how many programs are done vs still pending" -- across separate
migrate.py invocations, not just within a single run. Powers the
migration progress dashboard (GET /migration-dashboard in main.py).
"""

import os
import sqlite3
import time
import uuid
from typing import Optional

# Same SHADOW_DB_DIR convention as shadow_store.py, so both databases live
# alongside each other (or in the same mounted volume in Docker).
DB_DIR = os.environ.get("SHADOW_DB_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "migration_results.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    cobol_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    registered_at REAL NOT NULL,
    last_run_at REAL
);

CREATE TABLE IF NOT EXISTS iterations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id INTEGER NOT NULL REFERENCES programs(id),
    run_id TEXT NOT NULL,
    iteration_number INTEGER NOT NULL,
    match_rate_pct REAL,
    total_cases INTEGER,
    failure_count INTEGER,
    verdict TEXT NOT NULL,
    feedback TEXT,
    ts REAL NOT NULL
);
"""


def init_db(db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def register_program(name: str, cobol_path: str, db_path: str = DB_PATH) -> int:
    """Idempotent: inserts as 'pending' if new, otherwise a no-op. Returns the program id either way."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO programs (name, cobol_path, status, registered_at) "
            "VALUES (?, ?, 'pending', ?) ON CONFLICT(name) DO NOTHING",
            (name, cobol_path, time.time()),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM programs WHERE name = ?", (name,)).fetchone()
        return row[0]
    finally:
        conn.close()


def start_run(program_id: int, db_path: str = DB_PATH) -> str:
    """Marks the program in_progress for the duration of a migrate.py run; returns a fresh run_id."""
    run_id = uuid.uuid4().hex[:12]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE programs SET status = 'in_progress', last_run_at = ? WHERE id = ?",
            (time.time(), program_id),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def record_iteration(
    program_id: int,
    run_id: str,
    iteration_number: int,
    match_rate_pct: Optional[float],
    total_cases: Optional[int],
    failure_count: Optional[int],
    verdict: str,
    feedback: str,
    db_path: str = DB_PATH,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO iterations "
            "(program_id, run_id, iteration_number, match_rate_pct, total_cases, failure_count, verdict, feedback, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (program_id, run_id, iteration_number, match_rate_pct, total_cases, failure_count, verdict, feedback, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def finish_run(program_id: int, approved: bool, db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE programs SET status = ?, last_run_at = ? WHERE id = ?",
            ("approved" if approved else "failed", time.time(), program_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_stats(db_path: str = DB_PATH) -> dict:
    """Aggregate progress across every registered program, plus the
    program list itself (each with its latest-run iteration count and
    match rate) for the dashboard table."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        programs = [dict(r) for r in conn.execute(
            "SELECT * FROM programs ORDER BY registered_at"
        ).fetchall()]

        status_counts: dict = {}
        approved_iteration_counts = []

        for p in programs:
            status_counts[p["status"]] = status_counts.get(p["status"], 0) + 1

            latest_run = conn.execute(
                "SELECT run_id FROM iterations WHERE program_id = ? ORDER BY ts DESC LIMIT 1",
                (p["id"],),
            ).fetchone()
            if latest_run:
                run_rows = conn.execute(
                    "SELECT iteration_number, match_rate_pct, verdict FROM iterations "
                    "WHERE program_id = ? AND run_id = ? ORDER BY iteration_number DESC LIMIT 1",
                    (p["id"], latest_run["run_id"]),
                ).fetchone()
                p["latest_run_id"] = latest_run["run_id"]
                p["latest_iterations"] = run_rows["iteration_number"] if run_rows else 0
                p["latest_match_rate_pct"] = run_rows["match_rate_pct"] if run_rows else None
                if p["status"] == "approved":
                    approved_iteration_counts.append(p["latest_iterations"])
            else:
                p["latest_run_id"] = None
                p["latest_iterations"] = 0
                p["latest_match_rate_pct"] = None

        total_iterations = conn.execute("SELECT COUNT(*) FROM iterations").fetchone()[0]
    finally:
        conn.close()

    avg_iterations = (
        round(sum(approved_iteration_counts) / len(approved_iteration_counts), 2)
        if approved_iteration_counts else None
    )

    return {
        "total_programs": len(programs),
        "approved": status_counts.get("approved", 0),
        "in_progress": status_counts.get("in_progress", 0),
        "pending": status_counts.get("pending", 0),
        "failed": status_counts.get("failed", 0),
        "avg_iterations_to_approval": avg_iterations,
        "total_iterations_run": total_iterations,
        "programs": programs,
    }


def get_program_history(name: str, limit: int = 100, db_path: str = DB_PATH) -> list:
    """Full iteration history for one program, most recent run first."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        program = conn.execute("SELECT id FROM programs WHERE name = ?", (name,)).fetchone()
        if not program:
            return []
        rows = conn.execute(
            "SELECT * FROM iterations WHERE program_id = ? ORDER BY ts DESC LIMIT ?",
            (program["id"], limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
