FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY main.py README.md LICENSE NOTICE ./
COPY upi_link ./upi_link
COPY static ./static

RUN mkdir -p /app/runtime/qr /app/runtime/cache \
    && chown -R app:app /app

USER app

EXPOSE 15336

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:15336/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "15336"]

