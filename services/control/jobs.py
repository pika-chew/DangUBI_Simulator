"""단일 제어 API 프로세스의 제한된 Worker 실행 관리."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from fastapi import HTTPException

ACTIVE = {"starting", "running", "pausing", "paused", "resuming", "stopping"}


@dataclass
class Job:
    id: str
    result: dict
    mqtt: bool
    process: subprocess.Popen | None = None
    stop_requested: bool = False
    pause_requested: bool = False
    wall_limit_seconds: float = 0
    timed_out: bool = False
    forced: bool = False
    thread: threading.Thread | None = None
    force_timer: threading.Timer | None = None
    finished: threading.Event = field(default_factory=threading.Event)


class JobManager:
    def __init__(self, database: Callable, mqtt_lock: threading.Lock):
        self.database = database
        self.mqtt_lock = mqtt_lock
        self.lock = threading.RLock()
        self.active: Job | None = None
        self.closing = False

    def active_id(self):
        with self.lock:
            return self.active.id if self.active else None

    def save(self, job):
        with self.database() as db:
            db.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(job.result), job.id))

    def recover(self):
        with self.lock, self.database() as db:
            self.closing = False
            for run_id, raw in db.execute("SELECT id,result FROM runs"):
                result = json.loads(raw)
                if result.get("job_state") in ACTIVE:
                    result.update(job_state="interrupted", state="interrupted", connection_cleanup_confirmed=False,
                                  errors=["제어 API가 재시작되어 이전 실행의 최종 상태를 확인할 수 없습니다. 자동 재실행하지 않습니다."])
                    db.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result), run_id))

    def start(self, run_id, request, data, worker, timeout):
        mqtt = data["operation"] == "mqtt"
        c = data["config"]
        planned = c["vehicle_count"] * c["messages_per_vehicle"]
        result = dict(run_id=run_id, transport=data["operation"], state="starting", job_state="starting",
                      allocated_clients=c["client_count"], assignment_count=c["vehicle_count"], assignments=[],
                      planned_messages=planned, processed_messages=0, remaining_messages=planned,
                      payload_bytes=0, connected_clients=0, active_clients=0, mqtt_published=0, puback_count=0,
                      publish_attempts=0, unconfirmed_messages=0, reconnect_count=0, elapsed_ms=0,
                      unsent_messages=planned if mqtt else 0, samples=[], errors=[], connection_cleanup_confirmed=False)
        created = datetime.now(timezone.utc).isoformat()
        with self.lock:
            if self.closing or self.active:
                raise HTTPException(409, detail="실행이 진행 중이거나 종료 중입니다. 완료 후 새 실행을 시작하세요.")
            if mqtt and not self.mqtt_lock.acquire(blocking=False):
                raise HTTPException(409, detail="다른 MQTT 실행이 진행 중입니다.")
            job = Job(run_id, result, mqtt, wall_limit_seconds=data.get("mqtt", {}).get("max_duration_seconds", timeout))
            result["wall_limit_seconds"] = job.wall_limit_seconds
            try:
                with self.database() as db:
                    db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run_id, created, json.dumps(request), json.dumps(result)))
                self.active = job
                job.thread = threading.Thread(target=self.run, args=(job, data, worker, timeout), daemon=True)
                job.thread.start()
            except BaseException:
                self.active = None
                if mqtt:
                    self.mqtt_lock.release()
                raise
        return {"id": run_id, "created_at": created, "request": request, "result": result.copy()}

    def force(self, job):
        with self.lock:
            if not job.finished.is_set() and job.process and job.process.poll() is None:
                job.forced = True
                job.process.kill()

    def send_command(self, job, command):
        if job.process and job.process.poll() is None:
            try:
                job.process.stdin.write(json.dumps({"command": command}) + "\n")
                job.process.stdin.flush()
            except (OSError, ValueError):
                pass

    def signal_stop(self, job):
        self.send_command(job, "stop")

    def pause(self, run_id, pause):
        with self.lock:
            job = self.active
            if not job or job.id != run_id or job.stop_requested or job.result.get("job_state") not in ACTIVE:
                raise HTTPException(409, detail="진행 중인 실행에서만 일시중지·재개할 수 있습니다.")
            if job.pause_requested == pause:
                return
            job.pause_requested = pause
            job.result["job_state"] = "pausing" if pause else "resuming"
            self.send_command(job, "pause" if pause else "resume")
            self.save(job)

    def request_stop(self, job, timed_out=False):
        with self.lock:
            if job.finished.is_set() or self.active is not job:
                return
            if not job.stop_requested:
                job.stop_requested = True
                job.timed_out = timed_out
                job.result.update(job_state="stopping")
                self.save(job)
                self.signal_stop(job)
                job.force_timer = threading.Timer(2, self.force, args=(job,))
                job.force_timer.daemon = True
                job.force_timer.start()

    def stop(self, run_id):
        with self.lock:
            job = self.active
            if job and job.id == run_id:
                self.request_stop(job)

    def run(self, job, data, worker, timeout):
        deadline = threading.Timer(timeout, self.request_stop, args=(job, True))
        deadline.daemon = True
        final = None
        process = None
        try:
            env = os.environ.copy()
            env["DANGUBI_MANAGED"] = "1"
            process = subprocess.Popen([str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, env=env)
            process.stdin.write(json.dumps(data) + "\n")
            process.stdin.flush()
            with self.lock:
                job.process = process
                if job.stop_requested:
                    self.signal_stop(job)
                elif job.pause_requested:
                    self.send_command(job, "pause")
            deadline.start()
            while line := process.stdout.readline(1048577):
                if len(line) > 1048576:
                    raise ValueError("Worker 응답 크기 초과")
                event = json.loads(line)
                if "error" in event:
                    raise ValueError(event["error"])
                result = event["result"]
                if event["kind"] == "result":
                    final = result
                    break
                if event["kind"] != "progress":
                    raise ValueError("Worker 응답 형식 오류")
                with self.lock:
                    paused = result.get("execution_state") == "paused"
                    state = "paused" if paused and job.pause_requested else "pausing" if job.pause_requested else "resuming" if paused else "running"
                    job.result = {**result, "job_state": "stopping" if job.stop_requested else state, "connection_cleanup_confirmed": False, "wall_limit_seconds": job.wall_limit_seconds}
                    self.save(job)
            process.wait(timeout=3)
            if final is None or process.returncode:
                raise ValueError("Worker 최종 응답을 확인하지 못했습니다.")
            with self.lock:
                state = "stopped" if final["state"] == "stopped" else "failed" if final["state"] == "mqtt_failed" else "completed"
                job.result = {**final, "job_state": state, "connection_cleanup_confirmed": final.get("active_clients") == 0, "wall_limit_seconds": job.wall_limit_seconds}
                if job.timed_out:
                    job.result.update(job_state="failed", errors=["실행 제한시간을 초과해 중단했습니다."])
        except Exception:
            with self.lock:
                job.result.update(job_state="failed", state="failed", connection_cleanup_confirmed=False,
                                  errors=["Worker 실행 또는 최종 응답을 확인하지 못했습니다. 중간 결과만 보관합니다."])
        finally:
            deadline.cancel()
            if process:
                if process.poll() is None:
                    job.forced = True
                    process.kill()
                process.wait()
                process.stdin.close()
                process.stdout.close()
            with self.lock:
                if job.force_timer:
                    job.force_timer.cancel()
                job.result["forced_termination"] = job.forced
                if job.forced:
                    job.result.update(job_state="failed", connection_cleanup_confirmed=False,
                                      errors=["Worker를 강제 종료했습니다. 전송 및 연결 정리의 최종 응답은 미확인입니다."])
                if not job.result.get("connection_cleanup_confirmed"):
                    job.result["unconfirmed_messages"] = max(job.result.get("unconfirmed_messages", 0), job.result.get("publish_attempts", 0) - job.result.get("puback_count", 0))
                try:
                    self.save(job)
                finally:
                    if self.active is job:
                        self.active = None
                    if job.mqtt:
                        self.mqtt_lock.release()
                    job.finished.set()

    def shutdown(self):
        with self.lock:
            self.closing = True
            job = self.active
            if job:
                self.request_stop(job)
        if job:
            job.thread.join(timeout=5)
