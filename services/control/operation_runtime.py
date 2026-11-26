"""상시 운영 실행기. 명부 차량을 연속 가상시계로 운행시키고 가명 VIN으로 전송한다.

- 토픽 `{prefix}/ops-main/{가명VIN}/obd`, Client `sim-ops-main-cNNN`, 본문 `{vehicleId, ty, ts, pld}`.
- 정상 중단: 진행 중 운행은 끝까지 보내고 아직 시작하지 않은 운행은 보내지 않는다.
- 강제 중단·오류: 운행 시작 상태와 마지막 PUBACK 확인 시각으로 다음 시작 때 같은 운행을 다시 만들어
  확인된 메시지 다음부터 이어 보낸다(미확인 구간은 동일 내용 중복 가능, 기록).
- 기간 상한(실제 경과 시간)에 도달하면 자동으로 정상 중단한다. 재개하면 다시 0부터 센다.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import heapq
import math
from pathlib import Path
import threading
import time

from services.control.bounded_schedule import BoundedSchedule
from services.control.generation_pool import GenerationPool
from services.control.generation_session import GenerationFailure
from services.control.message_codec import with_vehicle_id
from services.control.mqtt_bridge import MQTTBridge, MQTTFailure, mqtt_destination
from services.control.operation_plan import (MAX_OPERATION_VEHICLES, MIN_PARKING_MS, FocusPlanner, OperationSettings, day_of,
                                             day_trips, trip_row, upcoming, vehicle_key)
from services.control.traffic_focus import window_at
from services.control.registry import Registry
from services.control.schedule_publisher import SchedulePublisher

RUN_ID = "ops-main"
CHECKPOINT_SECONDS = 5
INITIAL_STATE = dict(lat=37.5, lon=127.0, odometer_m=500000, coolant_c=20., heading_rad=0.)


SETTING_LABELS = {"transport": "전송 방식", "interval_ms": "Microtrip 주기", "client_count": "Client 수", "duration_cap_days": "가상 운영 기간",
                  "vehicle_count": "운영 차량 수", "daily_variation_percent": "날짜별 편차", "departure_jitter_minutes": "출발 편차",
                  "day_off_probability": "하루 미운행 확률", "weekend_activity_probability": "주말 운행 참여 확률",
                  "anomaly_events_per_hour": "이상 이벤트 후보 빈도", "anomaly_intensity": "이상 이벤트 강도", "generation_workers": "생성 Worker",
                  "traffic_focus": "운행 집중 시간대"}


def settings_diff(saved: dict, requested: dict) -> list[str]:
    """복구 운행의 당시 설정과 요청 설정이 다른 항목(화면 이름)."""
    full = {"vehicle_count": None, **saved}
    return [SETTING_LABELS.get(k, k) for k in requested if full.get(k) != requested[k]]


def client_id(index: int) -> str:
    return f"sim-{RUN_ID}-c{index + 1:03d}"


class ClientLoad:
    """배정 차량이 가장 적은 Client(같으면 ID가 앞선 것)를 고른다. min(clients, key=(차량 수, ID))와 같은 결과를
    힙으로 낸다(Client 수만큼 훑지 않음). 차량 수가 바뀐 Client는 새 항목을 넣고 낡은 항목은 꺼낼 때 버린다."""

    def __init__(self, clients):
        self.load = {c: 0 for c in clients}
        self.heap = [(0, c) for c in sorted(self.load)]

    def pick(self) -> str:
        while True:
            count, client = self.heap[0]
            if self.load[client] == count:
                break
            heapq.heappop(self.heap)  # 낡은 항목
        self.load[client] = count + 1
        heapq.heapreplace(self.heap, (count + 1, client))
        return client

    def release(self, client):
        self.load[client] -= 1
        heapq.heappush(self.heap, (self.load[client], client))


class OperationJob:
    """BoundedSchedule·GenerationPool이 읽는 실행 상태."""
    def __init__(self):
        self.id = RUN_ID
        self.stop_requested = False
        self.pause_requested = False
        self.processes = []
        self.forced = False
        self.timed_out = False


class OperationGenerator:
    """차량별 마지막 상태를 이어 운행을 하나씩 순서대로 생성한다(FleetExecution의 상시 운영판)."""
    def __init__(self, registry: Registry, worker, settings: OperationSettings, seed):
        self.registry, self.worker, self.settings, self.seed = registry, worker, settings, seed
        self.cursors = {}
        self.rows = {}
        self.by_tid = {}
        self.lock = threading.Lock()
        self.pages = 0

    def add(self, vehicle: str, info: dict):
        with self.lock:
            self.cursors[vehicle] = dict(id=info["id"], final=dict(info["final"] or INITIAL_STATE), last_end=info["last_end_ms"],
                                         key=None, initial=None, anomaly=info["fixed"]["anomaly_eligible"], finals={})

    def remember(self, row):
        with self.lock:
            self.rows[(row["vehicle_id"], row["trip_key"])] = row
            self.by_tid[(row["vehicle_id"], row["tid"])] = row

    def row_for_tid(self, vehicle, tid):
        with self.lock:
            return self.by_tid.get((vehicle, tid))

    def forget(self, vehicle, key):
        with self.lock:
            row = self.rows.pop((vehicle, key), None)
            if row:
                self.by_tid.pop((vehicle, row["tid"]), None)
            return self.cursors[vehicle]["finals"].pop(key, None)

    def stats(self):
        with self.lock:
            return dict(entries=len(self.cursors), page_requests=self.pages, mode="operations_vehicle_state")

    def generate(self, vehicle, key, offset, session=None, limit=100):
        if session is None:
            raise GenerationFailure("상시 운영 생성은 실행 전용 Worker 세션이 필요합니다.")
        session.worker_digest(self.worker)
        payload = self._payload(vehicle, key, offset, limit)
        output = session.page(payload)
        session.worker_digest(self.worker)
        return self._result(vehicle, key, offset, output)

    def generate_many(self, requests, session):
        session.worker_digest(self.worker)
        payloads = [self._payload(v, k, o, limit) for v, k, o, limit in requests]
        replies = session.batch(payloads)
        session.worker_digest(self.worker)
        return [self._result(v, k, o, reply) for (v, k, o, _), reply in zip(requests, replies)]

    def _payload(self, vehicle, key, offset, limit):
        with self.lock:
            cursor = self.cursors[vehicle]
            row = self.rows.get((vehicle, key))
        if row is None:
            raise GenerationFailure("상시 운영 계획에 없는 운행입니다.")
        a, b = row["start_ms"], row["end_ms"]
        if cursor["key"] != key:
            if offset != 0:
                raise GenerationFailure("상시 운영 운행의 페이지 순서를 확인하세요.")
            if row.get("recovered"):
                initial = dict(row["initial"])
            else:
                initial = dict(cursor["final"])
                if cursor["last_end"] is not None:
                    if a - cursor["last_end"] < 1800000:
                        raise GenerationFailure("최소 주차30분을 확인하세요.")
                    initial["coolant_c"] = 20 + (initial["coolant_c"] - 20) * math.exp(-(a - cursor["last_end"]) / 7200000)
            cursor.update(key=key, initial=initial)
        initial = cursor["initial"]
        return dict(start_ms=a, end_ms=b, from_ms=row["observed_start_ms"], until_ms=b, interval_ms=self.settings.interval_ms,
                    include_em=True, offset=offset, limit=limit, tid=row["tid"],
                    seed=int(hashlib.sha256(f"{self.seed}|{key}".encode()).hexdigest()[:7], 16), profile=row["road_profile"],
                    lat=initial["lat"], lon=initial["lon"], odometer_m=initial["odometer_m"],
                    continuity=dict(vehicle_seed=int(hashlib.sha256(f"{self.seed}|{vehicle}".encode()).hexdigest()[:7], 16),
                                    coolant_c=initial["coolant_c"], heading_rad=initial["heading_rad"]),
                    force_end=False, anomaly_eligible=cursor["anomaly"],
                    anomaly_events_per_hour=self.settings.anomaly_events_per_hour, anomaly_intensity=self.settings.anomaly_intensity)

    def _result(self, vehicle, key, offset, output):
        cursor = self.cursors[vehicle]
        final = dict(lat=output["final_lat"], lon=output["final_lon"], odometer_m=output["final_odometer_m"],
                     coolant_c=output["final_coolant_c"], heading_rad=output["final_heading_rad"])
        with self.lock:
            self.pages += 1
            cursor["finals"][key] = final
            cursor["final"] = final
            cursor["last_end"] = self.rows[(vehicle, key)]["end_ms"]
        return dict(status="generated_local_only", messages=output["messages"],
                    has_more=offset + len(output["messages"]) < output["microtrip_count"],
                    trip=output.get("trip"), synthetic_trip=output["synthetic_trip"])


class OperationController:
    """상시 운영 1개를 관리한다. 시험 실행(JOBS)과 별도 잠금·식별자를 쓴다."""
    day_seconds = 86400  # 기간 상한 계산 단위. 검사에서만 줄인다.
    def __init__(self, registry: Registry, worker, validate_message, mqtt_profile=None, clock=time.time):
        self.registry, self.worker, self.validate_message = registry, worker, validate_message
        self.mqtt_profile = mqtt_profile
        self.clock = clock
        self.lock = threading.Lock()
        self.thread = None
        self.job = None
        self.graceful = threading.Event()
        self.pause_request = threading.Event()
        self.live = {}
        self.pending = None          # 설정 변경 자동 재시작 대기(메모리 전용: 서버 재시작 시 실행하지 않음)
        self.graceful_reason = "사용자 정상 중단"
        self.recover_interrupted()

    def recover_interrupted(self):
        """서버가 실행 중에 꺼졌으면 비정상 종료로 기록한다. 자동 재시작하지 않는다."""
        op = self.registry.operation()
        if op["state"] in ("running", "pausing", "paused", "stopping"):
            self.registry.update_operation(state="stopped", last_stop="abnormal", stop_reason="서버 종료로 중단됐습니다. 다시 시작하면 확인된 지점부터 이어 보냅니다.")
            self.registry.mark_history_interrupted()

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def first_departure(self, settings: OperationSettings, wall_ms: int, planner: FocusPlanner | None = None):
        """처음 시작 때 편입할 차량들의 시작일(KST) 운행 중 가장 이른 출발 시각(집중 조정 포함). 그날 운행이 없으면 None."""
        today = day_of(wall_ms)
        seed = self.registry.seed()
        earliest = None
        selection = initial_selection(self.registry.session_vehicles(), settings.vehicle_count or MAX_OPERATION_VEHICLES)
        planner = planner or FocusPlanner(settings, seed, self.registry)
        for info in selection:
            planner.add(info["id"], info["fixed"])
        for info in selection:
            if info["start_day"] and date.fromisoformat(info["start_day"]) > today:
                continue
            floor = info["last_end_ms"] + MIN_PARKING_MS if info["last_end_ms"] is not None else 0
            for a, _, _ in day_trips(settings, seed, vehicle_key(info["id"]), info["fixed"], today, planner):
                if a >= floor:
                    earliest = a if earliest is None else min(earliest, a)
                    break
        return earliest

    def recovery(self, op=None):
        """다음 시작 때 이어 보낼 운행과 그 당시 설정. 없으면 None."""
        snapshots = self.registry.recovery_trips()
        if not snapshots:
            return None
        op = op or self.registry.operation()
        saved = snapshots[0]["trip"].get("settings", op["settings"])
        return {"trips": len(snapshots), "settings": {"vehicle_count": None, **saved} if saved else None}

    def status(self):
        op = self.registry.operation()
        live = dict(self.live) if self.running() else {}
        return {**{k: op[k] for k in ("state", "settings", "last_stop", "stop_reason", "error", "virtual_checkpoint_ms", "window_end_ms", "session_started_at")},
                "running": self.running(), "live": live, "registry": self.registry.summary(),
                "recovery": None if self.running() else self.recovery(op), "pending_settings": self.pending_status()}

    def pending_status(self):
        pending = self.pending
        if not pending:
            return None
        return {k: pending[k] for k in ("state", "changed", "reason", "requested_at")} | {"settings": pending["settings"].model_dump()}

    def apply_settings(self, settings: OperationSettings, mqtt=None):
        """실행 중 설정 변경: 정상 중단(진행 운행 마무리) 뒤 새 설정으로 자동 시작한다."""
        with self.lock:
            if not self.running():
                raise RuntimeError("실행 중인 상시 운영이 없습니다. 설정을 바꾼 뒤 운영 시작을 누르세요.")
            if self.job.stop_requested or self.live.get("stopping"):
                raise RuntimeError("이미 중단 중입니다. 중단이 끝난 뒤 새 설정으로 시작하세요.")
            if self.pending:
                raise RuntimeError("이미 설정 변경을 기다리는 중입니다. 취소한 뒤 다시 요청하세요.")
            current = self.registry.operation()["settings"] or {}
            changed = settings_diff(current, settings.model_dump())
            if not changed:
                raise RuntimeError("현재 운영 설정과 같습니다.")
            self.pending = {"settings": settings, "mqtt": mqtt, "changed": changed, "state": "waiting", "reason": None,
                            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            thread = self.thread
            self.graceful_reason = "설정 변경 적용을 위한 정상 중단"
            self.graceful.set()
        self.registry.add_event("settings_change", {"changed": changed})
        threading.Thread(target=self._restart_after, args=(thread,), name="operation-restart", daemon=True).start()

    def _restart_after(self, thread):
        thread.join()
        with self.lock:
            pending = self.pending
            if not pending or pending["state"] != "waiting":
                return
            if self.registry.operation()["last_stop"] != "graceful":
                pending.update(state="blocked", reason="정상 중단으로 끝나지 않아(강제 중단·오류) 자동 시작하지 않았습니다. "
                                                      "남은 운행을 당시 설정으로 먼저 이어 보낸 뒤 새 설정을 적용하세요.")
                self.registry.add_event("settings_restart_blocked", {"reason": pending["reason"]})
                return
            pending["state"] = "starting"
        try:
            self.start(pending["settings"], pending["mqtt"], from_restart=True)
        except Exception as error:  # 시작 거절 사유를 그대로 보여 준다.
            with self.lock:
                pending.update(state="blocked", reason=f"자동 시작 실패: {error}")
            self.registry.add_event("settings_restart_blocked", {"reason": pending["reason"]})
            return
        with self.lock:
            if self.pending is pending:
                self.pending = None
        self.registry.add_event("settings_restarted", {"changed": pending["changed"]})

    def cancel_pending(self):
        with self.lock:
            if not self.pending or self.pending["state"] == "starting":
                raise RuntimeError("취소할 설정 변경 대기가 없습니다.")
            self.pending = None
        self.registry.add_event("settings_change_cancelled", {})

    def start(self, settings: OperationSettings, mqtt=None, from_restart=False):
        with self.lock:
            if self.running():
                raise RuntimeError("상시 운영이 이미 실행 중입니다.")
            if not from_restart and self.pending:
                if self.pending["state"] == "waiting":
                    raise RuntimeError("설정 변경 자동 재시작을 기다리는 중입니다.")
                self.pending = None  # 막혀 있던 대기는 사용자가 직접 시작하면 정리한다.
            previous = self.registry.operation()
            snapshots = self.registry.recovery_trips()
            requested_settings = settings.model_dump()
            worker_hash = hashlib.sha256(Path(self.worker).read_bytes()).hexdigest()
            destination = mqtt_destination(mqtt)
            for vehicle in snapshots:
                trip = vehicle["trip"]
                saved_settings = trip.get("settings", previous["settings"])
                if not saved_settings:
                    raise RuntimeError(f"복구 대기 운행 {len(snapshots):,}건의 당시 설정 기록이 없어 이어 보낼 수 없습니다.")
                different = settings_diff(saved_settings, requested_settings)
                if different:
                    raise RuntimeError(f"복구 대기 운행 {len(snapshots):,}건은 비정상 종료 때 보내던 운행이라 당시 설정으로만 이어 보낼 수 있습니다. "
                                       f"다른 항목: {', '.join(different)}. 화면의 '당시 설정 불러오기'로 맞춘 뒤 시작하세요.")
                if trip.get("worker_hash", worker_hash) != worker_hash or trip.get("alias", vehicle["pseudonym"]) != vehicle["pseudonym"] or trip.get("destination", destination) != destination:
                    raise RuntimeError("복구할 운행의 Worker, 차량 식별자 또는 전송 대상이 바뀌었습니다. 기존 구성으로 복구하세요.")
            self.graceful.clear()
            self.pause_request.clear()
            self.graceful_reason = "사용자 정상 중단"
            self.job = OperationJob()
            self.live = {"state": "starting"}
            self.thread = threading.Thread(target=self._run, args=(settings, mqtt, self.job, worker_hash), name="operation", daemon=True)
            self.registry.update_operation(state="running", settings=settings.model_dump(), error=None)
            self.thread.start()

    def pause(self, pause: bool):
        with self.lock:
            if not self.running() or self.graceful.is_set() or self.job.stop_requested:
                raise RuntimeError("정상 운영 중에 일시중지하거나 재개할 수 있습니다.")
            if pause:
                self.pause_request.set()
            else:
                self.pause_request.clear()

    def stop(self, force=False):
        if not self.running():
            raise RuntimeError("실행 중인 상시 운영이 없습니다.")
        if force:
            self.job.stop_requested = True
        else:
            self.graceful.set()

    def wait(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)
        return not self.running()

    # ---- 실행 ----
    def _run(self, settings: OperationSettings, mqtt, job: OperationJob, worker_hash):
        registry = self.registry
        op = registry.operation()
        recovering = op["last_stop"] == "abnormal" and op["virtual_checkpoint_ms"] is not None
        wall = int(self.clock() * 1000)
        fresh = op["virtual_checkpoint_ms"] is None
        anchor = wall if fresh else int(op["virtual_checkpoint_ms"])
        planner = FocusPlanner(settings, registry.seed(), registry)
        if fresh:
            # 처음 시작: 시작일(실제 날짜, KST)의 가장 이른 출발 시각부터 가상시각을 시작해 그날 운행을 모두 보낸다.
            anchor = self.first_departure(settings, wall, planner) or wall
        window_end = op["window_end_ms"]
        if window_end is None or anchor >= window_end:
            window_end = anchor + int(settings.duration_cap_days * self.day_seconds * 1000)
        started = time.monotonic()
        seed = registry.seed()
        result = {"processed_messages": 0, "payload_bytes": 0, "samples": [], "trip_count": 0, "microtrip_count": 0,
                  "confirmed_messages": 0, "simulation_time_ms": 0, "recovered_trips": 0, "duplicate_risk_trips": 0}
        live = self.live = {"state": "running", "recovering": recovering, "virtual_anchor_ms": anchor, "result": result,
                            "vehicles": 0, "virtual_end_ms": window_end, "stopping": False, "stop_reason": None}
        session_id = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        registry.update_operation(state="running", last_stop=None, stop_reason=None, session_started_at=session_id,
                                  virtual_checkpoint_ms=anchor, window_end_ms=window_end)
        registry.add_event("start", {"recovering": recovering, "virtual_anchor_ms": anchor, "transport": settings.transport})
        generator = OperationGenerator(registry, self.worker, settings, seed)
        vstates = {}                # 내부 차량키 → 실행 상태
        clients = [client_id(i) for i in range(settings.client_count)]
        load = ClientLoad(clients)
        client_by_alias = {}
        progress = {}               # 차량 id → 마지막 확인 ts (체크포인트 대기)
        mqtt_mode = settings.transport == "mqtt"
        bridge = publisher = generation = bounded = None
        restart = {"flag": False}
        last_checkpoint = [time.monotonic()]
        vehicle_limit = settings.vehicle_count or MAX_OPERATION_VEHICLES
        paused_at = None
        paused_seconds = 0.0
        destination = mqtt_destination(mqtt)
        settings_snapshot = settings.model_dump()

        def active_seconds():
            now = time.monotonic() if paused_at is None else paused_at
            return now - started - paused_seconds

        def virtual_now():
            return anchor + int(active_seconds() * 1000)

        def provider(vehicle):
            vs = vstates[vehicle]
            info = vs["info"]
            last_end, not_before = info["last_end_ms"], anchor
            trip = info["trip"]
            if trip:
                # 비정상 종료 때 진행 중이던 운행: 같은 운행을 확인된 메시지 다음부터 다시 보낸다.
                row = trip_row(vehicle, trip["a"], trip["b"], trip["profile"], settings.interval_ms, resume_after_ms=info["confirmed_ts"])
                if row["trip_key"] != trip["key"]:
                    raise GenerationFailure("복구할 운행 키가 일치하지 않습니다.")
                row.update(recovered=True, initial=trip["initial"])
                generator.remember(row)
                # 복구 운행은 이미 수신 측에 일부가 갔으므로 정상 중단 때도 끝까지 보낸다.
                vs["active_key"] = row["trip_key"]
                result["recovered_trips"] += 1
                result["duplicate_risk_trips"] += 1
                recovery = (info["id"], trip["key"], info["confirmed_ts"])
                if deferred_recovery is not None:
                    deferred_recovery.append(recovery)  # 시작 단계: 첫 메시지 전에 묶어서 확정한다
                else:
                    registry.recover_trips([recovery])
                yield row
                last_end, not_before = trip["b"], trip["b"]
            for row in upcoming(settings, seed, info["id"], info["fixed"], day_of_iso(vs["start_day"]), not_before, last_end, planner):
                if vs["final"] or vs["removing"] or row["start_ms"] >= window_end:
                    return
                if row["end_ms"] > window_end:
                    continue
                generator.remember(row)
                yield row

        def first_day(start_day_ms, same_day=False):
            return (day_of(start_day_ms) + timedelta(days=0 if same_day else 1)).isoformat()

        def include(info, start_day_ms, same_day=False, day=None):
            """운영 중 편입 차량은 다음 가상 날짜부터, 처음 시작 때 들어가는 차량은 시작일부터 운행한다.
            day: 시작 단계에서 묶어 정한 첫 운행 날짜(없으면 한 대씩 정한다)."""
            vehicle = vehicle_key(info["id"])
            day = day or registry.ensure_start_day(info["id"], first_day(start_day_ms, same_day))
            client = load.pick()
            client_by_alias[info["pseudonym"]] = client
            vstates[vehicle] = dict(info=info, alias=info["pseudonym"], client=client, start_day=day, active_key=None,
                                    final=False, removing=info["status"] == "removing", pending_end=None)
            generator.add(vehicle, info)
            planner.add(info["id"], info["fixed"])
            return vehicle

        def confirm(vehicle, ty, ts):
            vs = vstates[vehicle]
            result["confirmed_messages"] += 1
            progress[vs["info"]["id"]] = ts
            if ty == 3:
                end, final = vs["pending_end"]
                registry.save_trip_end(vs["info"]["id"], end, final)
                vs["pending_end"] = None
                progress.pop(vs["info"]["id"], None)
                if vs["removing"] and vs["active_key"] is None:
                    removals.append(vehicle)

        def on_confirm(command, fields):
            if command != "publish":
                return
            message = fields["message"]
            vehicle = alias_to_vehicle[fields["vehicle_id"]]
            confirm(vehicle, message_ty.pop(id(message)), message["ts"])

        alias_to_vehicle = {}
        message_ty = {}
        deferred_recovery = []      # 시작 단계 복구 기록. 생성 준비 뒤 첫 메시지 전에 확정하고 이후에는 None(즉시 기록).
        removals = []               # PUBACK 처리 중에는 대기열을 바꾸지 않고 gate에서 정리한다.

        def finish_removal(vehicle):
            vs = vstates[vehicle]
            if vs.get("removed"):
                return
            retire_now(vehicle)
            vs["removed"] = True
            live["vehicles"] -= 1
            if mqtt_mode:
                publisher.enqueue("detach", hard_gate, route=vs["client"], vehicle_id=vs["alias"])
            load.release(vs["client"])
            registry.set_status([vs["info"]["id"]], "removed", only_from=("removing", "driving"))
            registry.add_event("removed", {"vehicle_id": vs["info"]["id"]})

        def retire_now(vehicle):
            """미시작 운행을 버려도 전송 확인 중인 종료 Trip은 보존한다."""
            bounded.retire(vehicle)

        def begin_graceful(reason):
            if live["stopping"]:
                return
            live["stopping"] = True
            live["stop_reason"] = reason
            live["state"] = "stopping"
            registry.update_operation(state="stopping", stop_reason=reason)
            registry.add_event("stopping", {"reason": reason})
            for vehicle, vs in vstates.items():
                vs["final"] = True
                if vs["active_key"] is None:
                    retire_now(vehicle)
            restart["flag"] = True

        def poll_registry():
            """새로 등록된 차량을 다음 날부터 넣고, 제외 요청 차량을 정리한다."""
            known = {vs["info"]["id"]: v for v, vs in vstates.items()}
            for info in registry.session_vehicles():
                vehicle = known.get(info["id"])
                if vehicle is None and info["status"] == "ready" and not live["stopping"] and live["vehicles"] < vehicle_limit:
                    vehicle = include(info, virtual_now())
                    alias_to_vehicle[info["pseudonym"]] = vehicle
                    registry.set_status([info["id"]], "driving", only_from=("ready",))
                    if mqtt_mode:
                        publisher.enqueue("attach", hard_gate, route=vstates[vehicle]["client"], vehicle_id=info["pseudonym"], client_id=vstates[vehicle]["client"])
                    bounded.add(vehicle)
                    live["vehicles"] += 1
                    restart["flag"] = True
                elif vehicle is not None and info["status"] == "removing" and not vstates[vehicle]["removing"]:
                    vs = vstates[vehicle]
                    vs["removing"] = True
                    if vs["active_key"] is None:
                        retire_now(vehicle)
                        if vs["pending_end"] is None:
                            finish_removal(vehicle)
                    restart["flag"] = True

        def checkpoint(force=False, state=None):
            if not force and time.monotonic() - last_checkpoint[0] < CHECKPOINT_SECONDS:
                return
            last_checkpoint[0] = time.monotonic()
            current = min(window_end, virtual_now())
            # 생성이 밀렸다면 아직 시작하지 않은 운행을 건너뛰지 않는 위치에 보관한다.
            head = bounded.peek(bounded.heads) if bounded else None
            queued_due = head["due"] if head else current
            registry.save_checkpoint(max(anchor, min(current, queued_due)), progress, state)
            progress.clear()
            live["virtual_now_ms"] = current
            live["driving_now"] = sum(1 for vs in vstates.values() if vs["active_key"] and not vs.get("removed"))
            live["focus"] = None
            if settings.traffic_focus is not None:
                today = day_of(current)
                report = planner.cache.get(today.isoformat())
                item, planned = window_at(report, current, today) if report else (None, None)
                if item:
                    live["focus"] = {"start": item["start"], "end": item["end"], "target_percent": item["target_percent"],
                                     "planned_percent": planned, "window_planned_percent": item["planned_percent"],
                                     "actual_percent": round(live["driving_now"] * 100 / max(1, live["vehicles"]), 1)}
            if bridge:
                live["mqtt"] = {k: bridge.stats.get(k) for k in ("connected_clients", "active_clients", "publish_attempts", "puback_count", "unconfirmed_messages", "reconnect_count")}
            if not force:
                poll_registry()

        def wait_if_paused():
            nonlocal paused_at, paused_seconds
            while (self.pause_request.is_set() and not self.graceful.is_set()
                   and not live["stopping"] and not job.stop_requested):
                if paused_at is None:
                    paused_at = time.monotonic()
                    live["state"] = "pausing"
                    checkpoint(force=True, state="pausing")
                if publisher:
                    publisher.pump(allow_start=False, elapsed_ms=active_seconds() * 1000,
                                   origin_ms=wall + paused_seconds * 1000)
                if live["state"] != "paused" and (not publisher or not publisher.active):
                    checkpoint(force=True, state="paused")
                    live["state"] = "paused"
                    registry.add_event("paused", {"virtual_ms": live["virtual_now_ms"]})
                time.sleep(.05)
            if paused_at is not None and not job.stop_requested:
                paused_seconds += time.monotonic() - paused_at
                paused_at = None
                registry.update_operation(state="running")
                live["state"] = "running"
                registry.add_event("resumed", {"virtual_ms": virtual_now()})

        def hard_gate(due=None):
            """중단(강제) 때만 False. 송신 대기열 백프레셔에서 메시지를 잃지 않게 한다."""
            while True:
                wait_if_paused()
                if job.stop_requested:
                    return False
                if publisher:
                    publisher.pump(elapsed_ms=active_seconds() * 1000, origin_ms=wall + paused_seconds * 1000)
                if due is None or active_seconds() * 1000 >= due:
                    return True
                time.sleep(min(.05, max(0, due / 1000 - active_seconds())))

        def gate(due=None):
            while True:
                wait_if_paused()
                elapsed = active_seconds()
                result["simulation_time_ms"] = int(elapsed * 1000)
                if job.stop_requested:
                    return False
                if self.graceful.is_set() and not live["stopping"]:
                    begin_graceful(self.graceful_reason)
                if virtual_now() >= window_end and not live["stopping"]:
                    begin_graceful(f"기간 상한 {settings.duration_cap_days}일 도달")
                if publisher:
                    publisher.pump(elapsed_ms=elapsed * 1000, origin_ms=wall + paused_seconds * 1000)
                while removals:
                    finish_removal(removals.pop(0))
                checkpoint()
                if restart["flag"]:
                    restart["flag"] = False
                    return False
                if due is None or elapsed * 1000 >= due:
                    return True
                time.sleep(min(.05, max(0, (due - elapsed * 1000) / 1000)))

        def consume(timestamp, vehicle, message, synthetic):
            if synthetic:
                raise GenerationFailure("상시 운영은 시험용 Trip을 보내지 않습니다.")
            vs = vstates[vehicle]
            row = generator.row_for_tid(vehicle, message["pld"]["tid"])
            if row is None:
                raise GenerationFailure("운행 번호를 확인하지 못했습니다.")
            ty = message["ty"]
            if vs["active_key"] != row["trip_key"]:
                # 차량별로 이전 Trip ACK가 끝나기 전에 새 운행 복구 정보를 덮어쓰지 않는다.
                while vs["pending_end"] is not None:
                    if not hard_gate():
                        return
                    time.sleep(.01)
                if vs["final"] or vs["removing"]:
                    return
                initial = generator.cursors[vehicle]["initial"]
                registry.save_trip_start(vs["info"]["id"], {
                    "key": row["trip_key"], "a": row["start_ms"], "b": row["end_ms"],
                    "profile": row["road_profile"], "initial": initial, "worker_hash": worker_hash,
                    "alias": vs["alias"], "destination": destination, "settings": settings_snapshot,
                }, session=session_id)
                vs["active_key"] = row["trip_key"]
            if ty == 3:
                vs["active_key"] = None
                vs["pending_end"] = (row["end_ms"], generator.forget(vehicle, row["trip_key"]))
                result["trip_count"] += 1
            else:
                vs["active_key"] = row["trip_key"]
                result["microtrip_count"] += 1
            wire = with_vehicle_id(vs["alias"], self.validate_message(message))
            result["processed_messages"] += 1
            result["payload_bytes"] += wire.size
            if len(result["samples"]) < 10:
                result["samples"].append({"vehicle_alias": vs["alias"], "client_id": vs["client"], "topic": f"{(mqtt or {}).get('topic_prefix', 'sim')}/{RUN_ID}/{vs['alias']}/obd", "message": wire.text[:2000]})
            if mqtt_mode:
                message_ty[id(wire)] = ty
                if not publisher.enqueue("publish", hard_gate, vehicle_id=vs["alias"], message=wire):
                    message_ty.pop(id(wire), None)
                    return
            else:
                confirm(vehicle, ty, message["ts"])

        state, error = "stopped", None
        try:
            for info in registry.session_vehicles():
                if info["status"] == "removing" and not info["trip"]:
                    registry.set_status([info["id"]], "removed", only_from=("removing",))
                    registry.add_event("removed", {"vehicle_id": info["id"]})
            selected = initial_selection(registry.session_vehicles(), vehicle_limit)
            days = registry.ensure_start_days([(info["id"], first_day(anchor, fresh)) for info in selected])
            for info in selected:
                vehicle = include(info, anchor, same_day=fresh, day=days[info["id"]])
                alias_to_vehicle[info["pseudonym"]] = vehicle
            registry.set_status([vs["info"]["id"] for vs in vstates.values()], "driving", only_from=("ready",))
            live["vehicles"] = len(vstates)
            data = {"execution_profile": "operations", "realtime": True, "config": {"interval_ms": settings.interval_ms},
                    "assignments": [{"vehicle_id": v} for v in vstates], "trip_provider": provider,
                    "generate": generator.generate, "generate_batch": generator.generate_many,
                    "due_offset": lambda ts: ts - anchor, "measurement_start_ms": anchor,
                    "generation_queue_messages": 2000, "generation_queue_bytes": 8388608}
            generation = GenerationPool(self.worker, job, _Manager(), settings.generation_workers)
            if mqtt_mode:
                if not mqtt:
                    raise MQTTFailure("상시 운영 MQTT 서버 설정이 없습니다.")
                bridge_data = {"mqtt": mqtt, "persistent": True, "clients": clients,
                               "assignments": [{"vehicle_id": vs["alias"], "client_id": vs["client"]} for vs in vstates.values()]}
                bridge = MQTTBridge(self.worker, bridge_data, job)
                bridge.receive()
                publisher = SchedulePublisher(bridge, result, client_by_alias, anchor, due_offset=lambda ts: ts - anchor,
                                              queue_capacity=min(4096, max(100, len(clients) * 2)), queue_bytes=8388608,
                                              clients=clients, on_confirm=on_confirm)
                for client in clients:
                    if not hard_gate():
                        break
                    publisher.enqueue("connect", hard_gate, client_id=client)
            bounded = BoundedSchedule(data, generation, gate, result)
            # 진행 중이던 운행의 복구 기록: 검사·생성 준비가 끝난 뒤, 첫 메시지를 보내기 전에 묶어서 확정한다.
            pending_recovery, deferred_recovery = deferred_recovery, None
            registry.recover_trips(pending_recovery)
            while not job.stop_requested:
                for timestamp, vehicle, message, synthetic in bounded:
                    consume(timestamp, vehicle, message, synthetic)
                if job.stop_requested:
                    break
                if live["stopping"] and bounded.peek(bounded.heads) is None:
                    break
                if bounded.peek(bounded.heads) is None and not gate(None):
                    continue
                if bounded.peek(bounded.heads) is None:
                    time.sleep(.2)
            while removals and not job.stop_requested:
                finish_removal(removals.pop(0))
            if publisher and not job.stop_requested:
                publisher.drain(hard_gate)
            if job.stop_requested:
                state = "stopped"
        except Exception as exc:  # 오류 문구에는 VIN이 없도록 종류와 제품 문구만 남긴다.
            error = str(exc) if isinstance(exc, (MQTTFailure, GenerationFailure)) else f"상시 운영 실행 오류({type(exc).__name__})"
        finally:
            abnormal = bool(job.stop_requested or error)
            if bridge:
                bridge.close(abort=abnormal)
                if not abnormal and (not bridge.cleaned or not bridge.close_acknowledged):
                    abnormal, error = True, "MQTT 정상 종료 응답 또는 연결 정리를 확인하지 못했습니다."
                if publisher:
                    publisher.discard()
            if generation:
                generation.close(abort=abnormal)
            checkpoint(force=True)
            if bounded:
                bounded.close()
            self._settle(vstates, abnormal, error, live)

    def _settle(self, vstates, abnormal, error, live):
        registry = self.registry
        ids = [vs["info"]["id"] for vs in vstates.values() if not vs.get("removed")]
        if abnormal:
            # 진행 중 운행·확인 시각은 남겨 다음 시작 때 이어 보낸다. 제외 중이던 차량은 정리 보류.
            holds = [vs["info"]["id"] for vs in vstates.values() if vs["removing"] and not vs.get("removed")]
            registry.set_status(holds, "cleanup_hold", only_from=("removing",))
            registry.mark_history_interrupted()
            registry.update_operation(state="stopped", last_stop="abnormal", error=error,
                                      stop_reason="강제 중단" if not error else "오류로 중단")
            registry.add_event("abnormal_stop", {"error": error, "cleanup_hold": len(holds)})
        else:
            registry.set_status(ids, "ready", only_from=("driving",))
            registry.update_operation(state="stopped", last_stop="graceful", error=None, stop_reason=live.get("stop_reason") or "정상 중단")
            registry.add_event("graceful_stop", {"reason": live.get("stop_reason")})
        live["state"] = "stopped"


def initial_selection(infos, limit):
    """운영 시작 때 편입할 차량: 복구 운행 우선, 그다음 등록 순서로 limit대(제외 대기 차량 제외)."""
    chosen = []
    for info in sorted(infos, key=lambda info: (not bool(info["trip"]), info["status"] != "driving", info["id"])):
        if info["status"] == "removing" and not info["trip"]:
            continue
        if len(chosen) >= limit and not info["trip"]:
            continue
        chosen.append(info)
    return chosen


class _Manager:
    def __init__(self):
        self.lock = threading.Lock()


def day_of_iso(value: str):
    from datetime import date
    return date.fromisoformat(value)
