"""Persistence for the AI migration pipeline (migrate.py) -- SQLite by
default, PostgreSQL via DATABASE_URL (see db_backend.py).

Tracks a registry of legacy COBOL programs and the Actor/Critic iteration
history for each one, so migration progress can be queried after the fact
-- "how many programs are done vs still pending" -- across separate
migrate.py invocations, not just within a single run. Powers the
migration progress dashboard (GET /migration-dashboard in main.py).
"""

import os
import time
import uuid
from typing import Optional

import db_backend

# Same SHADOW_DB_DIR convention as shadow_store.py, so both databases live
# alongside each other (or in the same mounted volume in Docker). Ignored
# when DATABASE_URL is set.
DB_DIR = os.environ.get("SHADOW_DB_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "migration_results.db")

PH = db_backend.placeholder()

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS programs (
    id {db_backend.autoincrement_pk()},
    name TEXT UNIQUE NOT NULL,
    cobol_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    registered_at REAL NOT NULL,
    last_run_at REAL
);

CREATE TABLE IF NOT EXISTS iterations (
    id {db_backend.autoincrement_pk()},
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

-- Append-only. No function in this module ever UPDATEs or DELETEs a row
-- here -- see record_audit_entry()'s docstring for what "append-only"
-- does and doesn't guarantee in plain SQLite/Postgres without extra
-- access controls.
CREATE TABLE IF NOT EXISTS audit_log (
    id {db_backend.autoincrement_pk()},
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    program TEXT,
    run_id TEXT,
    detail TEXT
);
"""

# Status values a program can be in. "awaiting_approval" (Critic approved
# empirically, but no human has signed off yet) and "approved" (a named
# human did) are deliberately distinct -- see approve_program().
STATUSES = ("pending", "in_progress", "awaiting_approval", "approved", "rejected", "failed")


def init_db(db_path: str = DB_PATH) -> None:
    if not db_backend.is_postgres():
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = db_backend.connect(db_path)
    try:
        if not db_backend.is_postgres():
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def register_program(name: str, cobol_path: str, db_path: str = DB_PATH) -> int:
    """Idempotent: inserts as 'pending' if new, otherwise a no-op. Returns the program id either way."""
    conn = db_backend.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO programs (name, cobol_path, status, registered_at) "
            f"VALUES ({PH}, {PH}, 'pending', {PH}) ON CONFLICT(name) DO NOTHING",
            (name, cobol_path, time.time()),
        )
        conn.commit()
        row = conn.execute(f"SELECT id FROM programs WHERE name = {PH}", (name,)).fetchone()
        return row[0]
    finally:
        conn.close()


def start_run(program_id: int, db_path: str = DB_PATH) -> str:
    """Marks the program in_progress for the duration of a migrate.py run; returns a fresh run_id."""
    run_id = uuid.uuid4().hex[:12]
    conn = db_backend.connect(db_path)
    try:
        conn.execute(
            f"UPDATE programs SET status = 'in_progress', last_run_at = {PH} WHERE id = {PH}",
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
    conn = db_backend.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO iterations "
            "(program_id, run_id, iteration_number, match_rate_pct, total_cases, failure_count, verdict, feedback, ts) "
            f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
            (program_id, run_id, iteration_number, match_rate_pct, total_cases, failure_count, verdict, feedback, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def finish_run(program_id: int, approved: bool, db_path: str = DB_PATH) -> None:
    """approved=True means the Critic approved at the configured match
    threshold -- NOT that the migration is cleared for production. That
    requires a named human to call approve_program() below, so the status
    here is 'awaiting_approval', not 'approved'."""
    conn = db_backend.connect(db_path)
    try:
        conn.execute(
            f"UPDATE programs SET status = {PH}, last_run_at = {PH} WHERE id = {PH}",
            ("awaiting_approval" if approved else "failed", time.time(), program_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_audit_entry(
    actor: str, action: str, program: Optional[str] = None,
    run_id: Optional[str] = None, detail: str = "", db_path: str = DB_PATH,
) -> None:
    """Append-only within this module's own API surface: nothing here
    ever issues an UPDATE or DELETE against audit_log. That's a real but
    partial guarantee -- it stops this codebase from silently editing
    history, but anyone with direct database access still could. A
    production deployment wanting genuine tamper-evidence should ship
    this to an external, access-controlled log store (a SIEM, an
    append-only object store, or a hash-chained log) rather than relying
    on this table alone -- see SHADOW_RUNNER.md's enterprise section.
    """
    conn = db_backend.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, program, run_id, detail) "
            f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
            (time.time(), actor, action, program, run_id, detail),
        )
        conn.commit()
    finally:
        conn.close()


def get_audit_log(limit: int = 200, program: Optional[str] = None, db_path: str = DB_PATH) -> list:
    conn = db_backend.connect(db_path)
    try:
        if program:
            return conn.fetchall_dicts(
                f"SELECT * FROM audit_log WHERE program = {PH} ORDER BY id DESC LIMIT {PH}", (program, limit)
            )
        return conn.fetchall_dicts(f"SELECT * FROM audit_log ORDER BY id DESC LIMIT {PH}", (limit,))
    finally:
        conn.close()


def _latest_run_id(conn, program_id: int) -> Optional[str]:
    row = conn.execute(
        f"SELECT run_id FROM iterations WHERE program_id = {PH} ORDER BY ts DESC LIMIT 1", (program_id,)
    ).fetchone()
    return row[0] if row else None


def approve_program(name: str, approver: str, comment: str = "", db_path: str = DB_PATH) -> dict:
    """A named human approves a program currently 'awaiting_approval'.
    Raises ValueError if the program doesn't exist or isn't in that
    state -- approval is only meaningful right after the Critic's own
    empirical sign-off, not at an arbitrary point."""
    conn = db_backend.connect(db_path)
    try:
        row = conn.execute(f"SELECT id, status FROM programs WHERE name = {PH}", (name,)).fetchone()
        if not row:
            raise ValueError(f"No program registered named '{name}'")
        program_id, status = row
        if status != "awaiting_approval":
            raise ValueError(f"'{name}' is '{status}', not 'awaiting_approval' -- nothing to approve")
        run_id = _latest_run_id(conn, program_id)
        conn.execute(f"UPDATE programs SET status = 'approved' WHERE id = {PH}", (program_id,))
        conn.commit()
    finally:
        conn.close()
    record_audit_entry(approver, "migration_approved", program=name, run_id=run_id, detail=comment, db_path=db_path)
    return {"program": name, "status": "approved", "approver": approver, "run_id": run_id}


def reject_program(name: str, approver: str, comment: str = "", db_path: str = DB_PATH) -> dict:
    """A named human rejects a program currently 'awaiting_approval' --
    a distinct terminal state from 'failed' (which means the AI pipeline
    itself never converged): 'rejected' means a human reviewed passing
    empirical evidence and still said no."""
    conn = db_backend.connect(db_path)
    try:
        row = conn.execute(f"SELECT id, status FROM programs WHERE name = {PH}", (name,)).fetchone()
        if not row:
            raise ValueError(f"No program registered named '{name}'")
        program_id, status = row
        if status != "awaiting_approval":
            raise ValueError(f"'{name}' is '{status}', not 'awaiting_approval' -- nothing to reject")
        run_id = _latest_run_id(conn, program_id)
        conn.execute(f"UPDATE programs SET status = 'rejected' WHERE id = {PH}", (program_id,))
        conn.commit()
    finally:
        conn.close()
    record_audit_entry(approver, "migration_rejected", program=name, run_id=run_id, detail=comment, db_path=db_path)
    return {"program": name, "status": "rejected", "approver": approver, "run_id": run_id}


def get_stats(db_path: str = DB_PATH) -> dict:
    """Aggregate progress across every registered program, plus the
    program list itself (each with its latest-run iteration count and
    match rate) for the dashboard table."""
    conn = db_backend.connect(db_path)
    try:
        programs = conn.fetchall_dicts("SELECT * FROM programs ORDER BY registered_at")

        status_counts: dict = {}
        approved_iteration_counts = []

        for p in programs:
            status_counts[p["status"]] = status_counts.get(p["status"], 0) + 1

            latest_run = conn.execute(
                f"SELECT run_id FROM iterations WHERE program_id = {PH} ORDER BY ts DESC LIMIT 1",
                (p["id"],),
            ).fetchone()
            if latest_run:
                run_row = conn.execute(
                    "SELECT iteration_number, match_rate_pct, verdict FROM iterations "
                    f"WHERE program_id = {PH} AND run_id = {PH} ORDER BY iteration_number DESC LIMIT 1",
                    (p["id"], latest_run[0]),
                ).fetchone()
                p["latest_run_id"] = latest_run[0]
                p["latest_iterations"] = run_row[0] if run_row else 0
                p["latest_match_rate_pct"] = run_row[1] if run_row else None
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
        "awaiting_approval": status_counts.get("awaiting_approval", 0),
        "in_progress": status_counts.get("in_progress", 0),
        "pending": status_counts.get("pending", 0),
        "rejected": status_counts.get("rejected", 0),
        "failed": status_counts.get("failed", 0),
        "avg_iterations_to_approval": avg_iterations,
        "total_iterations_run": total_iterations,
        "programs": programs,
    }


def get_program_history(name: str, limit: int = 100, db_path: str = DB_PATH) -> list:
    """Full iteration history for one program, most recent run first."""
    conn = db_backend.connect(db_path)
    try:
        program = conn.execute(f"SELECT id FROM programs WHERE name = {PH}", (name,)).fetchone()
        if not program:
            return []
        return conn.fetchall_dicts(
            f"SELECT * FROM iterations WHERE program_id = {PH} ORDER BY ts DESC LIMIT {PH}",
            (program[0], limit),
        )
    finally:
        conn.close()
