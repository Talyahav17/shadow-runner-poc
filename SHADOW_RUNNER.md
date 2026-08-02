# Shadow Runner: Zero-Risk COBOL → Python Migration

A working proof of concept for safely retiring legacy COBOL business logic —
validated against real production traffic, with **zero risk** to the systems
that depend on it.

---

## The Problem

Legacy COBOL systems often encode business-critical logic (interest
calculations, billing, eligibility rules) that nobody fully trusts to
rewrite — because a subtle rounding or precision bug in the rewrite could
silently corrupt financial results. Standard rewrite-and-cut-over migrations
gamble on that not happening, and only find out otherwise in production.

## The Solution: The Shadow Runner Pattern

Instead of cutting over, run the new implementation **alongside** the old
one, in the shadow, for as long as it takes to earn trust:

```
                        ┌────────────────────────────┐
   Client Request  ───▶ │      Smart Proxy (API)      │
                        └──────────────┬─────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                                      ▼
        ┌───────────────────────┐            ┌───────────────────────┐
        │   PRODUCTION PATH      │            │    SHADOW PATH         │
        │   Legacy COBOL         │            │    Modern Python       │
        │   (interest_calc.so)   │            │    (modern_logic.py)   │
        │   — synchronous —      │            │   — background task —  │
        └───────────┬───────────┘            └───────────┬───────────┘
                    │                                      │
                    ▼                                      ▼
          Returned to caller                    Compared to production
          (always, unconditionally)             result; match/mismatch
                                                 logged + persisted
```

**The caller only ever sees the COBOL result.** The Python path can crash,
lag, or disagree, and it will never affect a real request. Every comparison
is logged and persisted, so the modern path's accuracy can be proven with
data before it's ever trusted to go live.

---

## What's in This PoC

| Component | File | Role |
|---|---|---|
| Legacy systems | [`interest_calc.cbl`](interest_calc.cbl), [`late_fee_calc.cbl`](late_fee_calc.cbl) | Two genuinely different COBOL routines, compiled to native shared libraries and called directly via `ctypes` — proving the bridge generalizes, not just one hand-fitted case |
| COBOL parser | [`cobol_parser.py`](cobol_parser.py) | Derives field widths, calling order, and the entry-point symbol from a program's own source — no more hand-typed field specs per program |
| Generic COBOL proxy | [`cobol_proxy.py`](cobol_proxy.py) | ctypes bridge for any parsed program, not just one |
| Program registry | [`program_registry.py`](program_registry.py), [`programs.yaml`](programs.yaml) | Config-driven: adding a legacy program is a YAML entry, not a code change |
| Modern replacements | [`modern_logic.py`](modern_logic.py), [`late_fee_logic.py`](late_fee_logic.py) | Python reimplementations using `Decimal` arithmetic to avoid float rounding drift |
| Smart Proxy | [`main.py`](main.py) | FastAPI service implementing the Shadow Runner pattern above, RBAC-gated |
| Auth / RBAC | [`auth.py`](auth.py) | Role-scoped API keys (viewer/operator/admin) |
| Alerting | [`alerting.py`](alerting.py) | Slack-compatible webhook on shadow mismatches/errors |
| Metrics | [`metrics.py`](metrics.py) | Prometheus counters + latency histogram |
| DB backend | [`db_backend.py`](db_backend.py) | SQLite or PostgreSQL (`DATABASE_URL`), same code either way |
| Secrets helper | [`secrets_helper.py`](secrets_helper.py) | `..._FILE` convention for Vault/CSI-driver/Docker-secrets style deployments |
| Field encryption | [`field_crypto.py`](field_crypto.py) | Optional at-rest encryption + role-based masking |
| Results store | [`shadow_store.py`](shadow_store.py) | SQLite/Postgres log of every comparison, across every registered program |
| Unit tests | [`test_modern_logic.py`](test_modern_logic.py) | 17 cases: happy path, zero values, field-capacity boundaries, invalid input |
| Verification harness | [`test_e2e_shadow.py`](test_e2e_shadow.py) | Boots the real service, fires fixed + randomized + malformed traffic, proves the shadow comparator works end to end |
| Concurrency stress test | [`test_concurrency_stress.py`](test_concurrency_stress.py) | Fires hundreds of concurrent requests to prove the service holds up under real load |
| AI migration pipeline | [`migrate.py`](migrate.py), [`migration/`](migration/) | Actor/Critic loop that generates and empirically validates a candidate replacement for any registered program |
| Human approval gate | [`approve_migration.py`](approve_migration.py) | A named human must sign off before a Critic-approved candidate is "approved" |
| Migration progress store | [`migration_store.py`](migration_store.py) | SQLite/Postgres log of registered programs, iteration history, and the approval audit log |
| Kubernetes manifests | [`k8s/`](k8s/) | Deployment, Service, HPA, Secret template — verified against a real local cluster |

---

## Proof It Works

Running the verification harness (`python3 test_e2e_shadow.py`) boots the
actual service, sends 45 valid requests (5 hand-picked + 40 randomized,
spanning the full input range and varying decimal precision) plus 6
deliberately malformed requests, and reports:

```
SHADOW RUNNER E2E + FUZZ VERIFICATION SUMMARY
============================================================
  Fixed + fuzz requests sent:      45
  Malformed requests sent:         6
------------------------------------------------------------
  [SHADOW SUCCESS] markers found:  45
  [SHADOW MISMATCH] markers found: 0
  [SHADOW ERROR] markers found:    0
------------------------------------------------------------
  Persisted shadow_results.db stats: {'total': 45, 'success': 45, 'mismatch': 0, 'error': 0, 'match_rate_pct': 100.0}
  Match rate: 100.0% (45/45)

RESULT: PASS - Shadow Runner validated all outputs successfully across
45 valid requests (40 fuzzed), correctly rejected all 6 malformed inputs,
and persisted a 100.0% match rate to shadow_results.db.
```

All 6 malformed inputs (negative amounts, out-of-range values, wrong
types, missing fields) are rejected by input validation before either
engine ever runs.

**This 100% match rate was earned, not assumed** — an earlier fuzzing pass
against this same PoC caught a real bug: the modern path was computing on
the caller's raw input precision while the COBOL field silently truncates
to 2 decimal places, producing divergent results on high-precision inputs.
That's exactly the class of bug the Shadow Runner pattern exists to catch
before it reaches production — see `git log` for the fix.

---

## Live Match-Rate Endpoint

Beyond the log lines, every comparison is persisted, so match-rate is
queryable at any time — not just at demo time:

```bash
curl -H "X-API-Key: <your-key>" http://localhost:8000/shadow-stats
# {"total": 45, "success": 45, "mismatch": 0, "error": 0, "match_rate_pct": 100.0}
```

In a real rollout, this is the metric that justifies the cutover decision:
run the shadow path against live traffic for days or weeks, then promote
the modern path only once the match rate is provably high enough.

## Dashboard

`GET /dashboard` (no key required to load the page itself) serves a
client-facing view of the same data: a hero match-rate figure, a
severity-colored meter (green ≥99%, amber 95–99%, red below), KPI tiles for
total/success/mismatch/error, a cumulative-match-rate trend line with
hover detail, and a recent-activity table that doubles as the trend
chart's accessible table view. It prompts for the API key once (stored in
the browser, never embedded in the page) and auto-refreshes every 5
seconds.

## Authentication

Every endpoint except `/health` and the `/dashboard` shell requires an
`X-API-Key` header. If `SHADOW_RUNNER_API_KEY` isn't set, the service
generates a random key on startup and logs it once:

```
WARNING SHADOW_RUNNER_API_KEY not set -- generated a random key for this run. API key: <...>
```

Set the env var (e.g. via `docker compose` or your secrets manager) to pin
a fixed key across restarts instead.

---

## Running It Yourself

### Option A — Docker (recommended for client handoff)

The only prerequisite is [Docker](https://www.docker.com/products/docker-desktop/).
No Python, pip, or COBOL toolchain needs to be installed on the host — the
image compiles `interest_calc.cbl` fresh at build time and runs the whole
service in one container.

```bash
docker compose up --build
```

The generated API key is printed in the startup logs (`docker compose logs`)
— copy it, then, in another terminal:

```bash
curl -X POST localhost:8000/calculate-interest \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key from the logs>" \
  -d '{"loan_amount": 1000.00, "interest_rate": 5.00}'

curl -H "X-API-Key: <key from the logs>" localhost:8000/shadow-stats
```

Or just open [localhost:8000/dashboard](http://localhost:8000/dashboard) and
paste the key in when prompted.

Match-rate history is written to a named Docker volume (`shadow_data`), so
it survives `docker compose restart` — only `docker compose down -v` wipes
it. Stop the service with `docker compose down`.

To pin a fixed key instead of a random one each run, copy `.env.example` to
`.env` and set it there (`.env` is gitignored, so the real key never gets
committed):

```bash
cp .env.example .env
echo "SHADOW_RUNNER_API_KEY=$(openssl rand -base64 24)" > .env
docker compose up --build
```

`docker-compose.yml` already reads `SHADOW_RUNNER_API_KEY` from `.env` /
the shell environment and passes it through.

### Option B — Native (for development on this codebase)

```bash
# 1. Compile the legacy COBOL routines to shared libraries
cobc -m -o interest_calc.so interest_calc.cbl
cobc -m -o late_fee_calc.so late_fee_calc.cbl

# 2. Install pinned Python dependencies
pip3 install -r requirements.txt

# 3. Run the unit tests for the modern replacement
python3 -m pytest test_modern_logic.py -v

# 4. Run the full end-to-end + fuzz verification
python3 test_e2e_shadow.py

# 5. Or run the service directly and try it live
#    (set SHADOW_RUNNER_API_KEY, or use the random one printed on startup)
SHADOW_RUNNER_API_KEY=dev-key python3 -m uvicorn main:app --reload
curl -X POST localhost:8000/calculate-interest \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-key" \
  -d '{"loan_amount": 1000.00, "interest_rate": 5.00}'
```

---

## Scale & Concurrency

Sequential testing can prove correctness but says nothing about what
happens under real concurrent load — and this service had a genuine
concurrency bug that only showed up under one. `interest_calc.cbl`'s
`PROGRAM-ID` has no `RECURSIVE` clause, so GnuCOBOL gives it a single
process-global `WORKING-STORAGE` instance shared across every call.
FastAPI runs synchronous endpoints in a thread pool, so concurrent
requests were calling into that shared instance from multiple threads at
once — which crashed the **entire server process**, not just the
concurrent requests, the moment real concurrent traffic arrived:

```
libcob: error: recursive CALL from 'INTEREST-CALC' to 'INTEREST-CALC' which is NOT RECURSIVE
```

In a real migration you typically can't recompile or touch the legacy
binary at all, so the fix belongs on the proxy side: `main.py` now
serializes every call into the library behind a lock, trading COBOL-call
throughput for correctness — the same conservative assumption you'd make
fronting any opaque legacy system whose internal thread-safety is unknown.

[`test_concurrency_stress.py`](test_concurrency_stress.py) fires 500
concurrent requests, each with inputs unique enough that any
cross-contamination between calls is directly detectable, and confirms
the server stays up and every result is correct. Current measured
throughput on this fix: **~900–1,900 req/s** (varies by machine) with
zero errors. Run it yourself:

```bash
python3 test_concurrency_stress.py
```

---

## Continuous Integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push
and pull request: it compiles both registered COBOL routines, runs the
unit test suite, runs the full end-to-end + fuzz verification, runs the
concurrency stress test, dry-runs the AI migration pipeline mechanics,
scans pinned dependencies for known CVEs (`pip-audit`), and separately
builds and smoke-tests the Docker image — including the multi-program
generic endpoint, RBAC (a viewer-role write gets 403, reads come back
masked), and the `/metrics` endpoint. Push this repo to GitHub and it
runs automatically — no setup needed.

---

## AI Actor/Critic Migration Pipeline

[`modern_logic.py`](modern_logic.py) was written by hand for this PoC. But
the same Shadow Runner infrastructure that validates it can also be used
to *generate* it: [`migrate.py`](migrate.py) drives an AI Actor/Critic
loop, mirroring the pattern the (separate, unrelated) `secure-auditor` CLI
in this repo already uses — but grounding the Critic in real measurement
instead of just code review:

1. **Actor** (Claude) reads the COBOL source and proposes a Python
   translation.
2. The candidate is run **directly against the real compiled COBOL**
   across hundreds of fuzzed inputs — no LLM involved in this step, the
   same empirical approach as `test_e2e_shadow.py`'s fuzzing.
3. **Critic** (Claude) reviews the candidate together with that empirical
   evidence — the actual match rate and concrete failing cases — and
   either **APPROVEs** it or **REVISEs** with feedback tied to specific
   failures, which feeds back into another Actor attempt.

The loop stops once the Critic approves at or above `--match-threshold`
(default 100%) or `--max-iterations` is reached.

```bash
pip install -r requirements-migration.txt   # separate from the app's own deps
export ANTHROPIC_API_KEY=...                # set in your own shell, never entered interactively
python3 migrate.py --cobol interest_calc.cbl
```

Try it with `--dry-run` first — a stub LLM (no API calls, no cost) walks
through a full two-round loop, including a deliberate REVISE, so you can
see the mechanics before spending real API credits:

```bash
python3 migrate.py --dry-run
```

Each run writes every candidate (`migration_output/candidate_vN.py`), the
final approved code (`migration_output/approved_modern_logic.py`), and a
full iteration history (`migration_output/report.json`) — all gitignored,
since they're generated output, not source.

**Safety note**: this tool executes LLM-generated code locally via
`exec()` in order to test it — that's inherent to the tool's purpose (it
has to run to be compared against the COBOL output), so only point it at
COBOL sources you're comfortable generating and running code for.

### Migration Progress Dashboard

Every `migrate.py` run is recorded in `migration_results.db`
([`migration_store.py`](migration_store.py)) — which programs are
registered, their status (`pending` / `in_progress` / `approved` /
`failed`), and the full per-iteration match-rate history — so progress is
queryable across separate runs, not just visible in that run's terminal
output. `GET /migration-dashboard` (same API key as the Shadow Runner
dashboard) shows:

- **How many programs are done vs. still pending** — a hero "X / Y
  programs migrated" figure and a status breakdown (approved / in
  progress / pending / failed).
- **Aggregate stats** — average iterations to approval, total iterations
  run across every program.
- **Per-program match-rate trend** — pick a program from the dropdown to
  see its match rate climb iteration by iteration, with hover detail
  showing each iteration's Critic verdict and feedback.
- **A registered-programs table** — status, iteration count, latest match
  rate, last run time — doubling as the trend chart's accessible table
  view.

Today there's exactly one registered program (`interest_calc`), since
that's the only legacy COBOL source in this repo — but the registry and
dashboard are built to scale as more are migrated: point `migrate.py
--cobol <new_program.cbl>` at another legacy source and it registers
itself automatically.

---

## Security

- **Auth**: every endpoint except `/health` and the dashboard shells
  requires `X-API-Key`, checked with a timing-safe comparison
  (`secrets.compare_digest`), and scoped to a role (`auth.py` -- see
  Enterprise Readiness below). A random admin-role key is generated and
  logged once if no key is configured.
- **Container**: runs as a fixed non-root UID (1000, not just a named
  user -- see the Kubernetes section below for why that distinction
  matters); the base image is pinned by digest, not just the `3.12-slim`
  tag, for reproducible builds.
- **Dependencies**: pinned in `requirements.txt` and scanned by `pip-audit`
  in CI on every push.
- **Input validation**: every registered program's inputs are
  bounds-checked against that program's actual PIC field capacity
  (`cobol_parser.py`) before either engine ever sees them; SQL access is
  fully parameterized (no string-built queries) across both the SQLite
  and PostgreSQL backends.
- **Dashboards**: the API key lives in `sessionStorage` (cleared when the
  tab closes) rather than `localStorage`, and nothing user-controlled is
  ever written into either dashboard page unescaped.

---

## Enterprise Readiness

Everything below was added to take this from a single-program PoC toward
something a larger organization could actually run as its own migration
process. Each item was verified as described -- genuinely run and
checked, not just written and assumed to work -- except where noted.

### Multi-program architecture

The original PoC only ever worked for `interest_calc.cbl`, with every
field width hand-typed into `field_specs.py`. That's now fully generic:

- [`cobol_parser.py`](cobol_parser.py) parses a COBOL program's `LINKAGE
  SECTION` and `PROCEDURE DIVISION USING` clause directly -- field
  widths, calling order, and the entry-point symbol are all derived from
  the source, not hand-coded. It explicitly detects and rejects
  constructs this ctypes bridge can't safely handle (signed fields,
  `COMP`/`COMP-3`/binary storage, `OCCURS` tables, alphanumeric fields)
  with a clear error naming the offending field, rather than silently
  mis-encoding them.
- [`cobol_proxy.py`](cobol_proxy.py)'s `GenericCobolProxy` replaces the
  old `interest_calc`-only proxy class; [`program_registry.py`](program_registry.py)
  loads [`programs.yaml`](programs.yaml) and builds one per registered
  program.
- **Verified with a second, genuinely different program**:
  [`late_fee_calc.cbl`](late_fee_calc.cbl) (different field widths, a
  different multi-hyphen `PROGRAM-ID`) was added specifically to prove
  the parser/proxy/registry generalize rather than happening to work for
  one program's exact shape. Both programs run correctly, simultaneously,
  through the same generic code path -- confirmed via direct calls, live
  HTTP requests, the dashboard, and inside the actual Docker container.
- `POST /programs/{name}/calculate` is the new generic endpoint;
  `POST /calculate-interest` still works unchanged as a backward-compatible
  alias. Adding a new legacy program is a `programs.yaml` entry, not a
  code change.
- `migrate.py` uses the same parser, so the AI migration pipeline also
  works on any supported program, not just `interest_calc` -- verified by
  driving it against `late_fee_calc.cbl`'s real compiled binary (302/302
  match on a hand-written candidate, proving the empirical-comparison
  machinery generalizes) even without spending real API credits on it.

### Governance: a human approval gate + audit trail

`migrate.py` no longer promotes a Critic-approved candidate straight to
"approved". It stops at `awaiting_approval`, and a named human must run
[`approve_migration.py`](approve_migration.py) to actually clear it (or
reject it) with a comment -- recorded permanently via
`migration_store.record_audit_entry()` and visible in the migration
dashboard's audit log. `rejected` (a human said no) is tracked separately
from `failed` (the AI pipeline itself never converged), since those are
different findings for an audit. **Honesty note**: "append-only" here
means the application code never issues an `UPDATE`/`DELETE` against the
audit log -- it does not mean the underlying database is tamper-proof
against someone with direct DB access. A deployment wanting genuine
tamper-evidence should ship this to an external, access-controlled log
store (a SIEM, an append-only object store, or a hash-chained log).

### RBAC

Three roles -- `viewer` (read-only), `operator` (+ can trigger
calculations), `admin` (+ reserved for future admin actions) -- via
`SHADOW_RUNNER_API_KEYS="key1:admin,key2:operator,key3:viewer"`
([`auth.py`](auth.py)). A single `SHADOW_RUNNER_API_KEY` still works
exactly as before (treated as one admin-role key). Verified via real HTTP
requests across all three roles: viewer can read but a write attempt gets
403, operator can write, admin can do both -- both locally and inside the
built Docker container.

### Real alerting

[`alerting.py`](alerting.py) posts to any Slack-compatible incoming
webhook (`SHADOW_RUNNER_ALERT_WEBHOOK_URL`) on a shadow mismatch or
error, instead of only a `CRITICAL` log line no one is watching. Verified
with a real (local) HTTP receiver standing in for Slack: both the
mismatch and error paths deliver correctly, the success path correctly
sends nothing, and delivery works from inside the actual running Docker
container reaching an endpoint outside it.

### Prometheus metrics

`GET /metrics` (same viewer-role auth as other read endpoints) exposes
request counts, shadow-comparison counts, and a COBOL-call latency
histogram, all labeled by program -- match-rate and error-rate are meant
to be derived in Grafana/PromQL from the counters, not pre-computed here.
Verified with real observed latencies and counts, including inside the
Docker container.

### PostgreSQL, alongside SQLite

[`db_backend.py`](db_backend.py) is a small dual-dialect layer (not a
full ORM) that both `shadow_store.py` and `migration_store.py` go
through. SQLite remains the zero-setup default; set `DATABASE_URL` to
switch to Postgres for real production volume, since SQLite's
single-writer lock becomes a bottleneck under concurrent writes and reads
at scale. **Verified against a real PostgreSQL container**, not just
code review: both stores' full read/write paths, the complete governance
flow (register → run → approve → audit log) across separate process
invocations, and the actual FastAPI service running end-to-end with
`DATABASE_URL` pointed at it.

### Kubernetes

Plain manifests in [`k8s/`](k8s/) (no `helm` binary was available to
build a chart). **Verified against a real local cluster** (`kind`), not
just YAML syntax: the actual Docker image was deployed, the pod reached
`Running`, and a live HTTP request through the Service returned the
correct result. This caught a genuine bug -- `runAsNonRoot: true` failed
with `CreateContainerConfigError` because Kubernetes can't verify a
*named* container user is non-root, only a numeric one, which is why the
Dockerfile now pins `appuser` to a fixed UID (1000). See
[`k8s/README.md`](k8s/README.md) for exactly what was and wasn't
verified (multi-replica + Postgres together, in a real cluster, is
scaffolded but not run end-to-end here).

### Secrets management

Every secret-bearing config value (`SHADOW_RUNNER_API_KEY(S)`,
`DATABASE_URL`, `SHADOW_RUNNER_ALERT_WEBHOOK_URL`, `ANTHROPIC_API_KEY`,
`SHADOW_RUNNER_ENCRYPTION_KEY`) also accepts a `..._FILE` variant pointing
at a file path, via [`secrets_helper.py`](secrets_helper.py) -- the
convention Vault Agent injector, the Vault CSI driver, Docker/Swarm
secrets, and Kubernetes Secrets-mounted-as-files all use. Verified for
every one of these values, both as a bind-mounted file in a plain Docker
container and via direct module tests.

### Field-level encryption & role-based masking

[`field_crypto.py`](field_crypto.py) optionally encrypts the
`inputs_json` column at rest (Fernet, via `SHADOW_RUNNER_ENCRYPTION_KEY`)
-- verified by confirming the raw stored bytes contain no plaintext, that
the correct key decrypts, and that a missing or wrong key degrades a
single row to a clear error marker rather than crashing the whole read
(a real bug caught during testing and then fixed). Deliberately **not**
auto-generated like the API key: an encryption key has to stay identical
across restarts to keep old data readable, so silently rotating it would
be worse than not encrypting at all.

Separately, `viewer`-role reads of `/shadow-history` get sensitive
figures (inputs, results, diff) rounded to 2 significant figures rather
than exact values, with a `masked: true` flag and a visible note in the
dashboard -- `operator`/`admin` see exact figures. Verified across all
three roles via live HTTP requests and in the dashboard UI.

### LLM data residency

Some enterprises can't send code/prompts to the public Anthropic API at
all. `migrate.py --llm-backend {anthropic,bedrock,vertex}`
([`migration/llm_client.py`](migration/llm_client.py)) routes the same
Actor/Critic prompts through AWS Bedrock or Google Vertex AI instead,
using Anthropic's own drop-in-compatible client classes -- no changes
needed to the prompt code itself. **Honesty note**: verified only as far
as this environment allows -- both client classes construct successfully
and reject clearly-missing config, but no live Bedrock/Vertex API call
was made, since this environment has no AWS or GCP account to test
against. Confirm a real call succeeds against your own account before
relying on either.

---

## Why This De-Risks a Real Migration

- **Zero blast radius**: the modern code path never touches what the
  caller receives, even mid-migration.
- **Evidence, not faith**: the cutover decision is backed by a persisted
  match-rate over real traffic, not a one-time test suite.
- **Catches real bugs early**: this PoC's own fuzz testing already found
  and fixed a precision-handling divergence that unit tests alone missed.
- **No legacy rewrite risk**: the COBOL program is compiled and called
  as-is — nothing about the trusted system changes until the team is
  ready to retire it.
