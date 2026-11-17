"""로컬 제어 API: 기본 dry_run, 서버 프로필과 확인을 거친 소량 MQTT 시험."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
from contextlib import asynccontextmanager, contextmanager
from services.control.jobs import JobManager, ACTIVE
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / "contracts/obd-development.json").read_text(encoding="utf-8"))
WORKER = ROOT / ".local" / ("worker.exe" if os.name == "nt" else "worker")
DB = Path(os.environ.get("DANGUBI_DB", str(ROOT / ".local/runs.sqlite3")))
MQTT_PROFILE = Path(os.environ.get("DANGUBI_MQTT_PROFILE", str(ROOT / ".local/mqtt.json")))
MQTT_LOCK = threading.Lock()
@asynccontextmanager
async def lifespan(_app):
    JOBS.recover()
    try:
        yield
    finally:
        JOBS.shutdown()


app = FastAPI(title="DangUBI 로컬 제어 API", version="0.2.0", lifespan=lifespan)


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    vehicle_count: int = Field(ge=1, le=10000)
    mapping_mode: Literal["shared", "dedicated"]
    client_count: int = Field(ge=1, le=10000)
    messages_per_vehicle: int = Field(ge=1, le=10000)
    interval_ms: int = Field(ge=1, le=86400000)
    connection_lifetime: Literal["always", "while_driving"] = "always"

    @model_validator(mode="after")
    def bounded(self):
        if self.mapping_mode == "dedicated" and self.client_count != self.vehicle_count:
            raise ValueError("차량별 전체 연결은 차량 수와 Client 수가 같아야 합니다.")
        if self.mapping_mode == "shared" and self.client_count > self.vehicle_count:
            raise ValueError("풀 Client 수는 차량 수 이하여야 합니다.")
        return self


class Generation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    profile: Literal["urban", "highway", "congested"]
    seed: int = Field(ge=-2147483648, le=2147483647)
    start_time_ms: int = Field(ge=0, le=4102444800000)
    duration_seconds: int = Field(ge=10, le=3600)
    start_lat: float = Field(ge=-85, le=85)
    start_lon: float = Field(ge=-180, le=180)
    initial_odometer_m: float = Field(ge=0, le=1e9)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    config: ConnectionConfig
    message: dict[str, Any] | None = None
    generation: Generation | None = None
    transport: Literal["dry_run", "mqtt"] = "dry_run"
    confirm_mqtt: bool = False
    realtime: bool = False

    @model_validator(mode="after")
    def generation_plan(self):
        if self.generation:
            if self.message is not None:
                raise ValueError("자동 생성 설정과 수동 메시지는 동시에 사용할 수 없습니다.")
            duration_ms = self.generation.duration_seconds * 1000
            interval = self.config.interval_ms
            if interval < 100 or interval > duration_ms // 10 or duration_ms % interval:
                raise ValueError("생성 주기는 100ms 이상이고 운행시간을 나누어 떨어지게 하며 최소 10구간을 확보해야 합니다.")
            self.config.messages_per_vehicle = duration_ms // interval + 2
        elif self.message is None:
            raise ValueError("수동 메시지 또는 자동 생성 설정이 필요합니다.")
        if self.config.vehicle_count * self.config.messages_per_vehicle > 10000:
            raise ValueError("한 실행은 Trip을 포함해 최대 10,000건입니다.")
        if self.realtime and not self.generation and (self.config.messages_per_vehicle - 1) * self.config.interval_ms > 3600000:
            raise ValueError("실제 시간 실행은 최대 3,600초입니다.")
        return self


class MQTTProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    contract_reviewed: bool = False
    broker_url: str
    ca_file: str
    cert_file: str
    key_file: str
    topic_prefix: str = "sim"
    max_clients: int = Field(default=5, ge=1, le=20)
    max_messages: int = Field(default=20, ge=1, le=100)
    max_duration_seconds: int = Field(default=30, ge=1, le=30)
    connect_per_second: int = Field(default=2, ge=1, le=5)
    publish_per_second: int = Field(default=10, ge=1, le=70)
    timeout_ms: int = Field(default=3000, ge=100, le=5000)
    reconnect_attempts: int = Field(default=2, ge=0, le=3)


def mqtt_profile():
    try:
        profile = MQTTProfile.model_validate_json(MQTT_PROFILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise HTTPException(409, detail="서버 MQTT 설정이 준비되지 않았습니다. 제품 사용 안내를 확인하세요.")
    if not profile.enabled or not profile.contract_reviewed:
        raise HTTPException(409, detail="서버 MQTT 활성화와 수신 규격·시험 토픽 확인이 필요합니다.")
    result = profile.model_dump()
    for key in ("ca_file", "cert_file", "key_file"):
        result[key] = str((ROOT / result[key]).resolve())
    return result


@app.exception_handler(RequestValidationError)
async def input_error(_request: Request, exc: RequestValidationError):
    details = [" / ".join(str(v) for v in e["loc"][1:]) + ": " + str(e.get("ctx", {}).get("error", "입력값의 형식·범위를 확인하세요.")) for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": details})


def validate_message(message: dict[str, Any]):
    if type(message.get("ty")) is not int or message["ty"] not in (3, 4):
        raise HTTPException(422, detail=["ty: 개발용 OBD 타입은 3 또는 4입니다."])
    profile = CATALOG["profiles"][str(message["ty"])]
    errors = sorted(Draft202012Validator(profile["schema"]).iter_errors(message), key=lambda e: str(e.path))
    if errors:
        descriptions = {"required": "필수 항목이 누락됐습니다.", "type": "자료형을 확인하세요.", "minimum": "허용 최솟값 미만입니다.", "maximum": "허용 최댓값을 초과했습니다.", "additionalProperties": "규격에 없는 항목이 포함됐습니다.", "pattern": "값의 표현 형식을 확인하세요.", "maxLength": "허용 길이를 초과했습니다.", "const": "허용되지 않은 값입니다."}
        raise HTTPException(422, detail=[f"{' / '.join(map(str, e.path)) or '메시지'}: {descriptions.get(e.validator, '입력값을 확인하세요.')}" for e in errors[:20]])
    payload = message["pld"]
    if message["ty"] == 3 and payload["stt"] > payload["edt"]:
        raise HTTPException(422, detail=["Trip 시작 시각은 종료 시각 이하여야 합니다."])
    if len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()) > 131072:
        raise HTTPException(422, detail=["메시지는 128KiB 이하여야 합니다."])


def prepare_worker(body: RunRequest, operation: str, run_id: str):
    if body.message is not None:
        validate_message(body.message)
    if not WORKER.is_file():
        raise HTTPException(503, detail="Worker가 준비되지 않았습니다. 로컬 준비 작업을 실행하세요.")
    data = {"operation": operation, "run_id": run_id, "config": body.config.model_dump(), "message": body.message, "realtime": body.realtime and operation != "preview"}
    if body.generation:
        data["generation"] = body.generation.model_dump()
    timeout = 30
    if operation == "mqtt":
        if not body.confirm_mqtt:
            raise HTTPException(409, detail="실제 MQTT 송신 확인을 선택하세요.")
        profile = mqtt_profile()
        if body.generation and body.generation.duration_seconds >= profile["max_duration_seconds"]:
            raise HTTPException(422, detail="자동 운행시간은 실제 송신 전체 제한시간보다 짧아야 합니다. 접속·응답 시간도 필요합니다.")
        if body.config.client_count > profile["max_clients"] or body.config.vehicle_count * body.config.messages_per_vehicle > profile["max_messages"]:
            raise HTTPException(422, detail="서버의 실제 송신 Client 수 또는 메시지 상한을 초과했습니다.")
        data["mqtt"] = profile
        timeout = profile["max_duration_seconds"] + 15
    return data, timeout


def call_worker(body: RunRequest, operation: str, run_id: str):
    data, timeout = prepare_worker(body, operation, run_id)
    try:
        process = subprocess.run([str(WORKER)], input=json.dumps(data).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        result = json.loads(process.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        raise HTTPException(502, detail="Worker 응답을 확인하지 못했습니다.")
    if process.returncode or "error" in result:
        raise HTTPException(422, detail=result.get("error", "Worker 처리에 실패했습니다."))
    return result


@contextmanager
def database():
    DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB)
    connection.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT, request TEXT, result TEXT)")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


JOBS = JobManager(database, MQTT_LOCK)


@app.get("/api/health")
def health():
    try:
        profile = mqtt_profile()
        mqtt_ready = True
        limits = {k: profile[k] for k in ("max_clients", "max_messages", "max_duration_seconds")}
    except HTTPException:
        mqtt_ready = False
        limits = None
    return {"status": "ready", "worker_ready": WORKER.is_file(), "default_transport": "dry_run", "mqtt_ready": mqtt_ready, "mqtt_limits": limits, "aws_connected": False, "active_job_id": JOBS.active_id()}


@app.get("/api/schema")
def schema():
    return CATALOG


@app.post("/api/preview")
def preview(body: RunRequest):
    return {"message": body.message, "plan": call_worker(body, "preview", "preview")}


@app.post("/api/runs", status_code=201)
def create_run(body: RunRequest):
    if body.realtime:
        raise HTTPException(409, detail="실제 시간 실행은 /api/jobs에서 시작하세요.")
    run_id = uuid4().hex
    locked = False
    try:
        if body.transport == "mqtt":
            locked = MQTT_LOCK.acquire(blocking=False)
            if not locked:
                raise HTTPException(409, detail="다른 MQTT 실행이 진행 중입니다. 완료 후 다시 실행하세요.")
        result = call_worker(body, body.transport, run_id)
    finally:
        if locked:
            MQTT_LOCK.release()
    created = datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run_id, created, json.dumps(body.model_dump(exclude_none=True)), json.dumps(result)))
    return {"id": run_id, "created_at": created, "request": body.model_dump(exclude_none=True), "result": result}


@app.post("/api/jobs", status_code=202)
def start_job(body: RunRequest):
    run_id = uuid4().hex
    data, _ = prepare_worker(body, body.transport, run_id)
    if body.transport == "mqtt":
        timeout = data["mqtt"]["max_duration_seconds"] + 5
    elif body.realtime:
        duration_ms = body.generation.duration_seconds * 1000 if body.generation else (body.config.messages_per_vehicle - 1) * body.config.interval_ms
        timeout = duration_ms / 1000 + 10
    else:
        timeout = 30
    return JOBS.start(run_id, body.model_dump(exclude_none=True), data, WORKER, timeout)


@app.get("/api/jobs/{run_id}")
def get_job(run_id: str):
    run = get_run(run_id)
    if "job_state" not in run["result"]:
        raise HTTPException(409, detail="이 기록은 실행 제어를 지원하지 않는 이전 방식의 실행입니다.")
    return run


@app.post("/api/jobs/{run_id}/stop", status_code=202)
def stop_job(run_id: str):
    get_job(run_id)
    JOBS.stop(run_id)
    return get_job(run_id)


@app.post("/api/jobs/{run_id}/pause", status_code=202)
def pause_job(run_id: str):
    get_job(run_id)
    JOBS.pause(run_id, True)
    return get_job(run_id)


@app.post("/api/jobs/{run_id}/resume", status_code=202)
def resume_job(run_id: str):
    get_job(run_id)
    JOBS.pause(run_id, False)
    return get_job(run_id)


@app.get("/api/runs")
def list_runs():
    with database() as db:
        rows = db.execute("SELECT id,created,result FROM runs ORDER BY created DESC LIMIT 30").fetchall()
    return [{"id": row[0], "created_at": row[1], "result": json.loads(row[2])} for row in rows]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with database() as db:
        row = db.execute("SELECT created,request,result FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="실행 기록이 없습니다.")
    return {"id": run_id, "created_at": row[0], "request": json.loads(row[1]), "result": json.loads(row[2])}


class DeleteRuns(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ids: list[str] = Field(min_length=1, max_length=30)
    confirm: bool = False


@app.post("/api/runs/delete")
def delete_runs(body: DeleteRuns):
    if not body.confirm:
        raise HTTPException(409, detail="선택한 로컬 실행 기록의 영구 삭제 확인이 필요합니다.")
    ids = list(dict.fromkeys(body.ids))
    with JOBS.lock, database() as db:
        for run_id in ids:
            row = db.execute("SELECT result FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise HTTPException(404, detail="삭제 대상 실행 기록이 없습니다. 목록을 새로 확인하세요.")
            if JOBS.active_id() == run_id or json.loads(row[0]).get("job_state") in ACTIVE:
                raise HTTPException(409, detail="실행 중이거나 일시중지된 기록은 삭제할 수 없습니다. 먼저 중단하세요.")
        db.executemany("DELETE FROM runs WHERE id=?", [(run_id,) for run_id in ids])
    return {"deleted_ids": ids, "deleted_count": len(ids)}


@app.patch("/api/runs/{run_id}")
def frozen_run(run_id: str):
    get_run(run_id)
    raise HTTPException(409, detail="실행 설정과 배정표는 고정됩니다. 다른 조건은 새 실행으로 비교하세요.")


if (ROOT / "apps/web/dist").is_dir():
    app.mount("/", StaticFiles(directory=ROOT / "apps/web/dist", html=True), name="web")
