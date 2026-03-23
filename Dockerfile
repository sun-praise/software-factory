FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    HOST=0.0.0.0 \
    DB_PATH=/app/data/software_factory.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY example.env ./example.env

RUN mkdir -p /app/data /app/logs

EXPOSE 8000

CMD ["sh", "-c", "python scripts/init_db.py && python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
