FROM python:3.12-slim-bookworm

WORKDIR /app

RUN useradd --system --uid 1000 --create-home zarabot \
    && mkdir -p /data/backups \
    && chown -R zarabot:zarabot /data

COPY vendor/t_tech_investments-1.49.1-py3-none-any.whl /tmp/
COPY requirements.lock pyproject.toml ./
COPY zarabot/ zarabot/
COPY migrations/ migrations/

RUN pip install --no-cache-dir /tmp/t_tech_investments-1.49.1-py3-none-any.whl \
    && pip install --no-cache-dir -r requirements.lock \
    && rm -f /tmp/t_tech_investments-1.49.1-py3-none-any.whl

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    PYTHONPATH=/app

USER zarabot

CMD ["python", "-m", "zarabot"]
