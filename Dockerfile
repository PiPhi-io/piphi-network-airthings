FROM python:3.12-slim

ARG RELEASE_VERSION=0.0.0-unqualified
ARG SOURCE_REVISION=0000000000000000000000000000000000000000
ARG SOURCE_REPOSITORY=https://github.com/PiPhi-io/piphi-network-airthings
ARG MANIFEST_SHA256=unqualified
ARG BEHAVIORS_SHA256=unqualified

LABEL org.opencontainers.image.version="${RELEASE_VERSION}" \
    org.opencontainers.image.revision="${SOURCE_REVISION}" \
    org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
    io.piphi.manifest.sha256="${MANIFEST_SHA256}" \
    io.piphi.behaviors.sha256="${BEHAVIORS_SHA256}"

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
