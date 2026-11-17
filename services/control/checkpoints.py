"""재생성 가능한 차량 종료 상태만 보관하는 크기 제한 로컬 캐시."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import threading
import time


class CheckpointError(RuntimeError):
    pass


class CheckpointReset(CheckpointError):
    pass


def valid_state(state):
    keys = {"lat", "lon", "odometer_m", "coolant_c", "heading_rad"}
    return (isinstance(state, dict) and set(state) == keys
            and all(type(v) in (int, float) and math.isfinite(v) for v in state.values())
            and abs(state["lat"]) <= 85 and abs(state["lon"]) <= 180
            and 0 <= state["odometer_m"] <= 1e12 and -40 <= state["coolant_c"] <= 130)


class CheckpointStore:
    def __init__(self, path: Path, max_entries=2048, ttl_seconds=86400, clock=time.time):
        if not 1 <= max_entries <= 2048 or ttl_seconds <= 0:
            raise ValueError("중간 상태의 보관 상한을 확인하세요.")
        self.path = path
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            con = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                con = sqlite3.connect(self.path, timeout=2)
                con.execute("PRAGMA page_size=4096")
                con.execute("PRAGMA auto_vacuum=FULL")
                con.execute("PRAGMA max_page_count=1024")
                con.execute("BEGIN IMMEDIATE")
                con.execute("CREATE TABLE IF NOT EXISTS checkpoint_epoch (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)")
                con.execute("INSERT OR IGNORE INTO checkpoint_epoch VALUES (1,0)")
                con.execute("CREATE TABLE IF NOT EXISTS checkpoints (scope TEXT NOT NULL, seq INTEGER NOT NULL, end_ms INTEGER NOT NULL, state TEXT NOT NULL, accessed REAL NOT NULL, PRIMARY KEY(scope,seq))")
                con.execute("CREATE INDEX IF NOT EXISTS checkpoint_access ON checkpoints(accessed)")
                con.execute("DELETE FROM checkpoints WHERE accessed < ?", (self.clock()-self.ttl_seconds,))
                yield con
                con.commit()
            except (sqlite3.Error, OSError) as error:
                if con:
                    con.rollback()
                raise CheckpointError("중간 상태 저장소를 사용할 수 없습니다. 경로·디스크 여유를 확인하고 다시 조회하세요.") from error
            finally:
                if con:
                    con.close()

    def resume(self, scope, before):
        with self.transaction() as con:
            epoch = con.execute("SELECT value FROM checkpoint_epoch WHERE id=1").fetchone()[0]
            while True:
                row = con.execute("SELECT seq,end_ms,state FROM checkpoints WHERE scope=? AND seq<? ORDER BY seq DESC LIMIT 1", (scope,before)).fetchone()
                if row is None:
                    return epoch, None
                try:
                    state = json.loads(row[2])
                except (ValueError, TypeError):
                    state = None
                if row[0] >= 0 and type(row[1]) is int and 0 <= row[1] <= 4102444800000 and valid_state(state):
                    con.execute("UPDATE checkpoints SET accessed=? WHERE scope=? AND seq=?", (self.clock(),scope,row[0]))
                    return epoch, (row[0],row[1],state)
                # 손상된 재생성 캐시는 실행 기록과 무관하게 폐기한다.
                con.execute("DELETE FROM checkpoints WHERE scope=? AND seq=?", (scope,row[0]))

    def put(self, scope, seq, end_ms, state, epoch):
        if not valid_state(state) or seq < 0:
            raise CheckpointError("저장할 차량 중간 상태의 범위를 확인하세요.")
        encoded = json.dumps(state, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 1024:
            raise CheckpointError("중간 상태 크기가 저장 상한을 초과했습니다.")
        with self.transaction() as con:
            if con.execute("SELECT value FROM checkpoint_epoch WHERE id=1").fetchone()[0] != epoch:
                raise CheckpointReset("중간 상태가 비워졌습니다. 새 조회를 시작하세요.")
            exists = con.execute("SELECT 1 FROM checkpoints WHERE scope=? AND seq=?", (scope,seq)).fetchone()
            if not exists:
                count = con.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
                if count >= self.max_entries:
                    con.execute("DELETE FROM checkpoints WHERE rowid IN (SELECT rowid FROM checkpoints ORDER BY accessed,scope,seq LIMIT ?)", (count-self.max_entries+1,))
            con.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?)", (scope,seq,end_ms,encoded,self.clock()))

    def stats(self):
        with self.transaction() as con:
            epoch = con.execute("SELECT value FROM checkpoint_epoch WHERE id=1").fetchone()[0]
            count = con.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
            size = con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
        return {"epoch":epoch, "entries":count, "max_entries":self.max_entries, "ttl_seconds":self.ttl_seconds,
                "database_bytes":size, "max_database_bytes":4194304}

    def clear(self):
        with self.transaction() as con:
            con.execute("UPDATE checkpoint_epoch SET value=value+1 WHERE id=1")
            con.execute("DELETE FROM checkpoints")
        return self.stats()
