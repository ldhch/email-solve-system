# shouhou-agent backend (Python 3.11 slim, non-root, Phase 4 M-24)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Create a non-root user to run the service.
RUN useradd --create-home --uid 10001 appuser

# Install the package first (layer-cached on dependency changes).
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Runtime data (prompts). The .env.example template lives at the repo root and
# is NOT in this build context; container env comes from compose `env_file`.
COPY docs ./docs

# Data directory is a named volume (SQLite WAL lives here).
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
