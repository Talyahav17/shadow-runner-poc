"""Persistence for Shadow Runner comparison results (SQLite by default,
PostgreSQL via DATABASE_URL -- see db_backend.py).

Every shadow comparison (legacy COBOL vs modern Python) is recorded here
so the match-rate can be queried after the fact -- e.g. "what % of real
traffic would the modern path get right" -- instead of only being visible
as scrolled-past log lines.

Schema is program-agnostic (a `program` name + a JSON blob of whatever
input fields that program takes) so results from multiple registered
COBOL programs (see program_registry.py) can coexist in one table.
"""

import json
import os
import time
from typing import Optional

import db_backend
import field_crypto

# SHADOW_DB_DIR lets a deployment (e.g. the Docker image) point this at a
# mounted volume so match-rate history survives a container restart; local
# dev defaults to alongside this file, unchanged. Ignored entirely when
# DATABASE_URL is set (Postgres connects by URL, not by file path).
DB_DIR = os.environ.get("SHADOW_DB_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "shadow_results.db")

PH = db_backend.placeholder()

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS shadow_results (
    id {db_backend.autoincrement_pk()},
    ts REAL NOT NULL,
    program TEXT NOT NULL DEFAULT 'interest_calc',
    inputs_json TEXT NOT NULL DEFAULT '{{}}',
    legacy_result REAL,
    shadow_result REAL,
    diff REAL,
    status TEXT NOT NULL,
    detail TEXT
)
"""


def init_db(db_path: str = DB_PATH) -> None:
    if not db_backend.is_postgres():
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = db_backend.connect(db_path)
    try:
        if not db_backend.is_postgres():
            # WAL mode is a persistent property of the db file (set once
            # here, not per-connection): it lets shadow-comparison writes
            # and dashboard reads proceed concurrently instead of
            # blocking on SQLite's default single-writer lock. Not
            # applicable to Postgres, which has real MVCC concurrency.
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)

        if not db_backend.is_postgres():
            # Migrate pre-multi-program SQLite databases (fixed
            # loan_amount / interest_rate columns, no program /
            # inputs_json) forward, so an existing deployment's history
            # isn't lost by this schema change. Only relevant to existing
            # SQLite deployments -- a fresh Postgres database always
            # starts on the current schema already.
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(shadow_results)")}
            if "program" not in existing_cols:
                conn.execute("ALTER TABLE shadow_results ADD COLUMN program TEXT NOT NULL DEFAULT 'interest_calc'")
            if "inputs_json" not in existing_cols:
                conn.execute("ALTER TABLE shadow_results ADD COLUMN inputs_json TEXT NOT NULL DEFAULT '{}'")
                if "loan_amount" in existing_cols and "interest_rate" in existing_cols:
                    rows = conn.execute(
                        "SELECT id, loan_amount, interest_rate FROM shadow_results WHERE inputs_json = '{}'"
                    ).fetchall()
                    for row_id, loan, rate in rows:
                        conn.execute(
                            f"UPDATE shadow_results SET inputs_json = {PH} WHERE id = {PH}",
                            (field_crypto.encrypt(json.dumps({"loan_amount": loan, "interest_rate": rate})), row_id),
                        )
        conn.commit()
    finally:
        conn.close()


def record_result(
    program: str,
    inputs: dict,
    legacy_result: Optional[float],
    shadow_result: Optional[float],
    diff: Optional[float],
    status: str,
    detail: str = "",
    db_path: str = DB_PATH,
) -> None:
    """status is one of 'success', 'mismatch', 'error'."""
    conn = db_backend.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO shadow_results "
            "(ts, program, inputs_json, legacy_result, shadow_result, diff, status, detail) "
            f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
            (time.time(), program, field_crypto.encrypt(json.dumps(inputs)), legacy_result, shadow_result, diff, status, detail),
        )
        conn.commit()
    finally:
        conn.close()


def get_match_stats(program: Optional[str] = None, db_path: str = DB_PATH) -> dict:
    """Aggregate match-rate, optionally filtered to one program (default:
    across every program, matching the original single-program behavior)."""
    conn = db_backend.connect(db_path)
    try:
        if program:
            cur = conn.execute(
                f"SELECT status, COUNT(*) FROM shadow_results WHERE program = {PH} GROUP BY status", (program,)
            )
        else:
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


def get_recent_results(limit: int = 50, program: Optional[str] = None, db_path: str = DB_PATH) -> list:
    """Most recent comparisons, newest first -- powers the dashboard's
    trend chart and recent-activity table. Each row's `inputs` is the
    parsed dict of that program's input fields (e.g. {"loan_amount": ...,
    "interest_rate": ...} for interest_calc, {"balance": ..., ...} for
    another program)."""
    conn = db_backend.connect(db_path)
    try:
        cols = "ts, program, inputs_json, legacy_result, shadow_result, diff, status, detail"
        if program:
            rows = conn.fetchall_dicts(
                f"SELECT {cols} FROM shadow_results WHERE program = {PH} ORDER BY id DESC LIMIT {PH}",
                (program, limit),
            )
        else:
            rows = conn.fetchall_dicts(
                f"SELECT {cols} FROM shadow_results ORDER BY id DESC LIMIT {PH}", (limit,)
            )
        for row in rows:
            decrypted = field_crypto.decrypt(row.pop("inputs_json"))
            try:
                row["inputs"] = json.loads(decrypted)
            except (json.JSONDecodeError, ValueError):
                # Still ciphertext -- this process has no key configured
                # (or the wrong one) for a row that was actually
                # encrypted. Degrade this one row, don't crash the whole
                # read for every other row alongside it.
                row["inputs"] = {"_error": "cannot decode: missing or incorrect SHADOW_RUNNER_ENCRYPTION_KEY"}
    finally:
        conn.close()
    return rows
