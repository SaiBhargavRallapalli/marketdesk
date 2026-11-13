# MarketDesk — container image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata

WORKDIR /app

# curl is only used for the healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# application code (data/ + reports/ are created at runtime, volume-mounted)
COPY marketdesk/ ./marketdesk/
COPY app.py config.yaml ./
COPY .streamlit/ .streamlit/
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN mkdir -p /app/data /app/reports /app/docker \
    && sed -i 's/\r$//' /app/docker/entrypoint.sh \
    && chmod +x /app/docker/entrypoint.sh

EXPOSE 8501

# Streamlit's built-in health endpoint
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["/bin/sh", "/app/docker/entrypoint.sh"]
