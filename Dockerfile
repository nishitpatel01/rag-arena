# The RAG Arena v3 — single Cloud Run image (spec §8.1): the built SPA and the Starlette API on one origin.
# Build: gcloud builds submit --tag <region>-docker.pkg.dev/<project>/<repo>/rag-arena:<tag>   (scripts/deploy_cloudrun.sh)
# Local: docker build -t rag-arena . && docker run --rm -p 8080:8080 rag-arena      (replay mode, no credentials)
# No secrets, .env or deploy/*.env enter the image (.dockerignore); configuration comes only from env vars at deploy time.

# ---- Stage 1: build the SPA ---------------------------------------------------------------------------------------
# node 22 = the version the lockfile is built and tested with (node 20's npm 10.8.2 crashed in Cloud Build with
# "Exit handler never called!" yet exited 0, leaving no node_modules/.bin/tsc) — so verify the install explicitly.
FROM node:22-slim AS spa
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund && test -x node_modules/.bin/tsc && test -x node_modules/.bin/vite
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts frontend/tailwind.config.js frontend/postcss.config.js ./
COPY frontend/src ./src
RUN npm run build

# ---- Stage 2: Python runtime --------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    DEMO_MODE=replay \
    ALLOW_DEV_FIXTURES=0 \
    STATIC_DIR=/app/frontend/dist
WORKDIR /app

COPY backend/requirements.txt backend/requirements.lock backend/
RUN pip install --no-cache-dir -r backend/requirements.lock && python -c "import google.adk, sys; v = google.adk.__version__; sys.exit(0 if v.startswith(\"2.\") else f\"google-adk {v} != 2.x\")"

# Runtime data: golden benchmarks, recordings (replay), model verification, procurement corpus PDFs (document viewer
# and in-service ingestion read them locally).
COPY data/golden_benchmarks data/golden_benchmarks
COPY data/recordings data/recordings
COPY data/model_verification.json data/model_verification.json
COPY data/sample_docs data/sample_docs
COPY backend/app backend/app
COPY backend/pyproject.toml backend/pyproject.toml
COPY --from=spa /src/frontend/dist frontend/dist

RUN useradd --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin arena
USER 10001

EXPOSE 8080
# Cloud Run injects $PORT; uvicorn trusts the Google front end's X-Forwarded-* headers.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 10"]
