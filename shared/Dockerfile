# Image for the shared messaging tools: topology-init and DLQ replay.
# Build context: repository root.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
RUN pip install --no-cache-dir "aio-pika>=9.4,<11"
COPY shared/messaging ./messaging

CMD ["python", "-m", "messaging.topology"]
