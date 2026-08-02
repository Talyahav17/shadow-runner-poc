# Pinned by digest (not just the "3.12-slim" tag) so a rebuild months from
# now uses the exact image this Dockerfile was built and verified against,
# rather than whatever "3.12-slim" happens to point to at build time.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# GnuCOBOL provides `cobc`, used below to compile the legacy
# interest_calc.cbl into interest_calc.so at image build time -- so the
# shared library is always built fresh against this image's own libc and
# architecture, instead of shipping a pre-built binary that may not load.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gnucobol4 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY interest_calc.cbl late_fee_calc.cbl programs.yaml \
     field_specs.py modern_logic.py late_fee_logic.py \
     cobol_parser.py cobol_proxy.py program_registry.py db_backend.py \
     secrets_helper.py field_crypto.py shadow_store.py migration_store.py \
     auth.py alerting.py metrics.py main.py ./
COPY static ./static

# Compile every legacy program registered in programs.yaml. Adding a new
# program means adding it here too (or switching to a build-time loop
# over programs.yaml if the registry grows large).
RUN cobc -m -o interest_calc.so interest_calc.cbl \
    && cobc -m -o late_fee_calc.so late_fee_calc.cbl

# Match-rate history persists here; mount a volume at /app/data to keep it
# across container restarts (see docker-compose.yml).
ENV SHADOW_DB_DIR=/app/data
RUN mkdir -p /app/data

# Run as an unprivileged user rather than root (the base image's default).
# Fixed numeric UID/GID (not just a name): Kubernetes' runAsNonRoot check
# has to statically verify the image runs as non-root, which it can only
# do from a numeric UID in the image metadata -- a named user with no
# fixed UID fails that check with CreateContainerConfigError (found by
# actually deploying this image to a real cluster, not just reading the
# manifest -- see k8s/README.md).
RUN groupadd --system --gid 1000 appuser \
    && useradd --system --uid 1000 --gid appuser --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER 1000:1000

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
