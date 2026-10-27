# Lumen production image
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    LUMEN_DB=/data/lumen.db
RUN useradd --create-home --uid 10001 lumen && mkdir -p /data && chown lumen /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" pydantic
COPY lumen ./lumen
COPY static ./static
COPY sdk ./sdk
COPY tools ./tools
USER lumen
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/', timeout=4)"
# One worker: SQLite + in-process live events. Scale up later with Postgres.
CMD ["uvicorn", "lumen.app:app", "--host", "0.0.0.0", "--port", "8100", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
