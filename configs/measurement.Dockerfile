# 시험 전용 이미지. 제품 이미지와 별도로 빌드하며 ECR 배포에 사용하지 않는다.
ARG GO_IMAGE=golang:1.26-bookworm
ARG BASE_IMAGE=dangubi-simulator:git-6244df3
FROM ${GO_IMAGE} AS broker-build
WORKDIR /src
COPY services/worker/go.mod services/worker/go.sum ./
RUN go mod download
COPY services/worker/*.go ./
RUN CGO_ENABLED=0 go test -c -trimpath -o /out/operations-broker .

FROM ${BASE_IMAGE}
USER root
COPY --from=broker-build /out/operations-broker /opt/dangubi/bin/operations-broker
RUN ln -s /opt/dangubi/bin/worker /opt/dangubi/bin/operations-worker && mkdir -p /app/.local && chown 10001:10001 /app/.local
COPY scripts/measure_operations.py scripts/measure_schedule.py scripts/measure_mqtt.py scripts/measurement_process.py /app/scripts/
ENV DANGUBI_MEASURE_BIN_DIR=/opt/dangubi/bin
USER 10001:10001
CMD ["python", "scripts/measure_operations.py", "--stage", "mqtt", "--emit-json"]
