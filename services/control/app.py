"""로컬 제어 API: 기본 dry_run, 서버 프로필과 확인을 거친 소량 MQTT 시험."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
from contextlib import asynccontextmanager, contextmanager
from services.control.jobs import JobManager, ACTIVE
from services.control.runtime import RuntimePaths
from services.control.transmission_goal import assess
from services.control.operating import PROFILES, MAX_VEHICLES, TRIAL_DAYS, TRIAL_DURATION_MS
from services.control.registry import Registry, RegistryError, DevelopmentPseudonyms
from services.control.vin import trial_vins
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Query
from services.control.schedule import ScheduleRequest, plan as plan_schedule
from services.control.schedule_obd import ScheduledOBDRequest, generate as generate_scheduled_obd
from services.control.fleet import PatternMix, RoadMix, FleetRequest, FleetOBDRequest, preview as preview_fleet, obd as fleet_obd
from services.control.checkpoints import CheckpointStore, CheckpointError, CheckpointReset
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator
from services.control.message_codec import compile_check,prepare_message
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / "contracts/obd-development.json").read_text(encoding="utf-8"))
MESSAGE_VALIDATORS = {key: Draft202012Validator(profile["schema"]) for key,profile in CATALOG["profiles"].items()}
MESSAGE_CHECKS = {key: compile_check(profile["schema"]) for key,profile in CATALOG["profiles"].items()}
RUNTIME = RuntimePaths.from_env(ROOT)
WORKER = RUNTIME.worker
DB = RUNTIME.database
MQTT_PROFILE = RUNTIME.mqtt_profile
MQTT_LOCK = threading.Lock()
CHECKPOINTS = CheckpointStore(RUNTIME.checkpoints)
_REGISTRY = None
_OPERATION = None


def operation_vin_mode() -> str:
    """상시 운영 송신 식별자. 가명 VIN 생성 기능이 연결되기 전까지 기본은 원본(합성) VIN이다."""
    mode = os.environ.get("DANGUBI_OPERATION_VIN_MODE", "synthetic_raw")
    if mode not in ("synthetic_raw", "pseudonym"):
        raise RuntimeError("DANGUBI_OPERATION_VIN_MODE는 synthetic_raw 또는 pseudonym입니다.")
    return mode


def registry():
    # 상시 운영 명부. 실제 vault 연동 전에는 개발용 가명 발급기만 연결한다(실제 전송 불가 표시).
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = Registry(RUNTIME.registry, DevelopmentPseudonyms(), max_active=MAX_VEHICLES,
                             synthetic_raw_vin=operation_vin_mode() == "synthetic_raw")
    return _REGISTRY
@asynccontextmanager
async def lifespan(_app):
    JOBS.recover()
    # 시험 실행 자동 이어가기: 자동 이어가기를 켠 최근 실행이 오류·재시작으로 멈췄으면 확인 지점부터 다시 시작한다.
    from services.control.auto_resume import AutoResumer
    resumer = AutoResumer(JOBS, lambda run_id: recover_schedule(run_id, RecoverScheduleRequest(confirm_mqtt=True)))
    resumer.start()
    try:
        yield
    finally:
        resumer.stop()
        JOBS.shutdown()
        # 서버 종료 시 상시 운영은 강제 중단으로 기록해 다음 시작 때 확인된 지점부터 이어 보낸다.
        if _OPERATION is not None and _OPERATION.running():
            _OPERATION.stop(force=True)
            _OPERATION.wait(30)


app = FastAPI(title="DangUBI 로컬 제어 API", version="0.2.0", lifespan=lifespan)


@app.exception_handler(CheckpointError)
async def checkpoint_error(_request, error):
    return JSONResponse(status_code=409 if isinstance(error, CheckpointReset) else 503, content={"detail": str(error)})


@app.get("/api/checkpoints")
def checkpoint_stats():
    return CHECKPOINTS.stats()


@app.delete("/api/checkpoints")
def clear_checkpoints():
    return CHECKPOINTS.clear()


def plan_vins(fleet=None, schedule=None):
    """시험 실행 차량 VIN 표(전체 차량 기준, 가명처리 없음). 이전 운영 프로필은 기존 형식이라 VIN이 없다."""
    if fleet is not None:
        return {} if fleet.execution_profile=="operations" else trial_vins(f"v{i:06d}" for i in range(1,fleet.vehicle_count+1))
    return trial_vins(v.vehicle_id for v in schedule.vehicles)


def scheduled_response(data):
    return JSONResponse(status_code=202, content=data) if data["status"] == "preparing" else data


@app.post("/api/fleets/preview")
def fleet_preview(body: FleetRequest, offset: int = Query(default=0, ge=0, le=10000), limit: int = Query(default=100, ge=1, le=100), vehicle_id: str | None = Query(default=None)):
    try:
        return {**preview_fleet(body, offset, limit, vehicle_id), "vehicle_vins": plan_vins(fleet=body)}
    except ValueError as error:
        raise HTTPException(422, detail=str(error)) from error


@app.post("/api/fleets/obd/{vehicle_id}/{trip_key}")
def fleet_generate(body: FleetOBDRequest, vehicle_id: str, trip_key: str, start_ms: int | None = Query(default=None, ge=0), end_ms: int | None = Query(default=None, ge=0), offset: int = Query(default=0, ge=0, le=1000000), limit: int = Query(default=100, ge=1, le=100)):
    if not WORKER.is_file():
        raise HTTPException(503, detail="Worker 빌드가 필요합니다.")
    try:
        return scheduled_response(fleet_obd(body, vehicle_id, trip_key, WORKER, start_ms, end_ms, offset, limit, CHECKPOINTS))
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        raise HTTPException(422, detail=str(error)) from error


@app.post("/api/schedules/preview")
def schedule_preview(body: ScheduleRequest, offset: int = Query(default=0, ge=0, le=10000), limit: int = Query(default=100, ge=1, le=100), vehicle_id: str | None = Query(default=None)):
    try:
        return {**plan_schedule(body, offset, limit, vehicle_id), "vehicle_vins": plan_vins(schedule=body)}
    except ValueError as error:
        raise HTTPException(422, detail=str(error)) from error


@app.post("/api/schedules/obd/{vehicle_id}/{trip_key}")
def schedule_obd(body: ScheduledOBDRequest, vehicle_id: str, trip_key: str, start_ms: int | None = Query(default=None, ge=0), end_ms: int | None = Query(default=None, ge=0), offset: int = Query(default=0, ge=0, le=1000000), limit: int = Query(default=100, ge=1, le=100)):
    if not WORKER.is_file():
        raise HTTPException(503, detail="Worker 빌드가 필요합니다.")
    try:
        return scheduled_response(generate_scheduled_obd(body, vehicle_id, trip_key, WORKER, start_ms, end_ms, offset, limit, checkpoint_store=CHECKPOINTS))
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        raise HTTPException(422, detail=str(error)) from error


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    vehicle_count: int = Field(ge=1, le=10000)
    mapping_mode: Literal["shared", "dedicated"]
    client_count: int = Field(ge=1, le=10000)
    messages_per_vehicle: int = Field(ge=1, le=10000)
    interval_ms: int = Field(ge=1, le=86400000)
    connection_lifetime: Literal["always", "while_driving"] = "always"
    publish_mode: Literal["serial", "parallel"] = "serial"
    max_in_flight: int = Field(default=1, ge=1, le=100)
    queue_capacity: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def bounded(self):
        if self.max_in_flight > self.client_count or (self.publish_mode == "serial" and self.max_in_flight != 1):
            raise ValueError("동시 처리 수는 Client 수 이하여야 하며 순차 방식은1입니다.")
        if self.mapping_mode == "dedicated" and self.client_count != self.vehicle_count:
            raise ValueError("차량별 전체 연결은 차량 수와 Client 수가 같아야 합니다.")
        if self.mapping_mode == "shared" and self.client_count > self.vehicle_count:
            raise ValueError("풀 Client 수는 차량 수 이하여야 합니다.")
        return self


class Generation(BaseModel):
    @model_validator(mode="after")
    def mandatory_em(self):
        self.include_em = True
        return self

    include_em: bool = True
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    profile: Literal["urban", "highway", "congested", "mixed"]
    model_version: Literal["combustion-demo-1", "combustion-scenario-2", "combustion-route-3", "combustion-korea-4", "combustion-powertrain-5", "combustion-events-6"] = "combustion-demo-1"
    anomaly_intensity: Literal["low", "medium", "high"] | None = None
    engine_start: Literal["cold", "warm"] | None = None
    weather: Literal["dry", "rain"] | None = None
    anomaly_vehicle_percent: float = Field(default=0, ge=0, le=100)
    anomaly_events_per_hour: float = Field(default=0, ge=0, le=60)
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
    execution_profile: Literal["small_test", "operations"] = "small_test"
    broker_url: str
    ca_file: str
    cert_file: str
    key_file: str
    topic_prefix: str = "sim"
    max_clients: int = Field(default=5, ge=1, le=25000)
    max_messages: int = Field(default=20, ge=1, le=1000000000000)
    max_duration_seconds: int = Field(default=30, ge=1, le=2592600)
    connect_per_second: int = Field(default=2, ge=1, le=1000)
    publish_per_second: int = Field(default=10, ge=1, le=20000)
    max_connecting: int = Field(default=100, ge=1, le=1000)
    timeout_ms: int = Field(default=3000, ge=100, le=5000)
    reconnect_attempts: int = Field(default=2, ge=0, le=3)
    # 발행 응답 지연·연결 끊김 때 재접속 뒤 같은 메시지를 다시 보내며 버티는 시간(초, 0이면 즉시 중지).
    retry_window_seconds: int = Field(default=300, ge=0, le=3600)

    @model_validator(mode="after")
    def profile_bounds(self):
        if self.execution_profile=="small_test" and any(getattr(self,key)>value for key,value in
            dict(max_clients=20,max_messages=100,max_duration_seconds=30,connect_per_second=5,publish_per_second=70).items()):
            raise ValueError("소량 MQTT 시험의 연결·건수·시간·속도 상한을 초과했습니다.")
        return self


def mqtt_profile(mode=None):
    try:
        profile = MQTTProfile.model_validate_json(MQTT_PROFILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise HTTPException(409, detail="서버 MQTT 설정이 준비되지 않았습니다. 제품 사용 안내를 확인하세요.")
    if not profile.enabled or not profile.contract_reviewed:
        raise HTTPException(409, detail="서버 MQTT 활성화와 수신 규격·시험 토픽 확인이 필요합니다.")
    result = profile.model_dump()
    if mode=="small_test":
        result["execution_profile"]="small_test"
        for key,cap in dict(max_clients=20,max_messages=100,max_duration_seconds=30,connect_per_second=5,publish_per_second=70,max_connecting=20).items():
            result[key]=min(result[key],cap)
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
    key=str(message["ty"])
    check=MESSAGE_CHECKS[key]
    errors = [] if check is not None and check(message) else sorted(MESSAGE_VALIDATORS[key].iter_errors(message), key=lambda e: str(e.path))
    if errors:
        descriptions = {"required": "필수 항목이 누락됐습니다.", "type": "자료형을 확인하세요.", "minimum": "허용 최솟값 미만입니다.", "maximum": "허용 최댓값을 초과했습니다.", "additionalProperties": "규격에 없는 항목이 포함됐습니다.", "pattern": "값의 표현 형식을 확인하세요.", "maxLength": "허용 길이를 초과했습니다.", "const": "허용되지 않은 값입니다."}
        raise HTTPException(422, detail=[f"{' / '.join(map(str, e.path)) or '메시지'}: {descriptions.get(e.validator, '입력값을 확인하세요.')}" for e in errors[:20]])
    payload = message["pld"]
    if message["ty"] == 3 and payload["stt"] > payload["edt"]:
        raise HTTPException(422, detail=["Trip 시작 시각은 종료 시각 이하여야 합니다."])
    encoded=prepare_message(message)
    if encoded.size > 131072:
        raise HTTPException(422, detail=["메시지는 128KiB 이하여야 합니다."])
    return encoded


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
        if body.config.mapping_mode != "shared":
            raise HTTPException(422, detail="신규 MQTT 전송은 연결 풀만 지원합니다.")
        if not body.confirm_mqtt:
            raise HTTPException(409, detail="실제 MQTT 송신 확인을 선택하세요.")
        profile = mqtt_profile("small_test")
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
    if operation == "mqtt":
        result["transmission_goal"] = assess(result)
    return result


@contextmanager
def database():
    DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB)
    connection.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT, request TEXT, result TEXT)")
    connection.execute("CREATE TABLE IF NOT EXISTS workspace_plans (id TEXT PRIMARY KEY, created TEXT, request TEXT, summary TEXT)")
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
        limits = {k: profile[k] for k in ("execution_profile","max_clients", "max_messages", "max_duration_seconds","connect_per_second","publish_per_second","max_connecting")}
    except HTTPException:
        mqtt_ready = False
        limits = None
    return {"status": "ready", "worker_ready": WORKER.is_file(), "default_transport": "dry_run", "mqtt_ready": mqtt_ready, "mqtt_limits": limits, "execution_profiles":PROFILES,"features":{"microtrip_em":True,"microtrip_event_search":True},"aws_connected": False, "active_job_id": JOBS.active_id()}


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


class RecoverScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm_mqtt: bool = False


@app.post("/api/jobs/{run_id}/recover", status_code=202)
def recover_schedule(run_id: str, body: RecoverScheduleRequest):
    run = get_job(run_id)
    request, result = run["request"], run["result"]
    if (result.get("job_state") not in ("interrupted", "stopped", "failed")
            or not result.get("schedule_checkpoint") or not request.get("schedule_plan")):
        raise HTTPException(409, detail="이어서 재생할 시험 실행 체크포인트가 없습니다.")
    config = request["config"]
    generation = request["generation"]
    execution = ScheduleExecutionRequest(
        plan_key=request["source_plan_key"], transport=request["transport"], realtime=request["realtime"],
        mapping_mode=config["mapping_mode"], client_count=config["client_count"],
        connection_lifetime=config["connection_lifetime"], force_end_trip=request["force_end_trip"],
        confirm_mqtt=body.confirm_mqtt, generation_workers=generation["workers"],
        generation_queue_messages=generation["queue_messages"], generation_queue_bytes=generation["queue_bytes"],
        auto_resume=bool(request.get("auto_resume", False)),
    )
    return execute_saved_schedule(execution, recovery=run)


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


@app.get("/api/runs/{run_id}/trace/{vehicle_number}")
def vehicle_trace(run_id: str, vehicle_number: int, start_seconds: float = 0, end_seconds: float | None = None, message_offset: int = 0):
    import math
    run = get_run(run_id)
    if run["request"].get("source_plan_key") or run["request"].get("schedule_plan"):
        raise HTTPException(409, detail="일정 실행의 차량 데이터는 원본 일정에서 차량과 운행을 선택해 조회하세요. 이 조회는 단일 운행 실행용입니다.")
    body = RunRequest.model_validate(run["request"])
    if not body.generation or not 1 <= vehicle_number <= body.config.vehicle_count or message_offset < 0 or message_offset > 10000:
        raise HTTPException(422, detail="자동 생성 실행의 차량 번호를 확인하세요.")
    last_sequence = (run["result"]["processed_messages"] - vehicle_number) // body.config.vehicle_count
    if last_sequence < 0:
        return {"vehicle_id": f"v{vehicle_number:06d}", "trace": [], "samples": [], "message_total": 0, "message_offset": message_offset, "range_end_seconds": 0, "source": "아직 생성된 구간이 없습니다."}
    available_ms = min(last_sequence * body.config.interval_ms, body.generation.duration_seconds * 1000)
    end = available_ms / 1000 if end_seconds is None else end_seconds
    if not math.isfinite(start_seconds) or not math.isfinite(end) or start_seconds < 0 or end < start_seconds or end > available_ms / 1000:
        raise HTTPException(422, detail="조회 범위는 해당 차량이 생성한 구간 안으로 지정하세요.")
    start_ms, end_ms = math.ceil(start_seconds * 1000), math.floor(end * 1000)
    if start_ms > end_ms:
        return {"vehicle_id": f"v{vehicle_number:06d}", "trace": [], "samples": [], "message_total": 0, "message_offset": message_offset, "range_end_seconds": end, "model_version": body.generation.model_version, "source": "조회 범위에 생성 시점이 없습니다. 전송 수신 기록이 아닙니다."}
    data, timeout = prepare_worker(body, "trace", run_id)
    data.update(trace_vehicle=vehicle_number, trace_start_ms=start_ms, trace_until_ms=end_ms, trace_offset=message_offset, trace_limit=100)
    try:
        process = subprocess.run([str(WORKER)], input=json.dumps(data).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        result = json.loads(process.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise HTTPException(502, detail="차량별 조회 결과를 확인하지 못했습니다.")
    if process.returncode or "error" in result:
        raise HTTPException(422, detail=result.get("error", "차량별 조회에 실패했습니다."))
    samples = result.get("samples", [])
    # 재생성은 OBD 값 확인용이다. 당시 발행 토픽이나 송수신 결과로 해석하지 않는다.
    return {"vehicle_id": f"v{vehicle_number:06d}", "trace": result.get("trace", []), "samples": samples, "message_total": result["planned_messages"], "message_offset": message_offset, "range_end_seconds": end, "model_version": result["model_version"], "source": "저장 설정·seed·모델 버전으로 재생성한 구간입니다. 당시 전송 원문·토픽·수신 기록이 아닙니다."}


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


class WorkspacePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="차량 시뮬레이션", min_length=1, max_length=80)
    fleet: FleetRequest | None = None
    schedule: ScheduleRequest | None = None

    @model_validator(mode="after")
    def source(self):
        if (self.fleet is None) == (self.schedule is None) or not self.title.strip():
            raise ValueError("전체 조건 또는 개별 일정 하나와 작업 이름을 지정하세요.")
        return self


def workspace_record(kind, row, detail=False):
    record_id, created, raw_request, raw_result = row
    request, result = json.loads(raw_request), json.loads(raw_result)
    planned = kind == "plan"
    item = {"key":kind+":"+record_id,"id":record_id,"kind":kind,"created_at":created,
            "title":request["title"] if planned else ("MQTT 전송" if request.get("transport")=="mqtt" else "로컬 생성")+" · "+str(request["config"]["vehicle_count"])+"대",
            "state":"planned" if planned else result.get("job_state",result.get("state","unknown")),
            "vehicle_count":result["vehicle_count"] if planned else request["config"]["vehicle_count"],
            "planned_messages":result.get("planned_messages",0),
            "transport":"local" if planned else request.get("transport","dry_run")}
    if detail:
        item.update(request=request,result=result)
    return item


@app.get("/api/workspace/records")
def workspace_records():
    with database() as db:
        plans=db.execute("SELECT id,created,request,summary FROM workspace_plans ORDER BY created DESC LIMIT 50").fetchall()
        runs=db.execute("SELECT id,created,request,result FROM runs ORDER BY created DESC LIMIT 50").fetchall()
    records=[workspace_record("plan",r) for r in plans]+[workspace_record("run",r) for r in runs]
    return {"records":sorted(records,key=lambda r:r["created_at"],reverse=True)[:50],"limit":50}


@app.get("/api/workspace/records/{key}")
def workspace_get_record(key: str):
    kind, _, record_id=key.partition(":")
    if kind not in ("plan","run") or not record_id:
        raise HTTPException(404,detail="작업 기록이 없습니다.")
    with database() as db:
        query="SELECT id,created,request,summary FROM workspace_plans WHERE id=?" if kind=="plan" else "SELECT id,created,request,result FROM runs WHERE id=?"
        row=db.execute(query,(record_id,)).fetchone()
    if row is None:
        raise HTTPException(404,detail="작업 기록이 없습니다.")
    return workspace_record(kind,row,True)


@app.post("/api/workspace/plans",status_code=201)
def workspace_create_plan(body: WorkspacePlanRequest):
    payload=body.model_dump(exclude_none=True)
    if len(json.dumps(payload).encode())>2097152:
        raise HTTPException(422,detail="일정 설정은2MiB 이하여야 합니다.")
    from services.control.schedule import instant
    source = body.fleet or body.schedule
    if (body.fleet is None or body.fleet.execution_profile == "small_test") and (
        instant(source.measurement.end) - instant(source.measurement.start) > TRIAL_DURATION_MS
    ):
        raise HTTPException(422, detail=f"시험 일정은 최대 {TRIAL_DAYS}일입니다.")
    try:
        summary=preview_fleet(body.fleet) if body.fleet is not None else plan_schedule(body.schedule)
    except ValueError as error:
        raise HTTPException(422,detail=str(error)) from error
    summary={k:v for k,v in summary.items() if k not in ("trips","offset","limit","has_more")}
    # 시험 실행 차량의 VIN(가명처리 없음). 조회 화면 표시용이며 생성 키는 내부 ID를 유지한다.
    summary["vehicle_vins"]=plan_vins(fleet=body.fleet,schedule=body.schedule)
    record_id,created=uuid4().hex,datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute("INSERT INTO workspace_plans VALUES (?,?,?,?)",(record_id,created,json.dumps(payload),json.dumps(summary)))
    return workspace_get_record("plan:"+record_id)


class ScheduleExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_key: str
    realtime: bool = True
    mapping_mode: Literal["shared", "dedicated"] = "shared"
    client_count: int | None = Field(default=None, ge=1, le=25000)
    force_end_trip: bool = False
    transport: Literal["dry_run", "mqtt"] = "dry_run"
    confirm_mqtt: bool = False
    connection_lifetime: Literal["always", "while_driving"] = "always"
    generation_workers: int = Field(default=1,ge=1,le=4,strict=True)
    generation_queue_messages: int = Field(default=2000,ge=256,le=65536)
    generation_queue_bytes: int = Field(default=8388608,ge=4194304,le=67108864)
    # 오류로 멈추거나 서버가 재시작되면 서버가 확인 지점부터 자동으로 이어간다(화면 기본 사용).
    auto_resume: bool = False

    @model_validator(mode="after")
    def worker_count(self):
        if self.generation_workers not in (1,2,4):
            raise ValueError("생성 Worker 수는1·2·4개만 지원합니다.")
        return self


@app.post("/api/workspace/execute", status_code=202)
def workspace_execute(body: ScheduleExecutionRequest):
    return execute_saved_schedule(body)


def execute_saved_schedule(body: ScheduleExecutionRequest, recovery=None):
    from functools import partial
    from services.control.schedule import instant
    from services.control.schedule_execution import execute
    from services.control.mqtt_bridge import mqtt_destination
    if recovery:
        saved = {"planned_messages": recovery["result"]["planned_messages"]}
        source = WorkspacePlanRequest.model_validate(recovery["request"]["schedule_plan"])
    else:
        saved = workspace_get_record(body.plan_key)
        if saved["kind"] != "plan":
            raise HTTPException(422, detail="저장된 일정을 선택하세요.")
        source = WorkspacePlanRequest.model_validate(saved["request"])
    req = source.fleet or source.schedule
    mode=source.fleet.execution_profile if source.fleet else "small_test"
    limits=PROFILES[mode]
    if mode!="small_test" and _OPERATION is not None and _OPERATION.running():
        raise HTTPException(409, detail="상시 운영 중에는 이전 운영 프로필을 실행할 수 없습니다.")
    start, end = instant(req.measurement.start), instant(req.measurement.end)
    if end-start > limits["max_duration_ms"]:
        raise HTTPException(422, detail=f"일정 기간은 최대 {limits['max_duration_ms'] // 86400000}일입니다.")
    if saved["planned_messages"] > limits["max_messages"]:
        raise HTTPException(422, detail="예정 메시지 건수가 실행 상한을 초과했습니다.")
    if recovery:
        vehicle_ids = recovery["result"]["schedule_checkpoint"]["vehicles"]
    elif source.fleet:
        vehicle_ids = [f"v{i:06d}" for i in range(1, source.fleet.vehicle_count + 1)]
    else:
        vehicle_ids = sorted(v.vehicle_id for v in source.schedule.vehicles)
    if not recovery and body.mapping_mode != "shared":
        raise HTTPException(422, detail="신규 일정 실행은 연결 풀만 지원합니다.")
    clients=len(vehicle_ids) if body.mapping_mode=="dedicated" else (body.client_count if body.client_count is not None else min(limits["default_clients"],len(vehicle_ids)))
    if mode=="small_test" and (body.generation_queue_messages>2000 or body.generation_queue_bytes>16777216):
        raise HTTPException(422,detail="시험 실행의 대기열 상한을 초과했습니다.")
    if clients > len(vehicle_ids):
        raise HTTPException(422, detail="연결 풀 Client 수는 차량 수 이하여야 합니다.")
    from services.control.schedule_cursor import provider_for
    stream=None
    if source.fleet and (mode=="operations" or len(vehicle_ids)>100):
        from services.control.fleet_execution import FleetExecution
        stream=FleetExecution(source.fleet,WORKER,body.force_end_trip)
    trip_provider=stream.trips if stream else provider_for(source)
    planned=saved["planned_messages"]
    if body.force_end_trip and not recovery:
        for vehicle in vehicle_ids:
            for row in trip_provider(vehicle):
                if row["continues_after_window"]:planned+=0 if (end-row["start_ms"])%req.interval_ms==0 else 1
    if planned>limits["max_messages"]:
        raise HTTPException(422,detail="시험용 Trip을 포함한 실행 건수가 프로필 상한을 초과했습니다.")
    profile=None
    if body.transport=="mqtt":
        if not body.confirm_mqtt:
            raise HTTPException(409,detail="실제 MQTT 송신 확인을 선택하세요.")
        if body.force_end_trip:
            raise HTTPException(422,detail="시험용 종료 Trip은 실제 MQTT로 전송할 수 없습니다.")
        if mode=="operations" and not body.realtime:
            raise HTTPException(422,detail="이전 운영 프로필은 기본 전송만 지원합니다.")
        profile=mqtt_profile()
        if mode=="operations" and profile["execution_profile"]!="operations":
            raise HTTPException(409,detail="운영 MQTT 서버 프로필이 필요합니다. 현재 소량 시험 설정은 유지됩니다.")
        minimum_seconds = (end-start)/1000 if body.realtime else planned/profile["publish_per_second"]
        if clients>profile["max_clients"] or planned>profile["max_messages"] or minimum_seconds>=profile["max_duration_seconds"]:
            raise HTTPException(422,detail="서버 MQTT 연결·메시지·시간 상한을 초과했습니다. 접속·응답 여유를 포함해 일정을 줄이세요.")
        if body.realtime and not recovery:
            # 실제 경과 시간 한도에는 시작 준비·일시중지·재접속 대기가 함께 들어간다. 일정 길이의 10%를 남긴다.
            needed = int(minimum_seconds * 1.1) + 1
            if profile["max_duration_seconds"] < needed:
                raise HTTPException(422,detail=f"서버 MQTT 프로필의 실행 시간 한도(max_duration_seconds {profile['max_duration_seconds']:,}초)가 일정 {int(minimum_seconds):,}초에 비해 여유가 부족합니다. "
                                               f"일시중지·재접속 대기를 고려해 {needed:,}초 이상으로 설정하세요.")
        if mode=="operations":
            profile={**profile,"max_duration_seconds":min(profile["max_duration_seconds"],(end-start+999)//1000+600)}
        if not WORKER.is_file():
            raise HTTPException(503,detail="Worker가 준비되지 않았습니다.")
    run_id = recovery["id"] if recovery else uuid4().hex
    # 시험 실행은 VIN을 그대로 전송한다(가명처리 없음). 이전 운영 프로필(개발 측정) 경로는 기존 형식 유지.
    vehicle_alias=plan_vins(fleet=source.fleet,schedule=source.schedule) if mode=="small_test" else {}
    assignments=[{"vehicle_id":v,"client_id":f"sim-{run_id}-c{i%clients+1:03d}",**({"vin":vehicle_alias[v]} if vehicle_alias else {})} for i,v in enumerate(vehicle_ids)]
    config={"vehicle_count":len(vehicle_ids),"client_count":clients,"mapping_mode":body.mapping_mode,"messages_per_vehicle":0,"interval_ms":req.interval_ms,"connection_lifetime":body.connection_lifetime}
    request={"config":config,"execution_profile":mode,"transport":body.transport,"realtime":body.realtime,"source_plan_key":body.plan_key,"schedule_plan":source.model_dump(exclude_none=True),"force_end_trip":body.force_end_trip,"auto_resume":body.auto_resume}
    from services.control.execution_checkpoints import ExecutionCheckpoints
    execution_checkpoints=ExecutionCheckpoints();checkpoint_epoch=0
    scheduled_body=ScheduledOBDRequest(schedule=source.schedule,force_end_trip=body.force_end_trip,checkpoint_epoch=checkpoint_epoch) if source.schedule else None
    def generate(vehicle,key,offset,session=None,limit=100):
        if source.fleet:
            return fleet_obd(FleetOBDRequest(fleet=source.fleet,force_end_trip=body.force_end_trip,checkpoint_epoch=checkpoint_epoch),vehicle,key,WORKER,None,None,offset,limit,checkpoint_store=execution_checkpoints,session=session)
        return generate_scheduled_obd(scheduled_body,vehicle,key,WORKER,None,None,offset,limit,checkpoint_store=execution_checkpoints,session=session)
    data={"operation":body.transport,"execution_profile":mode,"config":config,"planned_messages":planned,"assignments":assignments,"client_by_vehicle":{a["vehicle_id"]:a["client_id"] for a in assignments},"generate":stream.generate if stream else generate,"measurement_start_ms":start,"duration_ms":end-start,"realtime":body.realtime,"validate_message":validate_message}
    if stream:data["generate_batch"]=stream.generate_many
    if len(vehicle_ids) > 100:
        data["sequential"] = True
    if vehicle_alias:data["vehicle_alias"]=vehicle_alias
    if profile:data["mqtt"]=profile
    if mode == "small_test":
        if not WORKER.is_file():
            raise HTTPException(503, detail="Worker가 준비되지 않았습니다.")
        identity = {"worker": hashlib.sha256(WORKER.read_bytes()).hexdigest(),
                    "destination": mqtt_destination(profile), "vehicles": vehicle_ids,
                    "aliases": vehicle_alias}
        if recovery and any(recovery["result"]["schedule_checkpoint"].get(k) != value for k, value in identity.items()):
            raise HTTPException(409, detail="복구할 Worker, 차량 VIN 또는 MQTT 전송 대상이 바뀌었습니다.")
        data["schedule_checkpoint"] = (recovery["result"]["schedule_checkpoint"] if recovery
                                       else {**identity, "virtual_ms": 0, "confirmed": {}, "count": 0})
    if recovery and recovery["result"].get("auto_recovery"):
        data["auto_recovery"] = recovery["result"]["auto_recovery"]  # 자동 이어가기 기록을 새 실행 결과로 넘긴다
    if mode=="operations":
        data.update(transmission_queue_messages=min(4096,max(100,clients*2)),transmission_queue_bytes=8388608,stop_grace_seconds=15)
    request["generation"]={"workers":body.generation_workers,"queue_messages":body.generation_queue_messages,"queue_bytes":body.generation_queue_bytes}
    data.update(trip_provider=trip_provider,generation_workers=body.generation_workers,generation_queue_messages=body.generation_queue_messages,generation_queue_bytes=body.generation_queue_bytes,execution_checkpoint_stats=stream.stats if stream else execution_checkpoints.stats)
    timeout = profile["max_duration_seconds"] if profile else max(120, (end-start)/1000 + 600)
    return JOBS.start(run_id,request,data,WORKER,timeout,runner=partial(execute,JOBS),recovery=recovery)


class DeleteWorkspaceRecords(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    keys: list[str]=Field(min_length=1,max_length=30)
    confirm: bool=False


@app.post("/api/workspace/delete")
def workspace_delete_records(body: DeleteWorkspaceRecords):
    if not body.confirm:
        raise HTTPException(409,detail="선택한 로컬 작업 기록의 영구 삭제 확인이 필요합니다.")
    keys=list(dict.fromkeys(body.keys))
    with JOBS.lock,database() as db:
        targets=[]
        for key in keys:
            kind,_,record_id=key.partition(":")
            if kind not in ("plan","run") or not record_id:
                raise HTTPException(404,detail="삭제할 작업 기록이 없습니다.")
            query="SELECT summary FROM workspace_plans WHERE id=?" if kind=="plan" else "SELECT result FROM runs WHERE id=?"
            row=db.execute(query,(record_id,)).fetchone()
            if row is None:
                raise HTTPException(404,detail="삭제 대상이 없습니다. 목록을 새로 확인하세요.")
            if kind=="run" and (JOBS.active_id()==record_id or json.loads(row[0]).get("job_state") in ACTIVE):
                raise HTTPException(409,detail="진행 중인 실행은 먼저 중단하세요.")
            targets.append((kind,record_id))
        for kind,record_id in targets:
            db.execute("DELETE FROM workspace_plans WHERE id=?" if kind=="plan" else "DELETE FROM runs WHERE id=?",(record_id,))
    return {"deleted_keys":keys,"deleted_count":len(keys)}


class RegisterVehicles(BaseModel):
    model_config=ConfigDict(extra="forbid")
    count: int=Field(ge=1,le=MAX_VEHICLES)
    pattern_mix: PatternMix=Field(default_factory=PatternMix)
    road_mix: RoadMix=Field(default_factory=RoadMix)
    anomaly_vehicle_percent: float=Field(default=5,ge=0,le=100)
    trip_min_minutes: int=Field(default=3,ge=2,le=240)
    trip_max_minutes: int=Field(default=120,ge=2,le=240)

    @model_validator(mode="after")
    def trip_range(self):
        if self.trip_min_minutes>self.trip_max_minutes:
            raise ValueError("최소 운행 시간은 최대 운행 시간 이하여야 합니다.")
        return self


def operation():
    global _OPERATION
    if _OPERATION is None:
        from services.control.operation_runtime import OperationController
        _OPERATION = OperationController(registry(), WORKER, validate_message)
    return _OPERATION


class StartOperation(BaseModel):
    model_config=ConfigDict(extra="forbid")
    settings: dict[str, Any]=Field(default_factory=dict)
    confirm_mqtt: bool=False


class StopOperation(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    force: bool=False


class ResolveHold(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    ids: list[int]=Field(min_length=1,max_length=MAX_VEHICLES)
    action: Literal["retry","drop"]


@app.get("/api/operations/status")
def operations_status():
    return operation().status()


@app.get("/api/operations/events")
def operations_events(limit: int = Query(default=50, ge=1, le=200)):
    return {"events": registry().events(limit)}


def operation_settings(raw: dict):
    from pydantic import ValidationError
    from services.control.operation_plan import OperationSettings
    try:
        return OperationSettings.model_validate(raw)
    except ValidationError as error:
        reasons=[str(e.get("ctx",{}).get("error") or e["msg"]) for e in error.errors()[:3]]
        raise HTTPException(422, detail="상시 운영 설정을 확인하세요. "+" / ".join(reasons)) from error


def prepare_operation(body: StartOperation):
    """시작·설정 변경 공통 검사. (설정, MQTT 프로필)을 돌려준다."""
    settings=operation_settings(body.settings)
    if not WORKER.is_file():
        raise HTTPException(503, detail="Worker가 준비되지 않았습니다.")
    profile=None
    if settings.transport=="mqtt":
        if not body.confirm_mqtt:
            raise HTTPException(409, detail="실제 MQTT 송신 확인을 선택하세요.")
        profile=mqtt_profile()
        from urllib.parse import urlsplit
        loopback=urlsplit(profile["broker_url"]).hostname in ("127.0.0.1","::1")
        if not (registry().synthetic_raw_vin or registry().provider.allows_real_transport or loopback):
            raise HTTPException(409, detail="개발용 가명 발급기로 만든 가명 VIN은 외부 브로커 전송에 쓸 수 없습니다. vault 연동 후 사용하세요(로컬 브로커 검증만 허용).")
        if profile["execution_profile"]!="operations":
            raise HTTPException(409, detail="운영 MQTT 서버 프로필이 필요합니다.")
        saved=registry().operation()
        checkpoint, window_end = saved["virtual_checkpoint_ms"], saved["window_end_ms"]
        remaining = ((window_end-checkpoint+999)//1000 if checkpoint is not None and window_end is not None and checkpoint<window_end
                     else settings.duration_cap_days*86400)
        needed=remaining+600
        if settings.client_count>profile["max_clients"] or needed>profile["max_duration_seconds"]:
            raise HTTPException(422, detail="서버 MQTT Client 수 또는 실제 실행시간 상한을 확인하세요(가상 운영 기간과 정리 여유 10분 이상 필요).")
    return settings, profile


@app.post("/api/operations/start", status_code=202)
def operations_start(body: StartOperation):
    settings, profile = prepare_operation(body)
    try:
        operation().start(settings, profile)
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error
    return operation().status()


@app.post("/api/operations/settings/apply", status_code=202)
def operations_apply_settings(body: StartOperation):
    """실행 중 설정 변경: 정상 중단 뒤 새 설정으로 자동 시작한다."""
    settings, profile = prepare_operation(body)
    try:
        operation().apply_settings(settings, profile)
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error
    return operation().status()


@app.post("/api/operations/settings/cancel")
def operations_cancel_settings():
    try:
        operation().cancel_pending()
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error
    return operation().status()


class FocusPreview(BaseModel):
    model_config=ConfigDict(extra="forbid")
    settings: dict[str, Any]
    date: str=Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.post("/api/operations/focus/preview")
def operations_focus_preview(body: FocusPreview):
    """현재 명부에서 운영에 넣을 차량 기준의 예상 집중 결과(저장하지 않음)."""
    from datetime import date as Date
    from services.control.operation_plan import FocusPlanner
    from services.control.operation_runtime import initial_selection
    settings=operation_settings(body.settings)
    if settings.traffic_focus is None:
        raise HTTPException(422, detail="운행 집중 시간대를 하나 이상 지정하세요.")
    try:
        day=Date.fromisoformat(body.date)
    except ValueError as error:
        raise HTTPException(422, detail="날짜를 확인하세요.") from error
    reg=registry()
    selection=initial_selection(reg.session_vehicles(), settings.vehicle_count or 20000)
    if not selection:
        raise HTTPException(409, detail="운영에 넣을 준비된 차량이 없습니다.")
    planner=FocusPlanner(settings, reg.seed(), None)
    for info in selection:
        planner.add(info["id"], info["fixed"])
    report=planner.report(day, save=False)
    saved=reg.focus_day(day.isoformat(), planner.digest)
    return {**report, "frozen": saved is not None, **({"frozen_report": saved} if saved else {})}


@app.post("/api/operations/stop")
def operations_stop(body: StopOperation):
    try:
        operation().stop(force=body.force)
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error
    return operation().status()


@app.post("/api/operations/pause", status_code=202)
@app.post("/api/operations/resume", status_code=202)
def operations_pause(request: Request):
    try:
        operation().pause(request.url.path.endswith("/pause"))
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error
    return operation().status()


@app.post("/api/operations/vehicles/hold")
def operations_resolve_hold(body: ResolveHold):
    return registry().resolve_hold(body.ids, body.action)


class RemoveVehicles(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    ids: list[int]=Field(min_length=1,max_length=MAX_VEHICLES)
    confirm: bool=False


@app.get("/api/operations/registry")
def operations_registry():
    return registry().summary()


@app.get("/api/operations/vehicles")
def operations_vehicles(status: str | None = Query(default=None), offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100)):
    try:
        return registry().vehicles(status, offset, limit)
    except RegistryError as error:
        raise HTTPException(422, detail=str(error)) from error


@app.post("/api/operations/vehicles")
def operations_register(body: RegisterVehicles):
    try:
        return registry().register(body.count, body.model_dump(exclude={"count"}))
    except RegistryError as error:
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/operations/pseudonyms/retry")
def operations_pseudonym_retry():
    return registry().issue_pseudonyms()


@app.post("/api/operations/vehicles/remove")
def operations_remove(body: RemoveVehicles):
    if not body.confirm:
        raise HTTPException(409, detail="선택한 차량을 명부에서 제외하려면 확인이 필요합니다. VIN 일련번호는 재사용하지 않습니다.")
    return registry().request_removal(body.ids)


@app.get("/api/operations/vehicles/lookup")
def operations_vehicle_lookup(q: str = Query(min_length=1, max_length=128)):
    vehicle=registry().find_vehicle(q)
    if vehicle is None:
        raise HTTPException(404, detail="명부 번호 또는 가명 VIN으로 차량을 찾지 못했습니다.")
    return vehicle


@app.get("/api/operations/vehicles/{vehicle_id}/history")
def operations_vehicle_history(vehicle_id: int, from_ms: int | None = Query(default=None, ge=0, le=4102444800000), to_ms: int | None = Query(default=None, ge=0, le=4102444800000)):
    if (from_ms is None) != (to_ms is None) or (from_ms is not None and (to_ms<=from_ms or to_ms-from_ms>31*86400000)):
        raise HTTPException(422, detail="조회 기간은 시작 < 종료이며 최대 31일입니다.")
    try:
        return registry().history(vehicle_id, from_ms, to_ms)
    except RegistryError as error:
        raise HTTPException(404, detail=str(error)) from error


class OperationOBDRequest(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    checkpoint_epoch: int | None=None  # 공용 이벤트 찾기 화면이 보내는 값. 상시 운영 조회는 중간 상태를 쓰지 않는다.
    search_events: bool=False
    event_mask: int=Field(default=0,ge=0,le=255)


@app.post("/api/operations/obd/{vehicle_id}/{trip_key}")
def operations_obd(body: OperationOBDRequest, vehicle_id: int, trip_key: str, start_ms: int | None = Query(default=None, ge=0), end_ms: int | None = Query(default=None, ge=0), offset: int = Query(default=0, ge=0, le=1000000), limit: int = Query(default=100, ge=1, le=100)):
    from services.control.operation_query import generate as regenerate
    if not WORKER.is_file():
        raise HTTPException(503, detail="Worker 빌드가 필요합니다.")
    try:
        trip=registry().history_trip(vehicle_id, trip_key)
    except RegistryError as error:
        raise HTTPException(404, detail=str(error)) from error
    try:
        return regenerate(trip, WORKER, start_ms, end_ms, offset, limit, body.search_events, body.event_mask)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        raise HTTPException(422, detail=str(error)) from error


@app.middleware("http")
async def current_web_page(request: Request, call_next):
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-store"
    return response


web_dist = Path(os.environ.get("DANGUBI_WEB_DIST", ROOT / "apps/web/dist"))
if web_dist.is_dir():
    app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
