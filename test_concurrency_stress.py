"""Concurrency stress test for the Shadow Runner service.

Sequential testing (test_e2e_shadow.py) can never catch a concurrency
bug -- and this service had one: interest_calc.cbl's PROGRAM-ID has no
RECURSIVE clause, so GnuCOBOL gives it a single process-global
WORKING-STORAGE instance shared across every call. Concurrent calls from
FastAPI's threadpool used to crash the whole server outright (libcob:
"recursive CALL ... which is NOT RECURSIVE"), taking down every caller,
not just the concurrent ones. main.py now serializes calls into the
library with a lock; this script proves that fix holds and measures the
throughput it costs.

Fires N concurrent requests, each with inputs unique enough that any
cross-contamination between concurrent COBOL calls is immediately
detectable (the returned result won't match that request's own inputs).

Exit code 0 on success, 1 on failure. Run directly with:
    python3 test_concurrency_stress.py
"""

import concurrent.futures
import os
import subprocess
import sys
import tempfile
import time

import requests

HOST = "127.0.0.1"
PORT = 8299
BASE_URL = f"http://{HOST}:{PORT}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TEST_API_KEY = "concurrency-test-key-do-not-use-in-prod"
HEADERS = {"X-API-Key": TEST_API_KEY, "Content-Type": "application/json"}

N_REQUESTS = 500
N_THREADS = 50


def wait_for_server(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/health", timeout=1).status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.3)
    return False


def make_request(i: int) -> dict:
    loan = round(100 + (i * 37) % 9000, 2)
    rate = round(1 + (i * 13) % 9800 / 100, 2)
    expected = round(loan * rate / 100, 2)
    try:
        resp = requests.post(f"{BASE_URL}/calculate-interest", json={"loan_amount": loan, "interest_rate": rate}, headers=HEADERS, timeout=10)
    except requests.exceptions.RequestException as exc:
        return {"i": i, "ok": False, "reason": f"request failed: {exc}"}
    if resp.status_code != 200:
        return {"i": i, "ok": False, "reason": f"HTTP {resp.status_code}"}
    actual = resp.json()["result"]
    ok = abs(actual - expected) < 0.02
    return {"i": i, "ok": ok, "loan": loan, "rate": rate, "expected": expected, "actual": actual}


def main() -> int:
    log_file_path = os.path.join(tempfile.gettempdir(), "shadow_runner_concurrency.log")
    log_file = open(log_file_path, "w")

    print(f"Launching uvicorn server on {BASE_URL} ...")
    env = {**os.environ, "SHADOW_RUNNER_API_KEY": TEST_API_KEY}
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", HOST, "--port", str(PORT)],
        cwd=SCRIPT_DIR, stdout=log_file, stderr=subprocess.STDOUT, env=env,
    )

    results = []
    elapsed = None
    server_survived = False
    try:
        if not wait_for_server():
            print("FAILED: server did not become healthy in time.")
            return 1

        print(f"Firing {N_REQUESTS} concurrent requests across {N_THREADS} threads, "
              f"each with unique inputs so cross-contamination is directly detectable...")
        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=N_THREADS) as pool:
            futures = [pool.submit(make_request, i) for i in range(N_REQUESTS)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())
        elapsed = time.time() - start

        server_survived = requests.get(f"{BASE_URL}/health", timeout=5).status_code == 200

    finally:
        print("\nShutting down server...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait(timeout=5)
        log_file.close()

    failures = [r for r in results if not r["ok"]]
    rps = len(results) / elapsed if elapsed else 0

    print("\n" + "=" * 60)
    print("CONCURRENCY STRESS TEST SUMMARY")
    print("=" * 60)
    print(f"  Requests sent:              {len(results)}")
    print(f"  Correct results:            {len(results) - len(failures)}")
    print(f"  Wrong/failed results:       {len(failures)}")
    print(f"  Server survived the burst:  {server_survived}")
    print(f"  Elapsed:                    {elapsed:.2f}s  (~{rps:.0f} req/s)")
    if failures:
        print("\n  First 10 failures:")
        for r in failures[:10]:
            print(f"    {r}")
    print(f"\n  Full server log: {log_file_path}")
    print("=" * 60)

    if not failures and server_survived and len(results) == N_REQUESTS:
        print(f"\nRESULT: PASS - server handled {N_REQUESTS} concurrent requests "
              f"(~{rps:.0f} req/s) with zero cross-contamination and stayed up.")
        return 0
    else:
        print("\nRESULT: FAIL - see details above.")
        if not server_survived:
            print("  -> the server crashed or stopped responding under concurrent load")
        return 1


if __name__ == "__main__":
    sys.exit(main())
