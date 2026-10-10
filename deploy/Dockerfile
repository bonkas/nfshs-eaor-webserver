# NFSHS Online - production image.
# Local testing doesn't need this: use "python run.py" instead.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NFSHS_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend.py nps_client.py ./
COPY static ./static

# Run as an unprivileged user; /data holds history.jsonl + hit_counter.txt
RUN useradd --system --uid 10001 nfshs \
    && mkdir /data && chown nfshs /data
USER nfshs
VOLUME /data

EXPOSE 8123
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8123/health', timeout=3)"

# --forwarded-allow-ips: trust X-Forwarded-For from the reverse proxy
# (Nginx Proxy Manager) so logs show real visitor IPs, not the proxy's.
CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8123", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
