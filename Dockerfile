# Spec §10: python -m zarabot as a non-root user. SDK from the vendored wheel.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN adduser --disabled-password --gecos "" --uid 1000 zarabot \
    && mkdir -p /data/backups \
    && chown -R zarabot:zarabot /data

WORKDIR /app

COPY vendor/SHA256SUMS vendor/t_tech_investments-1.49.1-py3-none-any.whl /tmp/vendor/
COPY requirements-image.txt .

RUN cd /tmp/vendor && sha256sum -c SHA256SUMS \
    && pip install /tmp/vendor/t_tech_investments-1.49.1-py3-none-any.whl \
    && pip install -r /app/requirements-image.txt \
    && rm -rf /tmp/vendor

COPY zarabot ./zarabot
# db.migrations resolves MIGRATIONS_DIR to /app/migrations. Without this the
# container starts, finds no migrations, reports schema version 0, and then
# fails on the first query with "no such table" - a confusing runtime error
# rather than a clear startup one.
COPY migrations ./migrations

USER zarabot
CMD ["python", "-m", "zarabot"]
