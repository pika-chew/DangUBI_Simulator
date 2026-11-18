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

from fastapi import FastAPI, HTTPException, Request, Query
from services.control.schedule import ScheduleRequest, plan as plan_schedule
from services.control.schedule_obd import ScheduledOBDRequest, generate as generate_scheduled_obd
from services.control.fleet import FleetRequest, FleetOBDRequest, preview as preview_fleet, obd as fleet_obd
from services.control.checkpoints import CheckpointStore, CheckpointError, CheckpointReset
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
CHECKPOINTS = CheckpointStore(ROOT / ".local" / "obd-state.sqlite3")
@asynccontextmanager
async def lifespan(_app):
    JOBS.recover()
    try:
        yield
    finally:
        JOBS.shutdown()


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


def scheduled_response(data):
    return JSONResponse(status_code=202, content=data) if data["status"] == "preparing" else data


@app.post("/api/fleets/preview")
def fleet_preview(body: FleetRequest, offset: int = Query(default=0, ge=0, le=10000), limit: int = Query(default=100, ge=1, le=100), vehicle_id: str | None = Query(default=None)):
    try:
        return preview_fleet(body, offset, limit, vehicle_id)
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
        return plan_schedule(body, offset, limit, vehicle_id)
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


@app.get("/api/runs/{run_id}/trace/{vehicle_number}")
def vehicle_trace(run_id: str, vehicle_number: int, start_seconds: float = 0, end_seconds: float | None = None, message_offset: int = 0):
    import math
    run = get_run(run_id)
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
    try:
        summary=preview_fleet(body.fleet) if body.fleet is not None else plan_schedule(body.schedule)
    except ValueError as error:
        raise HTTPException(422,detail=str(error)) from error
    summary={k:v for k,v in summary.items() if k not in ("trips","offset","limit","has_more")}
    record_id,created=uuid4().hex,datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute("INSERT INTO workspace_plans VALUES (?,?,?,?)",(record_id,created,json.dumps(payload),json.dumps(summary)))
    return workspace_get_record("plan:"+record_id)


class ScheduleExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_key: str
    realtime: bool = True
    mapping_mode: Literal["shared", "dedicated"] = "shared"
    client_count: int = Field(default=1, ge=1, le=100)
    force_end_trip: bool = False
    transport: Literal["dry_run", "mqtt"] = "dry_run"
    confirm_mqtt: bool = False
    connection_lifetime: Literal["always", "while_driving"] = "always"


@app.post("/api/workspace/execute", status_code=202)
def workspace_execute(body: ScheduleExecutionRequest):
    from functools import partial
    from services.control.schedule import instant
    from services.control.schedule_execution import execute
    saved = workspace_get_record(body.plan_key)
    if saved["kind"] != "plan":
        raise HTTPException(422, detail="저장된 일정을 선택하세요.")
    source = WorkspacePlanRequest.model_validate(saved["request"])
    req = source.fleet or source.schedule
    start, end = instant(req.measurement.start), instant(req.measurement.end)
    if end-start > 3600000 or saved["planned_messages"] > 10000:
        raise HTTPException(422, detail="초기 일정 실행은 측정 기간1시간·10,000메시지 이하입니다. 장기 계획 조회와 실행 상한은 다릅니다.")
    vehicle_ids=[v["vehicle_id"] for v in saved["result"]["vehicles"]]
    clients=len(vehicle_ids) if body.mapping_mode=="dedicated" else body.client_count
    if clients > len(vehicle_ids):
        raise HTTPException(422, detail="연결 풀 Client 수는 차량 수 이하여야 합니다.")
    preview = preview_fleet if source.fleet else plan_schedule
    trips={}
    planned=0
    for vehicle in vehicle_ids:
        rows=[]
        for offset in range(0,10001,100):
            page=preview(req,offset=offset,limit=100,vehicle_id=vehicle)
            rows.extend(page["trips"])
            if not page["has_more"]:
                break
        trips[vehicle]=rows
        for row in rows:
            count=row["microtrip_count"]+row["trip_count"]
            if body.force_end_trip and row["continues_after_window"]:
                count += 0 if (end-row["start_ms"]) % req.interval_ms == 0 else 1
            planned+=count
    if planned>10000:
        raise HTTPException(422,detail="시험용 Trip을 포함한 실행은10,000메시지 이하여야 합니다.")
    profile=None
    if body.transport=="mqtt":
        if not body.confirm_mqtt:
            raise HTTPException(409,detail="실제 MQTT 송신 확인을 선택하세요.")
        if not body.realtime or body.force_end_trip:
            raise HTTPException(422,detail="일정 MQTT는 실제 시간·정상 Trip만 지원합니다. 가상시간 또는 시험용 Trip을 해제하세요.")
        profile=mqtt_profile()
        if clients>profile["max_clients"] or planned>profile["max_messages"] or end-start>=profile["max_duration_seconds"]*1000:
            raise HTTPException(422,detail="서버 MQTT 연결·메시지·시간 상한을 초과했습니다. 접속·응답 여유를 포함해 일정을 줄이세요.")
        if not WORKER.is_file():
            raise HTTPException(503,detail="Worker가 준비되지 않았습니다.")
    run_id=uuid4().hex
    assignments=[{"vehicle_id":v,"client_id":f"sim-{run_id}-c{i%clients+1:03d}"} for i,v in enumerate(vehicle_ids)]
    config={"vehicle_count":len(vehicle_ids),"client_count":clients,"mapping_mode":body.mapping_mode,"messages_per_vehicle":0,"interval_ms":req.interval_ms,"connection_lifetime":body.connection_lifetime}
    request={"config":config,"transport":body.transport,"realtime":body.realtime,"source_plan_key":body.plan_key,"schedule_plan":source.model_dump(exclude_none=True),"force_end_trip":body.force_end_trip}
    checkpoint_epoch=CHECKPOINTS.stats()["epoch"]
    def generate(vehicle,key,offset):
        if source.fleet:
            return fleet_obd(FleetOBDRequest(fleet=source.fleet,force_end_trip=body.force_end_trip,checkpoint_epoch=checkpoint_epoch),vehicle,key,WORKER,None,None,offset,100,checkpoint_store=CHECKPOINTS)
        return generate_scheduled_obd(ScheduledOBDRequest(schedule=source.schedule,force_end_trip=body.force_end_trip,checkpoint_epoch=checkpoint_epoch),vehicle,key,WORKER,None,None,offset,100,checkpoint_store=CHECKPOINTS)
    data={"operation":body.transport,"config":config,"planned_messages":planned,"assignments":assignments,"client_by_vehicle":{a["vehicle_id"]:a["client_id"] for a in assignments},"trips_by_vehicle":trips,"generate":generate,"measurement_start_ms":start,"duration_ms":end-start,"realtime":body.realtime,"validate_message":validate_message}
    if profile:data["mqtt"]=profile
    timeout=profile["max_duration_seconds"] if profile else (end-start)/1000+30 if body.realtime else 120
    return JOBS.start(run_id,request,data,WORKER,timeout,runner=partial(execute,JOBS))


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


@app.middleware("http")
async def current_web_page(request: Request, call_next):
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-store"
    return response


if (ROOT / "apps/web/dist").is_dir():
    app.mount("/", StaticFiles(directory=ROOT / "apps/web/dist", html=True), name="web")
