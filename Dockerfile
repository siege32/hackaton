FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app app

COPY pyproject.toml README.md ./
COPY ai_engine ./ai_engine
COPY api ./api
COPY data_pipeline ./data_pipeline
COPY docs ./docs
COPY data ./data

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[api,news,ocr,redis]" \
    && mkdir -p /app/data/raw /app/data/processed \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
