"""상시 운영 실행기. 명부 차량을 연속 가상시계로 운행시키고 가명 VIN으로 전송한다.

- 토픽 `{prefix}/ops-main/{가명VIN}/obd`, Client `sim-ops-main-cNNN`, 본문 `{vehicleId, ty, ts, pld}`.
- 정상 중단: 진행 중 운행은 끝까지 보내고 아직 시작하지 않은 운행은 보내지 않는다.
- 강제 중단·오류: 운행 시작 상태와 마지막 PUBACK 확인 시각으로 다음 시작 때 같은 운행을 다시 만들어
  확인된 메시지 다음부터 이어 보낸다(미확인 구간은 동일 내용 중복 가능, 기록).
- 기간 상한(실제 경과 시간)에 도달하면 자동으로 정상 중단한다. 재개하면 다시 0부터 센다.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import math
import threading
import time

from services.control.bounded_schedule import BoundedSchedule
from services.control.generation_pool import GenerationPool
from services.control.generation_session import GenerationFailure
from services.control.message_codec import WireMessage
from services.control.mqtt_bridge import MQTTBridge, MQTTFailure
from services.control.operation_plan import OperationSettings, day_of, trip_row, upcoming, vehicle_key
from services.control.registry import Registry
from services.control.schedule_publisher import SchedulePublisher

RUN_ID = "ops-main"
CHECKPOINT_SECONDS = 5
INITIAL_STATE = dict(lat=37.5, lon=127.0, odometer_m=500000, coolant_c=20., heading_rad=0.)


def client_id(index: int) -> str:
    return f"sim-{RUN_ID}-c{index + 1:03d}"


from services.control.message_codec import with_vehicle_id  # noqa: E402  (공용 봉투)


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
        self.retired = set()

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
                # 첫 메시지 전송 전에 운행 시작 상태를 저장한다. 비정상 종료 후 같은 운행을 다시 만든다.
                # 버린(retire) 차량은 저장하지 않는다. 잠금 안에서 확인해 retire와 순서를 맞춘다.
                with self.lock:
                    if vehicle not in self.retired:
                        self.registry.save_trip_start(cursor["id"], {"key": key, "a": a, "b": b, "profile": row["road_profile"], "initial": initial})
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
        self.live = {}
        self.recover_interrupted()

    def recover_interrupted(self):
        """서버가 실행 중에 꺼졌으면 비정상 종료로 기록한다. 자동 재시작하지 않는다."""
        op = self.registry.operation()
        if op["state"] in ("running", "stopping"):
            self.registry.update_operation(state="stopped", last_stop="abnormal", stop_reason="서버 종료로 중단됐습니다. 다시 시작하면 확인된 지점부터 이어 보냅니다.")

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def status(self):
        op = self.registry.operation()
        live = dict(self.live) if self.running() else {}
        return {**{k: op[k] for k in ("state", "settings", "last_stop", "stop_reason", "error", "virtual_checkpoint_ms", "session_started_at")},
                "running": self.running(), "live": live, "registry": self.registry.summary()}

    def start(self, settings: OperationSettings, mqtt=None):
        with self.lock:
            if self.running():
                raise RuntimeError("상시 운영이 이미 실행 중입니다.")
            self.graceful.clear()
            self.job = OperationJob()
            self.live = {"state": "starting"}
            self.thread = threading.Thread(target=self._run, args=(settings, mqtt, self.job), name="operation", daemon=True)
            self.registry.update_operation(state="running", settings=settings.model_dump(), error=None)
            self.thread.start()

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
    def _run(self, settings: OperationSettings, mqtt, job: OperationJob):
        registry = self.registry
        op = registry.operation()
        recovering = op["last_stop"] == "abnormal" and op["virtual_checkpoint_ms"] is not None
        wall = int(self.clock() * 1000)
        anchor = int(op["virtual_checkpoint_ms"]) if recovering else wall
        started = time.monotonic()
        seed = registry.seed()
        result = {"processed_messages": 0, "payload_bytes": 0, "samples": [], "trip_count": 0, "microtrip_count": 0,
                  "confirmed_messages": 0, "simulation_time_ms": 0, "recovered_trips": 0, "duplicate_risk_trips": 0}
        live = self.live = {"state": "running", "recovering": recovering, "virtual_anchor_ms": anchor, "result": result,
                            "vehicles": 0, "stopping": False, "stop_reason": None}
        registry.update_operation(state="running", last_stop=None, stop_reason=None, session_started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  virtual_checkpoint_ms=anchor)
        registry.add_event("start", {"recovering": recovering, "virtual_anchor_ms": anchor, "transport": settings.transport})
        generator = OperationGenerator(registry, self.worker, settings, seed)
        vstates = {}                # 내부 차량키 → 실행 상태
        clients = [client_id(i) for i in range(settings.client_count)]
        load = {c: 0 for c in clients}
        client_by_alias = {}
        progress = {}               # 차량 id → 마지막 확인 ts (체크포인트 대기)
        mqtt_mode = settings.transport == "mqtt"
        bridge = publisher = generation = bounded = None
        restart = {"flag": False}
        last_checkpoint = [time.monotonic()]
        cap_seconds = settings.duration_cap_days * self.day_seconds

        def virtual_now():
            return anchor + int((time.monotonic() - started) * 1000)

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
                registry.add_event("recover_trip", {"vehicle_id": info["id"], "trip_key": trip["key"], "confirmed_ts": info["confirmed_ts"]})
                yield row
                last_end, not_before = trip["b"], trip["b"]
            for row in upcoming(settings, seed, info["id"], info["fixed"], day_of_iso(vs["start_day"]), not_before, last_end):
                if vs["final"] or vs["removing"]:
                    return
                generator.remember(row)
                yield row

        def include(info, start_day_ms):
            vehicle = vehicle_key(info["id"])
            day = registry.ensure_start_day(info["id"], (day_of(start_day_ms) + timedelta(days=1)).isoformat())
            client = min(clients, key=lambda c: (load[c], c))
            load[client] += 1
            client_by_alias[info["pseudonym"]] = client
            vstates[vehicle] = dict(info=info, alias=info["pseudonym"], client=client, start_day=day, active_key=None,
                                    final=False, removing=info["status"] == "removing", pending_end=[])
            generator.add(vehicle, info)
            return vehicle

        def confirm(vehicle, ty, ts, tid):
            vs = vstates[vehicle]
            result["confirmed_messages"] += 1
            progress[vs["info"]["id"]] = ts
            if ty == 3:
                key, end, final = vs["pending_end"].pop(0)
                registry.save_trip_end(vs["info"]["id"], end, final)
                progress.pop(vs["info"]["id"], None)
                if vs["removing"] and vs["active_key"] is None:
                    removals.append(vehicle)

        def on_confirm(command, fields):
            if command != "publish":
                return
            message = fields["message"]
            vehicle = alias_to_vehicle[fields["vehicle_id"]]
            confirm(vehicle, message_ty[id(message)], message["ts"], message_tid.pop(id(message)))
            message_ty.pop(id(message), None)

        alias_to_vehicle = {}
        message_ty, message_tid = {}, {}
        removals = []               # PUBACK 처리 중에는 대기열을 바꾸지 않고 gate에서 정리한다.

        def finish_removal(vehicle):
            vs = vstates[vehicle]
            if vs.get("removed"):
                return
            vs["removed"] = True
            if mqtt_mode:
                publisher.enqueue("detach", hard_gate, route=vs["client"], vehicle_id=vs["alias"])
            load[vs["client"]] -= 1
            registry.set_status([vs["info"]["id"]], "removed", only_from=("removing", "driving"))
            registry.add_event("removed", {"vehicle_id": vs["info"]["id"]})

        def retire_now(vehicle):
            """아직 메시지를 보내지 않은 운행을 버린다. 미리 저장한 운행 시작 기록은 지운다."""
            vs = vstates[vehicle]
            with generator.lock:
                generator.retired.add(vehicle)
            bounded.retire(vehicle)
            registry.clear_trip(vs["info"]["id"])

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
                if vehicle is None and info["status"] == "ready" and not live["stopping"]:
                    vehicle = include(info, virtual_now())
                    alias_to_vehicle[info["pseudonym"]] = vehicle
                    registry.set_status([info["id"]], "driving", only_from=("ready",))
                    if mqtt_mode:
                        publisher.enqueue("attach", hard_gate, route=vstates[vehicle]["client"], vehicle_id=info["pseudonym"], client_id=vstates[vehicle]["client"])
                    bounded.add(vehicle)
                    live["vehicles"] = len(vstates)
                    restart["flag"] = True
                elif vehicle is not None and info["status"] == "removing" and not vstates[vehicle]["removing"]:
                    vs = vstates[vehicle]
                    vs["removing"] = True
                    if vs["active_key"] is None:
                        retire_now(vehicle)
                        finish_removal(vehicle)
                    restart["flag"] = True

        def checkpoint(force=False):
            if not force and time.monotonic() - last_checkpoint[0] < CHECKPOINT_SECONDS:
                return
            last_checkpoint[0] = time.monotonic()
            pending = dict(progress)
            progress.clear()
            registry.save_confirmed(pending)
            registry.update_operation(virtual_checkpoint_ms=virtual_now())
            live["virtual_now_ms"] = virtual_now()
            if bridge:
                live["mqtt"] = {k: bridge.stats.get(k) for k in ("connected_clients", "active_clients", "publish_attempts", "puback_count", "unconfirmed_messages", "reconnect_count")}
            if not force:
                poll_registry()

        def hard_gate(due=None):
            """중단(강제) 때만 False. 송신 대기열 백프레셔에서 메시지를 잃지 않게 한다."""
            while True:
                if job.stop_requested:
                    return False
                if publisher:
                    publisher.pump(allow_start=True, elapsed_ms=(time.monotonic() - started) * 1000, origin_ms=wall)
                if due is None or (time.monotonic() - started) * 1000 >= due:
                    return True
                time.sleep(min(.05, max(0, (due - (time.monotonic() - started) * 1000) / 1000)))

        def gate(due=None):
            while True:
                elapsed = time.monotonic() - started
                result["simulation_time_ms"] = int(elapsed * 1000)
                if job.stop_requested:
                    return False
                if self.graceful.is_set() and not live["stopping"]:
                    begin_graceful("사용자 정상 중단")
                if elapsed >= cap_seconds and not live["stopping"]:
                    begin_graceful(f"기간 상한 {settings.duration_cap_days}일 도달")
                if publisher:
                    publisher.pump(allow_start=True, elapsed_ms=elapsed * 1000, origin_ms=wall)
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
            if ty == 3:
                vs["active_key"] = None
                vs["pending_end"].append((row["trip_key"], row["end_ms"], generator.forget(vehicle, row["trip_key"])))
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
                message_tid[id(wire)] = message["pld"]["tid"]
                if not publisher.enqueue("publish", hard_gate, vehicle_id=vs["alias"], message=wire):
                    message_ty.pop(id(wire), None)
                    message_tid.pop(id(wire), None)
                    return
            else:
                confirm(vehicle, ty, message["ts"], message["pld"]["tid"])

        state, error = "stopped", None
        try:
            infos = registry.session_vehicles()
            for info in infos:
                if info["status"] == "removing" and not info["trip"]:
                    registry.set_status([info["id"]], "removed", only_from=("removing",))
                    registry.add_event("removed", {"vehicle_id": info["id"]})
                    continue
                vehicle = include(info, anchor)
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
            if bounded:
                bounded.close()
            checkpoint(force=True)
            self._settle(vstates, abnormal, error, live)

    def _settle(self, vstates, abnormal, error, live):
        registry = self.registry
        ids = [vs["info"]["id"] for vs in vstates.values() if not vs.get("removed")]
        if abnormal:
            # 진행 중 운행·확인 시각은 남겨 다음 시작 때 이어 보낸다. 제외 중이던 차량은 정리 보류.
            holds = [vs["info"]["id"] for vs in vstates.values() if vs["removing"] and not vs.get("removed")]
            registry.set_status(holds, "cleanup_hold", only_from=("removing",))
            registry.update_operation(state="stopped", last_stop="abnormal", error=error,
                                      stop_reason="강제 중단" if not error else "오류로 중단")
            registry.add_event("abnormal_stop", {"error": error, "cleanup_hold": len(holds)})
        else:
            registry.set_status(ids, "ready", only_from=("driving",))
            registry.update_operation(state="stopped", last_stop="graceful", error=None, stop_reason=live.get("stop_reason") or "정상 중단")
            registry.add_event("graceful_stop", {"reason": live.get("stop_reason")})
        live["state"] = "stopped"


class _Manager:
    def __init__(self):
        self.lock = threading.Lock()


def day_of_iso(value: str):
    from datetime import date
    return date.fromisoformat(value)
