FROM python:3.12-slim

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

COPY interest_calc.cbl field_specs.py modern_logic.py shadow_store.py main.py ./

RUN cobc -m -o interest_calc.so interest_calc.cbl

# Match-rate history persists here; mount a volume at /app/data to keep it
# across container restarts (see docker-compose.yml).
ENV SHADOW_DB_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
