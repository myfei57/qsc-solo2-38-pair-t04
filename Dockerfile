FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KILNLINE_DATA_DIR=/var/lib/kilnline

WORKDIR /app

COPY pyproject.toml ./
COPY kilnline ./kilnline
COPY tests ./tests

RUN mkdir -p "$KILNLINE_DATA_DIR"

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["python", "-m", "kilnline", "serve", "--host", "0.0.0.0", "--port", "8080"]
