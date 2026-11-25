import random
import sqlite3

import pytest
from fastapi.testclient import TestClient

from services.control import vin
from services.control.registry import Registry, RegistryError, DevelopmentPseudonyms, deficit_order

SETTINGS = {"pattern_mix": {"commute": 60, "daytime": 20, "flexible": 20},
            "road_mix": {"urban": 50, "highway": 20, "congested": 10, "mixed": 20},
            "anomaly_vehicle_percent": 5, "trip_min_minutes": 3, "trip_max_minutes": 120}


def test_check_digit_matches_public_example():
    # NHTSA vPIC가 "Check Digit (9th position) is correct"로 판정한 공개 예시.
    assert vin.check_digit("1M8GDM9AXKP042788") == "X"
    assert vin.is_valid("1M8GDM9AXKP042788")
    assert not vin.is_valid("1M8GDM9A1KP042788")
    assert not vin.is_valid("1M8GDM9AXKP04278O")


def test_built_vin_follows_format_and_serial_uniqueness():
    seen = set()
    for serial in (1, 2, 999_999):
        value = vin.build(serial, 2026, random.Random(serial))
        assert len(value) == 17 and vin.is_valid(value)
        assert value.startswith(vin.VIRTUAL_WMI) and value[6] in vin.LETTERS
        assert value[9] == "T" and value[11:] == f"{serial:06d}"
        assert not set("IOQ") & set(value)
        seen.add(value)
    assert len(seen) == 3
    with pytest.raises(ValueError):
        vin.build(1_000_000, 2026, random.Random(1))
    assert vin.mask("0DSABCDEXTS000123") == "*************0123"


def test_deficit_order_restores_target_without_touching_existing():
    weights = {"commute": 60, "daytime": 20, "flexible": 20}
    fresh = deficit_order({}, weights, 1000)
    assert {k: fresh.count(k) for k in weights} == {"commute": 600, "daytime": 200, "flexible": 200}
    assert set(deficit_order({"commute": 300, "daytime": 200, "flexible": 200}, weights, 300)) == {"commute"}
    assert set(deficit_order({"commute": 300, "daytime": 200, "flexible": 200}, weights, 100)) == {"commute"}
    odd = deficit_order({}, weights, 7)
    assert {k: odd.count(k) for k in weights} == {"commute": 4, "daytime": 2, "flexible": 1}


def make(tmp_path, provider=None, **kw):
    return Registry(tmp_path / "registry.sqlite3", provider or DevelopmentPseudonyms(b"k" * 32), seed=7, **kw)


def test_register_fixes_traits_and_rebalances_only_new_vehicles(tmp_path):
    reg = make(tmp_path)
    first = reg.register(1000, SETTINGS)
    assert first["patterns"] == {"commute": 600, "daytime": 200, "flexible": 200}
    assert first["anomaly_eligible"] == 50 and first["pseudonyms"]["issued"] == 1000
    before = {v["id"]: v for v in reg.vehicles(limit=100)["vehicles"]}
    commute = [v["id"] for v in all_vehicles(reg) if v["pattern"] == "commute"][:300]
    assert reg.request_removal(commute) == {"removed": 300, "removing": 0, "unchanged": 0}
    assert reg.summary()["patterns"]["commute"]["count"] == 300
    added = reg.register(100, SETTINGS)
    assert added["patterns"] == {"commute": 100, "daytime": 0, "flexible": 0}
    assert added["first_id"] == 1001  # 제외된 일련번호는 재사용하지 않는다.
    after = {v["id"]: v for v in reg.vehicles(limit=100)["vehicles"]}
    for key in before:
        assert after[key]["traits"] == before[key]["traits"] and after[key]["pattern"] == before[key]["pattern"]


def all_vehicles(reg):
    out, offset = [], 0
    while True:
        page = reg.vehicles(offset=offset, limit=100)
        out += page["vehicles"]
        offset += 100
        if offset >= page["total"]:
            return out


def test_responses_never_contain_raw_vin(tmp_path):
    reg = make(tmp_path)
    reg.register(5, SETTINGS)
    with sqlite3.connect(tmp_path / "registry.sqlite3") as con:
        raws = [r[0] for r in con.execute("SELECT vin FROM vehicles")]
    text = repr(reg.vehicles()) + repr(reg.summary())
    assert all(vin.is_valid(r) for r in raws)
    assert not any(r in text for r in raws)
    pseudonyms = [v["pseudonym"] for v in reg.vehicles()["vehicles"]]
    assert all(p.startswith("PS") and len(p) == 17 for p in pseudonyms) and len(set(pseudonyms)) == 5


class Failing:
    name = "failing"
    allows_real_transport = True

    def issue(self, vins):
        raise RuntimeError("vault down for " + vins[0])


class Echo:
    name = "echo"
    allows_real_transport = True

    def issue(self, vins):
        return {v: v for v in vins}


@pytest.mark.parametrize("provider", [Failing(), Echo()])
def test_failed_or_unsafe_pseudonyms_keep_vehicles_waiting(tmp_path, provider):
    reg = make(tmp_path, provider)
    result = reg.register(3, SETTINGS)["pseudonyms"]
    assert result["issued"] == 0 and "error" in result
    assert "0DS" not in result["error"]  # 발급기 오류 문구의 VIN을 전달하지 않는다.
    assert reg.summary()["status"] == {"pending_pseudonym": {"label": "가명 대기", "count": 3}}
    reg.provider = DevelopmentPseudonyms(b"k" * 32)
    assert reg.issue_pseudonyms()["issued"] == 3
    assert reg.summary()["status"]["ready"]["count"] == 3


def test_capacity_and_reopen_keep_counter(tmp_path):
    reg = make(tmp_path, max_active=10)
    reg.register(10, SETTINGS)
    with pytest.raises(RegistryError, match="최대 10대"):
        reg.register(1, SETTINGS)
    reg.request_removal([1, 2])
    reopened = make(tmp_path, max_active=10)
    assert reopened.register(2, SETTINGS)["first_id"] == 11


def test_general_removal_cannot_bypass_cleanup_hold(tmp_path):
    reg = make(tmp_path)
    reg.register(1, SETTINGS)
    reg.ensure_start_day(1, "2026-11-25")
    reg.save_trip_start(1, {"key": "held-trip"})
    reg.set_status([1], "cleanup_hold")
    assert reg.request_removal([1]) == {"removed": 0, "removing": 0, "unchanged": 1}
    assert reg.vehicles()["vehicles"][0]["status"] == "cleanup_hold"
    assert reg.recovery_trips()[0]["trip"]["key"] == "held-trip"
    assert reg.resolve_hold([1], "drop") == {"changed": 1, "action": "drop"}
    assert reg.events()[0]["kind"] == "cleanup_loss"


def test_operations_registry_api(tmp_path, monkeypatch):
    from services.control.test_app import control
    monkeypatch.setattr(control, "DB", tmp_path / "runs.sqlite3")
    monkeypatch.setattr(control, "CHECKPOINTS", control.CheckpointStore(tmp_path / "states.sqlite3"))
    monkeypatch.setattr(control, "_REGISTRY", make(tmp_path))
    with TestClient(control.app) as c:
        created = c.post("/api/operations/vehicles", json={"count": 10}).json()
        assert created["registered"] == 10 and created["pseudonyms"]["issued"] == 10
        listed = c.get("/api/operations/vehicles", params={"status": "ready"}).json()
        assert listed["total"] == 10 and listed["vehicles"][0]["vin_masked"].startswith("*")
        assert c.post("/api/operations/vehicles/remove", json={"ids": [1]}).status_code == 409
        assert c.post("/api/operations/vehicles/remove", json={"ids": [1], "confirm": True}).json()["removed"] == 1
        summary = c.get("/api/operations/registry").json()
        assert summary["active"] == 9 and summary["provider_allows_real_transport"] is False
        assert c.post("/api/operations/vehicles", json={"count": 1, "trip_min_minutes": 50, "trip_max_minutes": 10}).status_code == 422
        assert c.get("/api/operations/vehicles", params={"status": "bogus"}).status_code == 422
