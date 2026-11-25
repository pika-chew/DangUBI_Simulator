"""상시 운영 차량 명부(SQLite).

- 차량 특성은 등록할 때 한 번 정하고 저장한다. 전체 대수가 바뀌어도 기존 차량은 변하지 않는다.
- 추가분은 현재 등록 차량 대비 목표 비율에 모자란 패턴부터 배정한다(부족분 우선, 결정적).
- 원본 VIN은 명부에만 둔다. 외부 식별자는 가명 발급 연결부가 준 가명 VIN이며,
  가명이 없는 차량은 '가명 대기'로 운행하지 않는다.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import threading
from typing import Protocol

from services.control import vin as vin_codec

SCHEMA = 1
EXTERNAL_ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
PATTERNS = ("commute", "daytime", "flexible")
ACTIVE = ("pending_pseudonym", "ready", "driving", "removing", "cleanup_hold")
STATUS_LABELS = {"pending_pseudonym": "가명 대기", "ready": "대기", "driving": "운행",
                 "removing": "정리 중", "cleanup_hold": "정리 보류", "removed": "제외"}
MODEL_YEAR_RANGE = (2016, 2026)


class RegistryError(ValueError):
    pass


class PseudonymProvider(Protocol):
    """가명 발급 연결부. 실제 vault는 후속 구현에서 이 형태로 교체한다."""
    name: str
    allows_real_transport: bool

    def issue(self, vins: list[str]) -> dict[str, str]: ...


class DevelopmentPseudonyms:
    """개발·로컬 검증 전용. 원본 VIN을 HMAC으로 변환하며 키는 프로세스 메모리에만 둔다.
    실제 AWS 전송 경로는 allows_real_transport=False인 발급기를 거부해야 한다."""
    name = "development"
    allows_real_transport = False

    def __init__(self, key: bytes | None = None):
        self._key = key or os.urandom(32)

    def issue(self, vins):
        result = {}
        for raw in vins:
            digest = hmac.new(self._key, raw.encode(), hashlib.sha256).digest()
            number = int.from_bytes(digest, "big")
            chars = []
            for _ in range(15):
                number, index = divmod(number, len(vin_codec.ALPHABET))
                chars.append(vin_codec.ALPHABET[index])
            result[raw] = "PS" + "".join(chars)
        return result


def now():
    return datetime.now(timezone.utc).isoformat()


def deficit_order(current: dict[str, int], weights: dict[str, float], count: int) -> list[str]:
    """새 차량 count대에 줄 범주 순서. 매번 목표 대비 가장 모자란 범주를 고른다.
    빈 명부에서 시작하면 최대 나머지 배분과 같은 결과가 된다."""
    counts = dict(current)
    total = sum(counts.values())
    keys = list(weights)
    order = []
    for _ in range(count):
        total += 1
        choice = max(keys, key=lambda k: (weights[k] * total / 100 - counts.get(k, 0), -keys.index(k)))
        counts[choice] = counts.get(choice, 0) + 1
        order.append(choice)
    return order


class Registry:
    def __init__(self, path: Path, provider: PseudonymProvider, max_active: int = 25000, seed: int | None = None):
        self.path = Path(path)
        self.provider = provider
        self.max_active = max_active
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, requested INTEGER NOT NULL,
                    settings TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS vehicles (
                    id INTEGER PRIMARY KEY, vin TEXT NOT NULL UNIQUE, pseudonym TEXT UNIQUE,
                    status TEXT NOT NULL, pattern TEXT NOT NULL, anomaly_eligible INTEGER NOT NULL,
                    traits TEXT NOT NULL, batch_id INTEGER NOT NULL REFERENCES batches(id),
                    registered_at TEXT NOT NULL, status_at TEXT NOT NULL, note TEXT);
                CREATE INDEX IF NOT EXISTS vehicles_status ON vehicles(status);
                CREATE TABLE IF NOT EXISTS operation (
                    id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL, settings TEXT,
                    virtual_checkpoint_ms INTEGER, last_stop TEXT, stop_reason TEXT, error TEXT,
                    session_started_at TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS vehicle_state (
                    id INTEGER PRIMARY KEY REFERENCES vehicles(id), start_day TEXT NOT NULL,
                    last_end_ms INTEGER, final TEXT, trip TEXT, confirmed_ts INTEGER, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operation_events (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS trip_settings (
                    id INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE, settings TEXT NOT NULL,
                    worker_hash TEXT, destination TEXT);
                CREATE TABLE IF NOT EXISTS trip_history (
                    id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id), trip_key TEXT NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, profile TEXT NOT NULL, initial TEXT NOT NULL,
                    settings_id INTEGER NOT NULL REFERENCES trip_settings(id), transport TEXT NOT NULL, session TEXT,
                    status TEXT NOT NULL, confirmed_ts INTEGER, resume_after_ms INTEGER, recoveries INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL, finished_at TEXT, UNIQUE(vehicle_id, trip_key));
                CREATE INDEX IF NOT EXISTS trip_history_vehicle ON trip_history(vehicle_id, start_ms);
                CREATE TABLE IF NOT EXISTS focus_days (
                    day TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (day, digest));
            """)
            con.execute("INSERT OR IGNORE INTO operation(id, state, updated_at) VALUES (1, 'stopped', ?)", (now(),))
            columns = {row["name"] for row in con.execute("PRAGMA table_info(operation)")}
            if "window_end_ms" not in columns:
                con.execute("ALTER TABLE operation ADD COLUMN window_end_ms INTEGER")
            con.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
            con.execute("INSERT OR IGNORE INTO meta VALUES ('next_serial', '1')")
            # 명부 고유 seed: 같은 명부에서 특성 난수를 재현하기 위한 값이며 비밀이 아니다.
            con.execute("INSERT OR IGNORE INTO meta VALUES ('seed', ?)", (str(seed if seed is not None else random.SystemRandom().randrange(2**31)),))
            stored = int(con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0])
            if stored != SCHEMA:
                raise RegistryError(f"지원하지 않는 명부 스키마 {stored}입니다.")
            # 운행 이력은 이 시각 이후 시작한 운행부터 남는다. 그 전 운행은 조회 근거가 없다.
            con.execute("INSERT OR IGNORE INTO meta VALUES ('history_since', ?)", (now(),))
            self._backfill_history(con)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def meta(self, con, key):
        return con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    def rng(self, con, *parts):
        value = "|".join(map(str, ("registry", self.meta(con, "seed"), *parts)))
        return random.Random(int.from_bytes(hashlib.sha256(value.encode()).digest(), "big"))

    def register(self, count: int, settings: dict) -> dict:
        """settings: pattern_mix, road_mix, anomaly_vehicle_percent, trip_min_minutes, trip_max_minutes."""
        if count < 1:
            raise RegistryError("등록 대수는 1대 이상이어야 합니다.")
        with self.lock, self.connect() as con:
            active = self.active_count(con)
            if active + count > self.max_active:
                raise RegistryError(f"등록 차량은 최대 {self.max_active:,}대입니다. 현재 {active:,}대이므로 {self.max_active-active:,}대까지 추가할 수 있습니다.")
            first = int(self.meta(con, "next_serial"))
            if first + count - 1 > vin_codec.SERIAL_MAX:
                raise RegistryError("VIN 일련번호를 모두 사용했습니다. 일련번호는 재사용하지 않습니다.")
            batch = con.execute("INSERT INTO batches(created_at, requested, settings, result) VALUES (?,?,?,?)",
                                (now(), count, json.dumps(settings, ensure_ascii=False), "{}")).lastrowid
            shuffle = self.rng(con, "batch", batch)
            patterns = deficit_order(self.counts(con, "pattern"), settings["pattern_mix"], count)
            percent = settings["anomaly_vehicle_percent"]
            anomaly = deficit_order({"1": self.counts(con, "anomaly_eligible").get("1", 0),
                                     "0": self.counts(con, "anomaly_eligible").get("0", 0)},
                                    {"1": percent, "0": 100 - percent}, count)
            shuffle.shuffle(patterns)
            shuffle.shuffle(anomaly)
            roads, road_weights = list(settings["road_mix"]), list(settings["road_mix"].values())
            low, high = settings["trip_min_minutes"], settings["trip_max_minutes"]
            stamp = now()
            for offset in range(count):
                serial = first + offset
                fixed = self.rng(con, "vehicle", serial)
                kind = patterns[offset]
                raw = vin_codec.build(serial, fixed.randint(*MODEL_YEAR_RANGE), fixed)
                traits = {
                    "first_departure_minute": fixed.randint(420, 540) if kind == "commute" else fixed.randint(480, 600) if kind == "daytime" else fixed.randint(360, 600),
                    "evening_departure_minute": fixed.randint(1020, 1140),
                    "road_profile": fixed.choices(roads, weights=road_weights)[0],
                    "commute_minutes": fixed.triangular(low, high, low + (high - low) * .3),
                    "trip_min_minutes": low, "trip_max_minutes": high,
                }
                con.execute("INSERT INTO vehicles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (serial, raw, None, "pending_pseudonym", kind, int(anomaly[offset] == "1"),
                             json.dumps(traits), batch, stamp, stamp, None))
            con.execute("UPDATE meta SET value=? WHERE key='next_serial'", (str(first + count),))
            summary = {"patterns": {k: patterns.count(k) for k in PATTERNS}, "anomaly_eligible": anomaly.count("1")}
            con.execute("UPDATE batches SET result=? WHERE id=?", (json.dumps(summary), batch))
        issued = self.issue_pseudonyms()
        return {"batch_id": batch, "registered": count, "first_id": first, "last_id": first + count - 1, **summary, "pseudonyms": issued}

    def issue_pseudonyms(self) -> dict:
        """가명 대기 차량을 묶음으로 발급 요청한다. 실패하면 대기 상태를 유지한다."""
        with self.lock:
            with self.connect() as con:
                rows = con.execute("SELECT id, vin FROM vehicles WHERE status='pending_pseudonym' ORDER BY id").fetchall()
                taken = {r[0] for r in con.execute("SELECT pseudonym FROM vehicles WHERE pseudonym IS NOT NULL")}
                raws = {r["vin"] for r in con.execute("SELECT vin FROM vehicles")}
            if not rows:
                return {"requested": 0, "issued": 0, "provider": self.provider.name}
            try:
                result = self.provider.issue([r["vin"] for r in rows])
            except Exception as error:  # 발급기 오류 문구에 VIN이 섞일 수 있어 종류만 남긴다.
                return {"requested": len(rows), "issued": 0, "provider": self.provider.name, "error": f"가명 발급 실패({type(error).__name__}). 차량은 가명 대기로 유지됩니다."}
            invalid = 0
            issued = 0
            stamp = now()
            with self.connect() as con:
                for row in rows:
                    alias = result.get(row["vin"]) if isinstance(result, dict) else None
                    if not isinstance(alias, str) or not EXTERNAL_ID.fullmatch(alias) or alias in taken or alias in raws:
                        invalid += 1
                        continue
                    taken.add(alias)
                    con.execute("UPDATE vehicles SET pseudonym=?, status='ready', status_at=? WHERE id=? AND status='pending_pseudonym'",
                                (alias, stamp, row["id"]))
                    issued += 1
            report = {"requested": len(rows), "issued": issued, "provider": self.provider.name}
            if invalid:
                report["error"] = f"사용할 수 없는 가명 {invalid:,}건(누락·형식 오류·중복·원본 VIN과 동일). 해당 차량은 가명 대기로 유지됩니다."
            return report

    def resolve_hold(self, ids: list[int], action: str) -> dict:
        """정리 보류 차량: retry=다음 운영 시작 때 남은 전송을 마무리, drop=손실을 기록하고 제외."""
        if action not in ("retry", "drop"):
            raise RegistryError("보류 처리는 retry 또는 drop입니다.")
        changed = 0
        stamp = now()
        with self.lock, self.connect() as con:
            for vehicle in dict.fromkeys(ids):
                row = con.execute("SELECT v.status, s.trip, s.confirmed_ts FROM vehicles v LEFT JOIN vehicle_state s ON s.id=v.id WHERE v.id=?", (vehicle,)).fetchone()
                if row is None or row["status"] != "cleanup_hold":
                    continue
                if action == "drop":
                    trip = json.loads(row["trip"]) if row["trip"] else None
                    self.event(con, "cleanup_loss", {"vehicle_id": vehicle, "trip_key": trip and trip["key"], "confirmed_ts": row["confirmed_ts"]})
                    if trip:
                        con.execute("UPDATE trip_history SET status='lost', confirmed_ts=?, finished_at=? WHERE vehicle_id=? AND trip_key=?",
                                    (row["confirmed_ts"], stamp, vehicle, trip["key"]))
                    con.execute("UPDATE vehicle_state SET trip=NULL, confirmed_ts=NULL, updated_at=? WHERE id=?", (stamp, vehicle))
                con.execute("UPDATE vehicles SET status=?, status_at=? WHERE id=?", ("removed" if action == "drop" else "removing", stamp, vehicle))
                changed += 1
        return {"changed": changed, "action": action}

    def event(self, con, kind, detail):
        con.execute("INSERT INTO operation_events(at, kind, detail) VALUES (?,?,?)", (now(), kind, json.dumps(detail, ensure_ascii=False)))

    def add_event(self, kind, detail):
        with self.connect() as con:
            self.event(con, kind, detail)

    def events(self, limit=50):
        with self.connect() as con:
            rows = con.execute("SELECT * FROM operation_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["id"], "at": r["at"], "kind": r["kind"], "detail": json.loads(r["detail"])} for r in rows]

    def operation(self) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT * FROM operation WHERE id=1").fetchone()
        data = dict(row)
        data["settings"] = json.loads(data["settings"]) if data["settings"] else None
        return data

    def update_operation(self, **fields):
        allowed = {"state", "settings", "virtual_checkpoint_ms", "window_end_ms", "last_stop", "stop_reason", "error", "session_started_at"}
        if set(fields) - allowed:
            raise RegistryError("알 수 없는 운영 상태 항목입니다.")
        if "settings" in fields and fields["settings"] is not None:
            fields["settings"] = json.dumps(fields["settings"], ensure_ascii=False)
        with self.connect() as con:
            con.execute(f"UPDATE operation SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? WHERE id=1", (*fields.values(), now()))

    def session_vehicles(self) -> list[dict]:
        """운영에 넣을 차량: 가명이 있고 제외·보류가 아닌 차량. 원본 VIN은 넣지 않는다."""
        with self.connect() as con:
            rows = con.execute("""SELECT v.id, v.pseudonym, v.status, v.pattern, v.anomaly_eligible, v.traits,
                s.start_day, s.last_end_ms, s.final, s.trip, s.confirmed_ts
                FROM vehicles v LEFT JOIN vehicle_state s ON s.id=v.id
                WHERE v.pseudonym IS NOT NULL AND v.status IN ('ready','driving','removing') ORDER BY v.id""").fetchall()
        return [self._session_row(r) for r in rows]

    def recovery_trips(self):
        with self.connect() as con:
            rows = con.execute("""SELECT v.pseudonym, s.trip FROM vehicle_state s
                JOIN vehicles v ON v.id=s.id WHERE s.trip IS NOT NULL AND v.status <> 'removed'""").fetchall()
        return [{"pseudonym": row["pseudonym"], "trip": json.loads(row["trip"])} for row in rows]

    @staticmethod
    def _session_row(r):
        fixed = {**json.loads(r["traits"]), "pattern": r["pattern"], "anomaly_eligible": bool(r["anomaly_eligible"])}
        return {"id": r["id"], "pseudonym": r["pseudonym"], "status": r["status"], "fixed": fixed,
                "start_day": r["start_day"], "last_end_ms": r["last_end_ms"],
                "final": json.loads(r["final"]) if r["final"] else None,
                "trip": json.loads(r["trip"]) if r["trip"] else None, "confirmed_ts": r["confirmed_ts"]}

    def set_status(self, ids, status, only_from=None):
        if status not in STATUS_LABELS:
            raise RegistryError("알 수 없는 상태입니다.")
        stamp = now()
        with self.lock, self.connect() as con:
            for vehicle in ids:
                if only_from:
                    con.execute(f"UPDATE vehicles SET status=?, status_at=? WHERE id=? AND status IN ({','.join('?'*len(only_from))})", (status, stamp, vehicle, *only_from))
                else:
                    con.execute("UPDATE vehicles SET status=?, status_at=? WHERE id=?", (status, stamp, vehicle))

    def ensure_start_day(self, vehicle, day: str):
        """운영에 처음 들어온 차량의 첫 운행 날짜를 정한다(이미 있으면 유지)."""
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO vehicle_state(id, start_day, updated_at) VALUES (?,?,?)", (vehicle, day, now()))
            return con.execute("SELECT start_day FROM vehicle_state WHERE id=?", (vehicle,)).fetchone()[0]

    def save_trip_start(self, vehicle, trip: dict, session: str | None = None):
        """첫 메시지를 보내기 전에 기록한다. 비정상 종료 후 같은 운행을 다시 만들 근거이며,
        같은 트랜잭션에 운행 이력(조회 재생성 근거) 한 행을 남긴다. 메시지 원문은 저장하지 않는다."""
        stamp = now()
        with self.connect() as con:
            con.execute("UPDATE vehicle_state SET trip=?, confirmed_ts=NULL, updated_at=? WHERE id=?", (json.dumps(trip), stamp, vehicle))
            self._insert_history(con, vehicle, trip, "sending", session, stamp)

    @staticmethod
    def _settings_id(con, settings: dict, worker_hash, destination) -> int:
        text = json.dumps(settings, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(json.dumps([text, worker_hash, destination]).encode()).hexdigest()
        con.execute("INSERT OR IGNORE INTO trip_settings(digest, settings, worker_hash, destination) VALUES (?,?,?,?)",
                    (digest, text, worker_hash, destination))
        return con.execute("SELECT id FROM trip_settings WHERE digest=?", (digest,)).fetchone()[0]

    def _insert_history(self, con, vehicle, trip, status, session, stamp, confirmed_ts=None):
        settings = trip.get("settings")
        if settings is None:
            row = con.execute("SELECT settings FROM operation WHERE id=1").fetchone()
            settings = json.loads(row[0]) if row and row[0] else None
        if settings is None:
            return  # 당시 설정을 확인할 수 없으면 재생성 근거가 없으므로 이력을 만들지 않는다.
        settings_id = self._settings_id(con, settings, trip.get("worker_hash"), trip.get("destination"))
        con.execute("""INSERT OR IGNORE INTO trip_history(vehicle_id, trip_key, start_ms, end_ms, profile, initial, settings_id,
                       transport, session, status, confirmed_ts, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (vehicle, trip["key"], trip["a"], trip["b"], trip["profile"], json.dumps(trip["initial"]), settings_id,
                     settings.get("transport", "dry_run"), session, status, confirmed_ts, stamp))

    def _backfill_history(self, con):
        """이력 기능 이전에 시작돼 아직 끝나지 않은 운행은 저장된 시작 기록으로 이력을 만든다(복구 대기)."""
        rows = con.execute("""SELECT s.id, s.trip, s.confirmed_ts FROM vehicle_state s
            WHERE s.trip IS NOT NULL AND NOT EXISTS (SELECT 1 FROM trip_history h WHERE h.vehicle_id=s.id
            AND h.trip_key=json_extract(s.trip, '$.key'))""").fetchall()
        for row in rows:
            self._insert_history(con, row["id"], json.loads(row["trip"]), "interrupted", None, now(), row["confirmed_ts"])

    def mark_history_interrupted(self):
        """비정상 종료: 전송 중이던 운행을 '중단·복구 대기'로 바꾸고 마지막 확인 시각을 옮겨 둔다."""
        with self.connect() as con:
            con.execute("""UPDATE trip_history SET status='interrupted', confirmed_ts=(SELECT s.confirmed_ts FROM vehicle_state s
                WHERE s.id=trip_history.vehicle_id AND json_extract(s.trip, '$.key')=trip_history.trip_key)
                WHERE status='sending'""")

    def recover_history(self, vehicle, trip_key, confirmed_ts):
        """복구 운행을 다시 보내기 시작한다. resume_after_ms 이후는 중복 가능 구간이다."""
        with self.connect() as con:
            con.execute("""UPDATE trip_history SET status='sending', recoveries=recoveries+1, resume_after_ms=?
                WHERE vehicle_id=? AND trip_key=?""", (confirmed_ts, vehicle, trip_key))

    def seed(self):
        with self.connect() as con:
            return int(self.meta(con, "seed"))

    def clear_trip(self, vehicle):
        """메시지를 보내기 전에 버린 운행의 시작 기록을 지운다(복구 대상 아님)."""
        with self.connect() as con:
            con.execute("UPDATE vehicle_state SET trip=NULL, confirmed_ts=NULL, updated_at=? WHERE id=?", (now(), vehicle))

    def save_confirmed(self, progress: dict):
        """{차량: 마지막 PUBACK 확인 메시지 ts} 체크포인트."""
        if not progress:
            return
        stamp = now()
        with self.connect() as con:
            con.executemany("UPDATE vehicle_state SET confirmed_ts=?, updated_at=? WHERE id=? AND trip IS NOT NULL",
                            [(ts, stamp, vehicle) for vehicle, ts in progress.items()])

    def save_checkpoint(self, virtual_ms: int, progress: dict, state: str | None = None):
        """가상시각과 차량별 확인 지점을 한 트랜잭션에 보관한다."""
        stamp = now()
        with self.connect() as con:
            if progress:
                con.executemany("UPDATE vehicle_state SET confirmed_ts=?, updated_at=? WHERE id=? AND trip IS NOT NULL",
                                [(ts, stamp, vehicle) for vehicle, ts in progress.items()])
            con.execute("UPDATE operation SET virtual_checkpoint_ms=?, state=COALESCE(?,state), updated_at=? WHERE id=1",
                        (virtual_ms, state, stamp))

    def save_trip_end(self, vehicle, last_end_ms, final: dict):
        """종료 Trip 확인(MQTT는 PUBACK, 송신 없는 생성은 생성 완료) 때 호출한다."""
        stamp = now()
        with self.connect() as con:
            row = con.execute("SELECT trip FROM vehicle_state WHERE id=?", (vehicle,)).fetchone()
            key = json.loads(row["trip"])["key"] if row and row["trip"] else None
            con.execute("UPDATE vehicle_state SET trip=NULL, confirmed_ts=NULL, last_end_ms=?, final=?, updated_at=? WHERE id=?",
                        (last_end_ms, json.dumps(final), stamp, vehicle))
            if key:
                con.execute("UPDATE trip_history SET status='completed', confirmed_ts=?, finished_at=? WHERE vehicle_id=? AND trip_key=?",
                            (last_end_ms, stamp, vehicle, key))

    def request_removal(self, ids: list[int]) -> dict:
        """운행 중인 차량은 운행을 마친 뒤 정리하도록 '정리 중'으로, 그 외는 즉시 '제외'로 바꾼다."""
        changed = {"removed": 0, "removing": 0, "unchanged": 0}
        stamp = now()
        with self.lock, self.connect() as con:
            for vehicle in dict.fromkeys(ids):
                row = con.execute("SELECT status FROM vehicles WHERE id=?", (vehicle,)).fetchone()
                if row is None or row["status"] in ("removed", "removing", "cleanup_hold"):
                    changed["unchanged"] += 1
                    continue
                target = "removing" if row["status"] == "driving" else "removed"
                con.execute("UPDATE vehicles SET status=?, status_at=? WHERE id=?", (target, stamp, vehicle))
                changed[target] += 1
        return changed

    def active_count(self, con):
        return con.execute(f"SELECT COUNT(*) FROM vehicles WHERE status IN ({','.join('?'*len(ACTIVE))})", ACTIVE).fetchone()[0]

    def counts(self, con, column):
        if column not in ("pattern", "anomaly_eligible", "status"):
            raise RegistryError("지원하지 않는 집계입니다.")
        where = "" if column == "status" else f" WHERE status IN ({','.join('?'*len(ACTIVE))})"
        rows = con.execute(f"SELECT {column}, COUNT(*) FROM vehicles{where} GROUP BY {column}", () if column == "status" else ACTIVE)
        return {str(key): n for key, n in rows}

    def summary(self) -> dict:
        with self.connect() as con:
            active = self.active_count(con)
            patterns = self.counts(con, "pattern")
            latest = con.execute("SELECT settings FROM batches ORDER BY id DESC LIMIT 1").fetchone()
            return {
                "active": active, "max_active": self.max_active,
                "status": {k: {"label": STATUS_LABELS[k], "count": v} for k, v in self.counts(con, "status").items()},
                "patterns": {k: {"count": patterns.get(k, 0), "percent": round(patterns.get(k, 0) * 100 / active, 1) if active else 0} for k in PATTERNS},
                "anomaly_eligible": self.counts(con, "anomaly_eligible").get("1", 0),
                "target": json.loads(latest[0]) if latest else None,
                "serial_remaining": vin_codec.SERIAL_MAX - int(self.meta(con, "next_serial")) + 1,
                "pseudonym_provider": self.provider.name,
                "provider_allows_real_transport": self.provider.allows_real_transport,
            }

    def vehicles(self, status: str | None = None, offset: int = 0, limit: int = 100) -> dict:
        """응답 기본값에는 원본 VIN을 넣지 않는다. 가린 값과 가명 VIN만 준다."""
        if status is not None and status not in STATUS_LABELS:
            raise RegistryError("알 수 없는 상태입니다.")
        where, args = ("WHERE status=?", (status,)) if status else ("", ())
        with self.connect() as con:
            total = con.execute(f"SELECT COUNT(*) FROM vehicles {where}", args).fetchone()[0]
            rows = con.execute(f"SELECT * FROM vehicles {where} ORDER BY id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"total": total, "offset": offset, "limit": limit, "vehicles": [self._public_vehicle(r) for r in rows]}

    @staticmethod
    def _public_vehicle(r):
        return {"id": r["id"], "vin_masked": vin_codec.mask(r["vin"]), "pseudonym": r["pseudonym"],
                "status": r["status"], "status_label": STATUS_LABELS[r["status"]], "pattern": r["pattern"],
                "anomaly_eligible": bool(r["anomaly_eligible"]), "traits": json.loads(r["traits"]),
                "batch_id": r["batch_id"], "registered_at": r["registered_at"], "status_at": r["status_at"]}

    # ---- 운행 집중 시간대 ----
    def focus_day(self, day: str, digest: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT data FROM focus_days WHERE day=? AND digest=?", (day, digest)).fetchone()
        return json.loads(row[0]) if row else None

    def save_focus_day(self, day: str, digest: str, data: dict) -> dict:
        """처음 저장한 값이 우선한다(같은 날짜를 두 번 정하지 않음)."""
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO focus_days VALUES (?,?,?,?)", (day, digest, json.dumps(data), now()))
            return json.loads(con.execute("SELECT data FROM focus_days WHERE day=? AND digest=?", (day, digest)).fetchone()[0])

    # ---- 운행 이력 조회 ----
    def find_vehicle(self, query: str) -> dict | None:
        """명부 번호 또는 가명 VIN으로 찾는다. 원본 VIN 검색·표시는 하지 않는다."""
        text = query.strip()
        with self.connect() as con:
            if text.isdigit():
                row = con.execute("SELECT * FROM vehicles WHERE id=?", (int(text),)).fetchone()
            else:
                row = con.execute("SELECT * FROM vehicles WHERE pseudonym IN (?,?)", (text, text.upper())).fetchone()
        return self._public_vehicle(row) if row else None

    def history(self, vehicle: int, from_ms: int | None = None, to_ms: int | None = None, limit: int = 2000) -> dict:
        """선택 차량의 기간 내 운행 이력. 진행 중 운행의 확인 시각은 최근 체크포인트 값이다.
        기간을 주지 않으면 마지막 운행이 있는 KST 날짜까지의 7일을 쓴다(운행이 없으면 오늘까지)."""
        with self.connect() as con:
            row = con.execute("SELECT * FROM vehicles WHERE id=?", (vehicle,)).fetchone()
            if row is None:
                raise RegistryError("명부에 없는 차량입니다.")
            span = con.execute("SELECT COUNT(*), MIN(start_ms), MAX(end_ms) FROM trip_history WHERE vehicle_id=?", (vehicle,)).fetchone()
            if from_ms is None or to_ms is None:
                anchor = span[2] if span[2] is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
                kst = timezone(timedelta(hours=9))
                midnight = datetime.combine(datetime.fromtimestamp(anchor / 1000, kst).date() + timedelta(days=1), datetime.min.time(), kst)
                to_ms = int(midnight.timestamp() * 1000)
                from_ms = to_ms - 7 * 86400000
            rows = con.execute("""SELECT h.*, s.trip AS live_trip, s.confirmed_ts AS live_confirmed FROM trip_history h
                LEFT JOIN vehicle_state s ON s.id=h.vehicle_id WHERE h.vehicle_id=? AND h.end_ms>? AND h.start_ms<?
                ORDER BY h.start_ms LIMIT ?""", (vehicle, from_ms, to_ms, limit + 1)).fetchall()
            state = con.execute("SELECT last_end_ms FROM vehicle_state WHERE id=?", (vehicle,)).fetchone()
            since = self.meta(con, "history_since")
        trips = [self._history_row(r) for r in rows[:limit]]
        return {"vehicle": self._public_vehicle(row), "trips": trips, "has_more": len(rows) > limit, "from_ms": from_ms, "to_ms": to_ms,
                "total_trips": span[0], "first_start_ms": span[1], "last_end_ms": span[2], "history_since": since,
                "last_completed_end_ms": state["last_end_ms"] if state else None}

    @staticmethod
    def _history_row(r):
        confirmed = r["confirmed_ts"]
        live = json.loads(r["live_trip"]) if r["live_trip"] else None
        if r["status"] == "sending" and live and live.get("key") == r["trip_key"]:
            confirmed = r["live_confirmed"]
        return {"vehicle_id": r["vehicle_id"], "trip_key": r["trip_key"], "start_ms": r["start_ms"], "end_ms": r["end_ms"],
                "observed_start_ms": r["start_ms"], "observed_end_ms": r["end_ms"], "road_profile": r["profile"],
                "status": r["status"], "transport": r["transport"], "session": r["session"], "confirmed_ts": confirmed,
                "resume_after_ms": r["resume_after_ms"], "recoveries": r["recoveries"],
                "started_at": r["started_at"], "finished_at": r["finished_at"]}

    def history_trip(self, vehicle: int, trip_key: str) -> dict:
        """재생성 입력: 운행 이력 + 당시 설정·Worker 지문 + 차량 고정 특성."""
        with self.connect() as con:
            row = con.execute("""SELECT h.*, t.settings, t.worker_hash, v.anomaly_eligible, v.pseudonym,
                s.trip AS live_trip, s.confirmed_ts AS live_confirmed FROM trip_history h
                JOIN trip_settings t ON t.id=h.settings_id JOIN vehicles v ON v.id=h.vehicle_id
                LEFT JOIN vehicle_state s ON s.id=h.vehicle_id WHERE h.vehicle_id=? AND h.trip_key=?""", (vehicle, trip_key)).fetchone()
            seed = int(self.meta(con, "seed"))
        if row is None:
            raise RegistryError("운행 이력에 없는 운행입니다.")
        return {**self._history_row(row), "initial": json.loads(row["initial"]), "settings": json.loads(row["settings"]),
                "worker_hash": row["worker_hash"], "anomaly_eligible": bool(row["anomaly_eligible"]),
                "pseudonym": row["pseudonym"], "seed": seed}
