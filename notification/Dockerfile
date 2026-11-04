# Build context is the repository root, so the shared messaging package can be copied in.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY notification/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/messaging ./messaging
COPY notification/app ./app
COPY notification/scripts ./scripts

EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
