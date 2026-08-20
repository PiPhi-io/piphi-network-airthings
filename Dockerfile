FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    PIPHI_AUTOMATION_LEDGER_PATH=/var/lib/piphi/automation-actions.sqlite3

WORKDIR /app

RUN groupadd --system --gid 10001 piphi \
    && useradd --system --uid 10001 --gid piphi --home-dir /nonexistent --shell /usr/sbin/nologin piphi \
    && mkdir -p /var/lib/piphi \
    && chown piphi:piphi /var/lib/piphi \
    && python -m pip install --upgrade pip

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN python -m pip install .

USER piphi

VOLUME ["/var/lib/piphi"]
EXPOSE 3669

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json, urllib.request; json.load(urllib.request.urlopen('http://127.0.0.1:3669/health', timeout=3))" || exit 1

CMD ["python", "-m", "piphi_network_airthings.app"]
