"""시험 실행 자동 이어가기.

오류로 멈추거나(failed) 제어 API 재시작으로 끊긴(interrupted) 가장 최근 실행이 자동 이어가기를 켰다면,
대기 뒤 확인 지점부터 같은 실행 ID로 이어간다(`/api/jobs/{id}/recover`와 같은 경로).
- 사용자가 직접 중단한 실행, 실행 시간 한도를 넘긴 실행, 더 새로운 실행이 있으면 이어가지 않는다.
- 대기는 30초→1분→2분→5분→10분(이후 10분). 확인 지점이 나아가지 않은 채 연속 20번 실패하면 멈추고 기록한다.
- 시도 횟수·다음 시각·마지막 오류는 실행 결과의 `auto_recovery`에 남겨 서버가 다시 시작돼도 이어서 센다.
- 마지막 확인 지점 이후 메시지는 다시 보내므로 수신 측 중복이 생길 수 있다(QoS1과 같은 성질).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import time

DELAYS = (30, 60, 120, 300, 600)
MAX_WITHOUT_PROGRESS = 20
ELIGIBLE = ("failed", "interrupted")
HISTORY = 20


class AutoResumer:
    def __init__(self, jobs, recover, clock=time.time, interval=5.0, delays=DELAYS, max_without_progress=MAX_WITHOUT_PROGRESS):
        self.jobs, self.recover, self.clock = jobs, recover, clock
        self.interval, self.delays, self.max_without_progress = interval, delays, max_without_progress
        self.stopped = threading.Event()
        self.thread = None

    def start(self):
        self.stopped.clear()
        self.thread = threading.Thread(target=self._loop, name="auto-resume", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(5)

    def _loop(self):
        while not self.stopped.wait(self.interval):
            try:
                self.tick()
            except Exception:  # 감시 스레드는 멈추지 않는다. 다음 주기에 다시 본다.
                pass

    def _latest(self):
        with self.jobs.database() as db:
            row = db.execute("SELECT id, request, result FROM runs ORDER BY created DESC LIMIT 1").fetchone()
        return (row[0], json.loads(row[1]), row[2]) if row else None

    def _save(self, run_id, raw, result):
        """실행이 진행 중이 아닐 때만, 읽은 뒤 바뀌지 않은 기록에 덮어쓴다."""
        with self.jobs.lock, self.jobs.database() as db:
            if self.jobs.active:
                return False
            return db.execute("UPDATE runs SET result=? WHERE id=? AND result=?", (json.dumps(result), run_id, raw)).rowcount == 1

    def tick(self):
        if self.jobs.active_id() or getattr(self.jobs, "closing", False):
            return
        latest = self._latest()
        if not latest:
            return
        run_id, request, raw = latest
        result = json.loads(raw)
        if (not request.get("auto_resume") or result.get("job_state") not in ELIGIBLE
                or not result.get("schedule_checkpoint") or result.get("failure_kind") in ("user", "timeout")):
            return
        record = dict(result.get("auto_recovery") or {})
        if record.get("gave_up"):
            return
        progress = int(result["schedule_checkpoint"].get("count", 0))
        if progress > record.get("progress", -1):
            record["without_progress"] = 0
        record["progress"] = progress
        now = self.clock()
        if record.get("without_progress", 0) >= self.max_without_progress:
            record.update(gave_up=True, next_at=None)
            result["auto_recovery"] = record
            self._save(run_id, raw, result)
            return
        if record.get("next_at") is None:
            delay = self.delays[min(record.get("without_progress", 0), len(self.delays) - 1)]
            record["next_at"] = now + delay
            result["auto_recovery"] = record
            self._save(run_id, raw, result)
            return
        if now < record["next_at"]:
            return
        stamp = datetime.now(timezone.utc).isoformat()
        cause = (result.get("errors") or [result.get("job_state")])[0]
        record.update(attempts=record.get("attempts", 0) + 1, without_progress=record.get("without_progress", 0) + 1,
                      last_at=stamp, next_at=None, last_error=None)
        record["history"] = (record.get("history") or [])[-(HISTORY - 1):] + [{"at": stamp, "state": result.get("job_state"), "cause": str(cause)[:300], "confirmed": progress}]
        result["auto_recovery"] = record
        if not self._save(run_id, raw, result):
            return
        try:
            self.recover(run_id)
        except Exception as error:  # 이어가기 자체가 거절되면 기록하고 다음 대기 뒤 다시 본다.
            latest = self._latest()
            if latest and latest[0] == run_id:
                current = json.loads(latest[2])
                failed = dict(current.get("auto_recovery") or record)
                failed["last_error"] = str(getattr(error, "detail", error))[:300]
                current["auto_recovery"] = failed
                self._save(run_id, latest[2], current)
