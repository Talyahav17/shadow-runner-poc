"""End-to-end verification of the Shadow Runner service.

Launches the FastAPI app (main:app) via uvicorn in a background process,
fires 5 test payloads at /calculate-interest, inspects the captured server
logs for "[SHADOW SUCCESS]" markers, and shuts the server down.

Exit code 0 on success, 1 on failure. Run directly with:
    python3 test_e2e_shadow.py
"""

import os
import subprocess
import sys
import tempfile
import time

import requests

HOST = "127.0.0.1"
PORT = 8199
BASE_URL = f"http://{HOST}:{PORT}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 5 payloads: a plain passing case, zero values, a typical case, and two
# that probe the decimal boundaries of the COBOL PIC 9(7)V99 / PIC 9(2)V99
# fields (max representable loan+rate, and a tiny loan against the max rate
# to exercise sub-cent rounding).
TEST_PAYLOADS = [
    {"name": "basic_pass", "loan_amount": 1000.00, "interest_rate": 5.00},
    {"name": "zero_values", "loan_amount": 0.00, "interest_rate": 0.00},
    {"name": "typical_case", "loan_amount": 250000.00, "interest_rate": 3.75},
    {"name": "max_field_boundary", "loan_amount": 9999999.99, "interest_rate": 99.99},
    {"name": "sub_cent_rounding_boundary", "loan_amount": 0.01, "interest_rate": 99.99},
]


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

    print(f"Launching uvicorn server on {BASE_URL} ...")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", HOST, "--port", str(PORT)],
        cwd=SCRIPT_DIR,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    results = []
    try:
        if not wait_for_server():
            print("FAILED: server did not become healthy in time.")
            return 1
        print("Server is up. Sending 5 test payloads...\n")

        for payload in TEST_PAYLOADS:
            body = {"loan_amount": payload["loan_amount"], "interest_rate": payload["interest_rate"]}
            resp = requests.post(f"{BASE_URL}/calculate-interest", json=body, timeout=5)
            ok = resp.status_code == 200
            result_value = resp.json().get("result") if ok else None
            results.append({"name": payload["name"], "request": body, "status": resp.status_code, "result": result_value})
            print(f"  [{payload['name']}] -> HTTP {resp.status_code}, result={result_value}")

        # Give the BackgroundTasks (shadow comparisons) time to run and flush logs.
        time.sleep(2)

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

    print("\n" + "=" * 60)
    print("SHADOW RUNNER E2E VERIFICATION SUMMARY")
    print("=" * 60)
    for r in results:
        print(f"  {r['name']:<30} HTTP {r['status']}  result={r['result']}")
    print("-" * 60)
    print(f"  [SHADOW SUCCESS] markers found:  {shadow_success_count}")
    print(f"  [SHADOW MISMATCH] markers found: {shadow_mismatch_count}")
    print(f"  [SHADOW ERROR] markers found:    {shadow_error_count}")
    print(f"  Full server log: {log_file_path}")
    print("=" * 60)

    all_requests_ok = all(r["status"] == 200 for r in results)
    shadow_validated = shadow_success_count == len(TEST_PAYLOADS) and shadow_mismatch_count == 0 and shadow_error_count == 0

    if all_requests_ok and shadow_validated:
        print("\nRESULT: PASS - Shadow Runner validated all outputs successfully.")
        return 0
    else:
        print("\nRESULT: FAIL - see log for details.")
        print(log_contents)
        return 1


if __name__ == "__main__":
    sys.exit(main())
