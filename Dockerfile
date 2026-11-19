# syntax=docker/dockerfile:1
ARG GO_IMAGE=golang:1.26-bookworm
ARG NODE_IMAGE=node:24-bookworm-slim
ARG PYTHON_IMAGE=python:3.14-slim-bookworm

FROM ${GO_IMAGE} AS worker-build
WORKDIR /src
COPY services/worker/go.mod services/worker/go.sum ./
RUN go mod download
COPY services/worker/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/worker .

FROM ${NODE_IMAGE} AS web-build
WORKDIR /src
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS runtime
ARG REVISION=unknown
LABEL org.opencontainers.image.title="DangUBI Simulator" \
      org.opencontainers.image.revision="${REVISION}"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DANGUBI_DATA_DIR=/data \
    DANGUBI_WORKER=/opt/dangubi/bin/worker \
    DANGUBI_MQTT_PROFILE=/run/secrets/mqtt.json
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 dangubi \
    && useradd --uid 10001 --gid 10001 --no-create-home dangubi \
    && mkdir -p /data /opt/dangubi/bin \
    && chown 10001:10001 /data
COPY services/control/requirements.lock.txt /app/services/control/requirements.lock.txt
RUN python -m pip install --no-cache-dir -r services/control/requirements.lock.txt
COPY services/control/ /app/services/control/
COPY contracts/ /app/contracts/
COPY --from=worker-build /out/worker /opt/dangubi/bin/worker
COPY --from=web-build /src/dist/ /app/apps/web/dist/
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=3)); assert r['status']=='ready' and r['worker_ready']"]
# 제어기에 먼저 종료 신호를 보내고 자식 종료/회수는 제어기와 tini가 처리한다.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "services.control.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--timeout-graceful-shutdown", "15"]
