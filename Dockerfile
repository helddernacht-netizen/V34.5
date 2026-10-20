FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY . .
RUN ls -la && echo "--- core ---" && ls -la core/ && echo "--- templates ---" && ls -la templates/ && wc -l app_v34.py
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PORT=10000
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD curl -f http://localhost:$PORT/api/v34/health || exit 1
CMD uvicorn app_v34:app --host 0.0.0.0 --port $PORT
