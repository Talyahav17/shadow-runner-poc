# Kubernetes manifests

Plain YAML, not a Helm chart (no `helm` binary was available while
building this) — apply directly or wrap in Kustomize/Helm yourself.

## Two deployment shapes

**Multi-replica (recommended, `deployment.yaml`'s default: `replicas: 2`)**
requires the Postgres backend (`DATABASE_URL` in the Secret). SQLite's
file-based storage has no safe way to be shared for concurrent writes
across separate pods — that's a different problem from the in-process
COBOL concurrency bug fixed earlier (`cobol_proxy.py`'s lock only
serializes calls *within* one process; it says nothing about two
separate pod processes sharing a file).

**Single-replica** can use the default SQLite instead: drop
`DATABASE_URL` from the Secret, set `replicas: 1`, and mount
`pvc.yaml.example` at `/app/data` (`SHADOW_DB_DIR` already points there).

## Files

| File | Purpose |
|---|---|
| `deployment.yaml` | The app itself — probes, resource limits, non-root security context |
| `service.yaml` | ClusterIP Service, port 80 → 8000 |
| `hpa.yaml` | CPU-based autoscaling, 2–10 replicas |
| `secret.yaml.example` | Template only — see the file for how to create the real one without committing secret values |
| `pvc.yaml.example` | Only needed for the single-replica SQLite option above |

## Apply

```bash
kubectl apply -f secret.yaml   # your real one, created via kubectl create secret (see the .example file)
kubectl apply -f deployment.yaml -f service.yaml -f hpa.yaml
```

## What's actually been verified here, and how

These manifests were validated against a **real** local cluster
(`kind`), not just YAML syntax checking:

- `kubectl apply --dry-run=server` against a live API server (catches
  schema errors client-side dry-run can miss, since no cluster was
  configured to fetch the OpenAPI schema from).
- The real Docker image was loaded into the cluster and actually
  deployed — the pod reached `Running`, the readiness probe passed, and
  a live HTTP request through the Service (`kubectl port-forward` →
  `/health` and `/calculate-interest`) returned the correct result.
- This caught a real bug: `runAsNonRoot: true` alone fails with
  `CreateContainerConfigError` because Kubernetes can't verify a
  *named* user (the Dockerfile originally just did `USER appuser`) is
  non-root — it needs a numeric UID. Fixed by pinning `appuser` to a
  fixed UID/GID (1000) in the Dockerfile and setting `runAsUser: 1000`
  here to match. This is exactly the kind of thing that only shows up
  by actually deploying, not by reading the manifest.

## Not yet verified against a real cluster

- **Postgres-backed multi-replica**: `shadow_store.py`/`migration_store.py`
  were verified against a real Postgres container directly (not through
  Kubernetes), and the K8s manifests were verified with SQLite/single-pod.
  The combination — two pods both pointed at one Postgres via
  `DATABASE_URL` in a real cluster — was not run end-to-end here.
- **HPA scaling behavior** under real load (validated for schema
  correctness only, not by generating load and watching it scale).
- **A managed Postgres** (RDS/Cloud SQL/etc.) instead of a
  locally-run one.
