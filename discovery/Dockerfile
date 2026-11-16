FROM python:3.12-slim
WORKDIR /app
COPY discovery/requirements-lock.txt /app/discovery/requirements-lock.txt
RUN pip install --no-cache-dir -r discovery/requirements-lock.txt
COPY shared /app/shared
COPY discovery /app/discovery
ENV PYTHONPATH=/app:/app/shared PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "discovery.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
