#!/usr/bin/env python3
"""Human approval gate for the AI migration pipeline.

migrate.py stops at 'awaiting_approval' once the Critic empirically signs
off -- it never promotes a program straight to 'approved'. A named human
must run this tool to actually approve (or reject) it, which is recorded
permanently in the audit log (migration_store.get_audit_log()).

Usage:
    python3 approve_migration.py --program interest_calc --approver "Jane Doe" \\
        --comment "Reviewed 300-case empirical match at 100%, cleared for staging"

    python3 approve_migration.py --program interest_calc --approver "Jane Doe" \\
        --reject --comment "Match rate looks right but formula misses a fee cap our spec requires"
"""

import argparse
import sys

import migration_store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--program", required=True, help="Program name (as registered by migrate.py, e.g. 'interest_calc')")
    parser.add_argument("--approver", required=True, help="Your name/identity -- recorded permanently in the audit log")
    parser.add_argument("--comment", default="", help="Why you're approving or rejecting this migration")
    parser.add_argument("--reject", action="store_true", help="Reject instead of approve")
    args = parser.parse_args()

    migration_store.init_db()

    try:
        if args.reject:
            result = migration_store.reject_program(args.program, args.approver, args.comment)
        else:
            result = migration_store.approve_program(args.program, args.approver, args.comment)
    except ValueError as exc:
        print(f"Cannot record decision: {exc}", file=sys.stderr)
        return 1

    print(f"Recorded: {args.program} -> {result['status']} (by {args.approver})")
    if args.comment:
        print(f"  comment: {args.comment}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
