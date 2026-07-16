"""End-to-end verification of the Shadow Runner service.

Launches the FastAPI app (main:app) via uvicorn in a background process,
fires 5 fixed test payloads plus a randomized fuzz/soak batch and a batch
of malformed inputs at /calculate-interest, inspects the captured server
logs for "[SHADOW SUCCESS]" / "[SHADOW MISMATCH]" / "[SHADOW ERROR]"
markers, and shuts the server down.

Exit code 0 on success, 1 on failure. Run directly with:
    python3 test_e2e_shadow.py
"""

import os
import random
import subprocess
import sys
import tempfile
import time

import requests

HOST = "127.0.0.1"
PORT = 8199
BASE_URL = f"http://{HOST}:{PORT}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TEST_API_KEY = "e2e-test-key-do-not-use-in-prod"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}

FUZZ_SEED = 42
FUZZ_ITERATIONS = 40

# COBOL PIC 9(7)V99 / PIC 9(2)V99 capacity limits (see main.py).
MAX_LOAN_AMOUNT = 9999999.99
MAX_INTEREST_RATE = 99.99

# 5 fixed payloads: a plain passing case, zero values, a typical case, and
# two that probe the decimal boundaries of the COBOL PIC 9(7)V99 / PIC
# 9(2)V99 fields (max representable loan+rate, and a tiny loan against the
# max rate to exercise sub-cent rounding).
TEST_PAYLOADS = [
    {"name": "basic_pass", "loan_amount": 1000.00, "interest_rate": 5.00},
    {"name": "zero_values", "loan_amount": 0.00, "interest_rate": 0.00},
    {"name": "typical_case", "loan_amount": 250000.00, "interest_rate": 3.75},
    {"name": "max_field_boundary", "loan_amount": 9999999.99, "interest_rate": 99.99},
    {"name": "sub_cent_rounding_boundary", "loan_amount": 0.01, "interest_rate": 99.99},
]

# Inputs the API must reject (4xx) before either calculation path ever runs.
MALFORMED_PAYLOADS = [
    {"name": "negative_loan", "loan_amount": -100.00, "interest_rate": 5.00},
    {"name": "negative_rate", "loan_amount": 1000.00, "interest_rate": -5.00},
    {"name": "loan_over_capacity", "loan_amount": 10000000.00, "interest_rate": 5.00},
    {"name": "rate_over_capacity", "loan_amount": 1000.00, "interest_rate": 100.00},
    {"name": "non_numeric_loan", "loan_amount": "not-a-number", "interest_rate": 5.00},
    {"name": "missing_rate_field", "loan_amount": 1000.00},
]


def generate_fuzz_payloads(count: int, seed: int) -> list:
    """Random *valid* inputs spanning the full field range and varying
    decimal precision (1-6 fractional digits), to probe for any rounding
    divergence between the COBOL fixed-width encoding and the Python
    Decimal path that isn't caught by the 5 fixed test cases."""
    rng = random.Random(seed)
    payloads = []
    for i in range(count):
        precision = rng.randint(1, 6)
        loan = round(rng.uniform(0, MAX_LOAN_AMOUNT), precision)
        rate = round(rng.uniform(0, MAX_INTEREST_RATE), precision)
        payloads.append({"name": f"fuzz_{i:02d}_p{precision}", "loan_amount": loan, "interest_rate": rate})
    return payloads


def wait_for_server(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"{BASE_URL}/health", timeout=1)
            if resp.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    log_file_path = os.path.join(tempfile.gettempdir(), "shadow_runner_e2e.log")
    log_file = open(log_file_path, "w")

    # Start from a clean persisted-results store so the /shadow-stats counts
    # below correspond exactly to this run's requests.
    db_path = os.path.join(SCRIPT_DIR, "shadow_results.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    print(f"Launching uvicorn server on {BASE_URL} ...")
    env = {**os.environ, "SHADOW_RUNNER_API_KEY": TEST_API_KEY}
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", HOST, "--port", str(PORT)],
        cwd=SCRIPT_DIR,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )

    results = []
    malformed_results = []
    shadow_stats = None
    try:
        if not wait_for_server():
            print("FAILED: server did not become healthy in time.")
            return 1
        print("Server is up. Sending 5 fixed test payloads...\n")

        for payload in TEST_PAYLOADS:
            body = {"loan_amount": payload["loan_amount"], "interest_rate": payload["interest_rate"]}
            resp = requests.post(f"{BASE_URL}/calculate-interest", json=body, headers=AUTH_HEADERS, timeout=5)
            ok = resp.status_code == 200
            result_value = resp.json().get("result") if ok else None
            results.append({"name": payload["name"], "request": body, "status": resp.status_code, "result": result_value})
            print(f"  [{payload['name']}] -> HTTP {resp.status_code}, result={result_value}")

        fuzz_payloads = generate_fuzz_payloads(FUZZ_ITERATIONS, FUZZ_SEED)
        print(f"\nFuzzing with {len(fuzz_payloads)} randomized valid payloads (seed={FUZZ_SEED})...")
        for payload in fuzz_payloads:
            body = {"loan_amount": payload["loan_amount"], "interest_rate": payload["interest_rate"]}
            resp = requests.post(f"{BASE_URL}/calculate-interest", json=body, headers=AUTH_HEADERS, timeout=5)
            ok = resp.status_code == 200
            result_value = resp.json().get("result") if ok else None
            results.append({"name": payload["name"], "request": body, "status": resp.status_code, "result": result_value})
        print(f"  sent {len(fuzz_payloads)} fuzz payloads")

        print(f"\nProbing {len(MALFORMED_PAYLOADS)} malformed payloads (must be rejected before reaching either engine)...")
        for payload in MALFORMED_PAYLOADS:
            body = {k: v for k, v in payload.items() if k != "name"}
            resp = requests.post(f"{BASE_URL}/calculate-interest", json=body, headers=AUTH_HEADERS, timeout=5)
            rejected = 400 <= resp.status_code < 500
            malformed_results.append({"name": payload["name"], "status": resp.status_code, "rejected": rejected})
            print(f"  [{payload['name']}] -> HTTP {resp.status_code} ({'rejected as expected' if rejected else 'NOT REJECTED'})")

        print("\nProbing auth: request with no API key and request with wrong API key must both be rejected...")
        no_key_resp = requests.post(f"{BASE_URL}/calculate-interest", json={"loan_amount": 100.0, "interest_rate": 5.0}, timeout=5)
        wrong_key_resp = requests.post(
            f"{BASE_URL}/calculate-interest", json={"loan_amount": 100.0, "interest_rate": 5.0},
            headers={"X-API-Key": "wrong-key"}, timeout=5,
        )
        auth_rejected = no_key_resp.status_code == 401 and wrong_key_resp.status_code == 401
        print(f"  [no_api_key] -> HTTP {no_key_resp.status_code}")
        print(f"  [wrong_api_key] -> HTTP {wrong_key_resp.status_code}")

        # Give the BackgroundTasks (shadow comparisons) time to run, flush logs,
        # and persist to shadow_results.db.
        time.sleep(3)

        stats_resp = requests.get(f"{BASE_URL}/shadow-stats", headers=AUTH_HEADERS, timeout=5)
        shadow_stats = stats_resp.json() if stats_resp.status_code == 200 else None

    finally:
        print("\nShutting down server...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait(timeout=5)
        log_file.close()

    with open(log_file_path) as f:
        log_contents = f.read()

    shadow_success_count = log_contents.count("[SHADOW SUCCESS]")
    shadow_mismatch_count = log_contents.count("[SHADOW MISMATCH]")
    shadow_error_count = log_contents.count("[SHADOW ERROR]")
    mismatch_lines = [line for line in log_contents.splitlines() if "[SHADOW MISMATCH]" in line or "[SHADOW ERROR]" in line]

    print("\n" + "=" * 60)
    print("SHADOW RUNNER E2E + FUZZ VERIFICATION SUMMARY")
    print("=" * 60)
    print(f"  Fixed + fuzz requests sent:      {len(results)}")
    print(f"  Malformed requests sent:         {len(malformed_results)}")
    print(f"  Auth correctly rejected:         {auth_rejected}")
    print("-" * 60)
    print(f"  [SHADOW SUCCESS] markers found:  {shadow_success_count}")
    print(f"  [SHADOW MISMATCH] markers found: {shadow_mismatch_count}")
    print(f"  [SHADOW ERROR] markers found:    {shadow_error_count}")
    if mismatch_lines:
        print("\n  Mismatch/error details:")
        for line in mismatch_lines:
            print(f"    {line}")
    print("-" * 60)
    if shadow_stats:
        print(f"  Persisted shadow_results.db stats: {shadow_stats}")
        print(f"  Match rate: {shadow_stats['match_rate_pct']}% "
              f"({shadow_stats['success']}/{shadow_stats['total']})")
    else:
        print("  Persisted shadow stats: UNAVAILABLE (/shadow-stats did not respond)")
    print(f"\n  Full server log: {log_file_path}")
    print("=" * 60)

    all_valid_requests_ok = all(r["status"] == 200 for r in results)
    all_malformed_rejected = all(m["rejected"] for m in malformed_results)
    shadow_validated = (
        shadow_success_count == len(results)
        and shadow_mismatch_count == 0
        and shadow_error_count == 0
    )
    persisted_matches_log = (
        shadow_stats is not None
        and shadow_stats["total"] == len(results)
        and shadow_stats["success"] == len(results)
        and shadow_stats["mismatch"] == 0
        and shadow_stats["error"] == 0
    )

    if all_valid_requests_ok and all_malformed_rejected and shadow_validated and persisted_matches_log and auth_rejected:
        print("\nRESULT: PASS - Shadow Runner validated all outputs successfully "
              f"across {len(results)} valid requests ({FUZZ_ITERATIONS} fuzzed), "
              f"correctly rejected all {len(malformed_results)} malformed inputs, "
              f"correctly rejected unauthenticated/wrong-key requests, "
              f"and persisted a {shadow_stats['match_rate_pct']}% match rate to shadow_results.db.")
        return 0
    else:
        print("\nRESULT: FAIL - see details above / log below.")
        if not all_valid_requests_ok:
            print("  -> one or more valid requests did not return HTTP 200")
        if not all_malformed_rejected:
            print("  -> one or more malformed requests were NOT rejected")
        if not shadow_validated:
            print("  -> shadow comparator reported a mismatch or error")
        if not persisted_matches_log:
            print("  -> persisted shadow_results.db stats did not match expected counts")
        if not auth_rejected:
            print("  -> unauthenticated or wrong-key request was NOT rejected with 401")
        print(log_contents)
        return 1


if __name__ == "__main__":
    sys.exit(main())
