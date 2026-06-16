# App container: FastAPI server + Gradio UI + filesystem watcher
# + Confluence scheduler. Reranker model is NOT baked in — mount the
# HuggingFace cache at /hf-cache (see docs/DEPLOY.md).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/hf-cache/sentence-transformers \
    HF_HUB_OFFLINE=0 \
    PYTHONPATH=/app/src

# git: git_history tools (`git -C data/repos/<repo> log …`)
# ca-certificates + update-ca-certificates: corp TLS chain
# curl: healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Optional corp root CA (TLS-inspecting proxy). Build with:
#   docker build --build-arg CA_CERT=ops/corp-root-ca.crt -t cto-app .
# (file is COPYed only if the arg is set; harmless when empty)
ARG CA_CERT=
COPY ${CA_CERT:-pyproject.toml} /tmp/_corp_ca
RUN if [ -n "$CA_CERT" ]; then \
        cp /tmp/_corp_ca /usr/local/share/ca-certificates/corp-root.crt && \
        update-ca-certificates; \
    fi && rm -f /tmp/_corp_ca
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# Layer-cache deps separately from source.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e .

COPY src ./src
COPY scripts ./scripts
COPY Makefile ./

# data/ and /hf-cache are bind-mounted at runtime.
RUN mkdir -p data /hf-cache

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "--app-dir", "src", "rag.api.app:api", \
     "--host", "0.0.0.0", "--port", "8000"]
