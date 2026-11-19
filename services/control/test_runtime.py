from services.control.runtime import RuntimePaths


def test_runtime_default_and_external_data(tmp_path):
    local = RuntimePaths.from_env(tmp_path, {})
    external = RuntimePaths.from_env(tmp_path, {"DANGUBI_DATA_DIR": str(tmp_path / "persistent")})
    assert local.database == tmp_path / ".local/runs.sqlite3"
    assert local.checkpoints == tmp_path / ".local/obd-state.sqlite3"
    assert external.database == tmp_path / "persistent/runs.sqlite3"
    assert external.checkpoints == tmp_path / "persistent/obd-state.sqlite3"
    assert external.worker == local.worker
    assert external.mqtt_profile == local.mqtt_profile


def test_runtime_individual_overrides_survive_data_directory(tmp_path):
    paths = RuntimePaths.from_env(tmp_path, {
        "DANGUBI_DATA_DIR": "data", "DANGUBI_DB": "separate/runs.db",
        "DANGUBI_CHECKPOINT_DB": "cache/states.db", "DANGUBI_WORKER": "bin/worker",
        "DANGUBI_MQTT_PROFILE": "private/mqtt.json",
    })
    assert paths.database == tmp_path / "separate/runs.db"
    assert paths.checkpoints == tmp_path / "cache/states.db"
    assert paths.worker == tmp_path / "bin/worker"
    assert paths.mqtt_profile == tmp_path / "private/mqtt.json"


def test_new_fleet_defaults_and_explicit_saved_values():
    from services.control.fleet import FleetRequest
    base = dict(measurement=dict(start="2026-11-19T00:00:00+09:00", end="2026-11-20T00:00:00+09:00"))
    new = FleetRequest(fleet_version="fleet-plan-4", **base)
    assert (new.trip_min_minutes, new.trip_max_minutes, new.interval_ms, new.day_off_probability) == (3, 120, 2000, .2)
    assert FleetRequest(fleet_version="fleet-plan-3", **base).trip_max_minutes == 90
    assert FleetRequest(fleet_version="fleet-plan-4", trip_max_minutes=90, **base).trip_max_minutes == 90
