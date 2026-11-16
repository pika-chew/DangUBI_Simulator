import copy
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

spec = importlib.util.spec_from_file_location("control_app", Path(__file__).with_name("app.py"))
control = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = control
spec.loader.exec_module(control)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "DB", tmp_path / "runs.sqlite3")
    with TestClient(control.app) as c:
        yield c


def request(mode="shared", clients=2, ty="4"):
    return {"config": {"vehicle_count": 3, "mapping_mode": mode, "client_count": clients, "messages_per_vehicle": 2, "interval_ms": 2000, "connection_lifetime": "always"}, "message": copy.deepcopy(control.CATALOG["profiles"][ty]["example"]), "transport": "dry_run", "confirm_mqtt": False, "realtime": False}


def test_schemas_and_examples():
    for profile in control.CATALOG["profiles"].values():
        Draft202012Validator.check_schema(profile["schema"])
        control.validate_message(profile["example"])


def test_api_worker_and_fixed_record(client):
    body = request()
    preview = client.post("/api/preview", json=body)
    assert preview.status_code == 200
    assert preview.json()["plan"]["processed_messages"] == 0
    assert client.get("/api/runs").json() == []
    response = client.post("/api/runs", json=body)
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["result"]["processed_messages"] == 6
    assert run["result"]["mqtt_published"] == run["result"]["puback_count"] == run["result"]["connected_clients"] == 0
    assert len({a["client_id"] for a in run["result"]["assignments"]}) == 2
    assert client.patch("/api/runs/" + run["id"], json={"client_count": 3}).status_code == 409
    assert client.get("/api/runs/" + run["id"]).json()["request"] == body
    assert len(client.get("/api/runs").json()) == 1


def test_two_modes_same_message_count_and_trip(client):
    pool = client.post("/api/runs", json=request()).json()["result"]
    full = client.post("/api/runs", json=request("dedicated", 3)).json()["result"]
    assert pool["processed_messages"] == full["processed_messages"]
    assert len({a["client_id"] for a in full["assignments"]}) == 3
    assert client.post("/api/runs", json=request(ty="3")).status_code == 201


@pytest.mark.parametrize("mutate", [
    lambda x: x["message"]["pld"].pop("clt"),
    lambda x: x["message"]["pld"].update(lat=100),
    lambda x: x["message"]["pld"].update(sp="30"),
    lambda x: x.update(transport="unknown"),
    lambda x: x["config"].update(mapping_mode="dedicated"),
    lambda x: x["config"].update(messages_per_vehicle=10000),
    lambda x: x["config"].update(vehicle_count=True),
])
def test_invalid_never_stored(client, mutate):
    body = request()
    mutate(body)
    assert client.post("/api/runs", json=body).status_code == 422
    assert client.get("/api/runs").json() == []


def test_mqtt_requires_server_and_user_confirmation(client, monkeypatch, tmp_path):
    monkeypatch.setattr(control, "MQTT_PROFILE", tmp_path / "missing.json")
    body = request()
    body["transport"] = "mqtt"
    assert client.post("/api/runs", json=body).status_code == 409
    body["confirm_mqtt"] = True
    assert client.post("/api/runs", json=body).status_code == 409
    assert client.get("/api/health").json()["mqtt_ready"] is False
    assert client.get("/api/runs").json() == []


def test_profile_never_saved_and_cap(client, monkeypatch, tmp_path):
    import json
    profile = json.loads((control.ROOT / "configs/mqtt.example.json").read_text())
    profile.update(enabled=True, contract_reviewed=True, max_messages=1)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(control, "MQTT_PROFILE", path)
    body = request()
    body.update(transport="mqtt", confirm_mqtt=True)
    assert client.post("/api/runs", json=body).status_code == 422
    health = client.get("/api/health").json()
    assert health["mqtt_ready"] is True
    assert "ca_file" not in json.dumps(health)


def automatic_request():
    body = request()
    body.pop("message")
    body["generation"] = {"profile": "urban", "seed": 42, "start_time_ms": 1794783600000, "duration_seconds": 60, "start_lat": 37.5, "start_lon": 127.0, "initial_odometer_m": 500000.0}
    return body


def test_automatic_generation_schema_replay_and_storage(client):
    body = automatic_request()
    preview = client.post("/api/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["plan"]["planned_messages"] == 96
    assert client.get("/api/runs").json() == []
    response = client.post("/api/runs", json=body)
    assert response.status_code == 201, response.text
    run = response.json()
    result = run["result"]
    assert result["microtrip_count"] == 93 and result["trip_count"] == 3
    assert run["request"]["config"]["messages_per_vehicle"] == 32
    for sample in result["samples"] + result["trip_summaries"]:
        control.validate_message(sample["message"])
    assert client.get("/api/runs/" + run["id"]).json() == run
    replay = client.post("/api/runs", json=body).json()["result"]
    assert replay["trace"] == result["trace"]
    assert [s["message"] for s in replay["trip_summaries"]] == [s["message"] for s in result["trip_summaries"]]
    body["config"].update(mapping_mode="dedicated", client_count=3)
    dedicated = client.post("/api/runs", json=body).json()["result"]
    assert dedicated["trace"] == result["trace"]
    assert dedicated["planned_messages"] == result["planned_messages"]


@pytest.mark.parametrize("mutate", [
    lambda b: b["config"].update(interval_ms=700),
    lambda b: b["config"].update(vehicle_count=10000),
    lambda b: b["generation"].update(start_lat=90),
    lambda b: b["generation"].update(duration_seconds=5),
    lambda b: b["generation"].update(profile="unknown"),
    lambda b: b.update(message=request()["message"]),
])
def test_invalid_automatic_not_stored(client, mutate):
    body = automatic_request()
    mutate(body)
    assert client.post("/api/runs", json=body).status_code == 422
    assert client.get("/api/runs").json() == []


def wait_job(client, run_id, predicate, timeout=4):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get("/api/jobs/" + run_id).json()
        if predicate(run["result"]):
            return run
        time.sleep(0.02)
    pytest.fail("실행 상태 대기시간 초과: " + str(run))


def test_background_start_poll_stop_and_fixed_record(client):
    import time
    body = automatic_request()
    body["realtime"] = True
    started = time.monotonic()
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 202, response.text
    assert time.monotonic() - started < 2
    run_id = response.json()["id"]
    run = wait_job(client, run_id, lambda r: r["processed_messages"] > 0)
    assert run["result"]["job_state"] == "running"
    assert run["result"]["processed_messages"] < 96
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").json()["active_job_id"] == run_id
    assert client.post("/api/jobs", json=body).status_code == 409
    assert client.patch("/api/runs/" + run_id, json={}).status_code == 409
    assert client.post("/api/jobs/" + run_id + "/stop").status_code == 202
    run = wait_job(client, run_id, lambda r: r["job_state"] == "stopped")
    assert run["result"]["processed_messages"] < 96
    assert run["result"]["trip_count"] == 0
    assert run["result"]["connection_cleanup_confirmed"] is True
    assert run["result"]["forced_termination"] is False
    assert run["request"]["generation"]["seed"] == 42
    assert client.post("/api/jobs/" + run_id + "/stop").status_code == 202
    assert client.get("/api/runs/" + run_id).json() == run
    assert client.get("/api/health").json()["active_job_id"] is None


def test_background_completion_and_no_network(client):
    body = request()
    body["realtime"] = True
    body["config"]["interval_ms"] = 200
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 202
    run = wait_job(client, response.json()["id"], lambda r: r["job_state"] == "completed")
    assert run["result"]["processed_messages"] == 6
    assert run["result"]["puback_count"] == 0
    assert run["result"]["remaining_messages"] == 0
    assert run["result"]["elapsed_ms"] >= 190
    assert client.post("/api/jobs/" + run["id"] + "/stop").json() == run


def test_worker_failure_saved_and_recovery(client, monkeypatch, tmp_path):
    import json
    profile = json.loads((control.ROOT / "configs/mqtt.example.json").read_text())
    profile.update(enabled=True, contract_reviewed=True)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(control, "MQTT_PROFILE", path)
    body = request()
    body.update(transport="mqtt", confirm_mqtt=True)
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 202
    failed = wait_job(client, response.json()["id"], lambda r: r["job_state"] == "failed")
    assert failed["result"]["errors"]
    assert "ca_file" not in json.dumps(failed)
    assert not control.MQTT_LOCK.locked()
    with control.database() as db:
        result = failed["result"]
        result["job_state"] = "running"
        db.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result), failed["id"]))
    control.JOBS.recover()
    recovered = client.get("/api/jobs/" + failed["id"]).json()
    assert recovered["result"]["job_state"] == "interrupted"
    assert recovered["result"]["connection_cleanup_confirmed"] is False
    assert control.JOBS.active_id() is None


def test_lifespan_stops_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "DB", tmp_path / "shutdown.sqlite3")
    with TestClient(control.app) as c:
        body = automatic_request()
        body["realtime"] = True
        response = c.post("/api/jobs", json=body)
        run_id = response.json()["id"]
        wait_job(c, run_id, lambda r: r["processed_messages"] > 0)
    assert control.JOBS.active_id() is None
    with control.database() as db:
        import json
        result = json.loads(db.execute("SELECT result FROM runs WHERE id=?", (run_id,)).fetchone()[0])
    assert result["job_state"] == "stopped"
    assert result["forced_termination"] is False


def test_async_guards_never_stored(client):
    body = request()
    body.update(transport="mqtt")
    assert client.post("/api/jobs", json=body).status_code == 409
    body = request()
    body["realtime"] = True
    body["config"].update(interval_ms=86400000)
    assert client.post("/api/jobs", json=body).status_code == 422
    assert client.get("/api/runs").json() == []


def test_forced_stop_marks_uncertainty_and_releases_slot(client, monkeypatch):
    monkeypatch.setattr(control.JOBS, "signal_stop", lambda job: None)
    body = automatic_request()
    body["realtime"] = True
    response = client.post("/api/jobs", json=body)
    run_id = response.json()["id"]
    wait_job(client, run_id, lambda r: r["processed_messages"] > 0)
    client.post("/api/jobs/" + run_id + "/stop")
    run = wait_job(client, run_id, lambda r: r["job_state"] == "failed")
    assert run["result"]["forced_termination"] is True
    assert run["result"]["connection_cleanup_confirmed"] is False
    assert control.JOBS.active_id() is None


def test_run_deadline_stops_and_saves_failure(client):
    body = control.RunRequest.model_validate(automatic_request())
    body.realtime = True
    data, _ = control.prepare_worker(body, "dry_run", "deadline-test")
    run = control.JOBS.start("deadline-test", body.model_dump(exclude_none=True), data, control.WORKER, 0.2)
    saved = wait_job(client, run["id"], lambda r: r["job_state"] == "failed")
    assert saved["result"]["forced_termination"] is False
    assert "제한시간" in saved["result"]["errors"][0]
    assert saved["result"]["connection_cleanup_confirmed"] is True


def test_async_real_tls_mqtt_stop(client, monkeypatch, tmp_path):
    import os
    import subprocess
    import time
    profile_path = tmp_path / "mqtt-profile.json"
    env = os.environ.copy()
    env.update(DANGUBI_BROWSER_TEST_PROFILE=str(profile_path), GOCACHE=str(control.ROOT / ".local/go-cache"), GOMODCACHE=str(control.ROOT / ".local/go-mod"))
    broker = subprocess.Popen(["go", "test", "-run", "TestMQTTBrowserFixture", "-count=1"], cwd=control.ROOT / "services/worker", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        while not profile_path.exists() and time.monotonic() < deadline and broker.poll() is None:
            time.sleep(0.02)
        assert profile_path.exists(), "로컬 TLS 시험 브로커 준비 실패"
        monkeypatch.setattr(control, "MQTT_PROFILE", profile_path)
        body = request()
        body.update(transport="mqtt", confirm_mqtt=True)
        response = client.post("/api/jobs", json=body)
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        running = wait_job(client, run_id, lambda r: r["puback_count"] > 0)
        assert running["result"]["active_clients"] == 2
        assert control.MQTT_LOCK.locked()
        assert client.post("/api/jobs/" + run_id + "/pause").status_code == 202
        paused = wait_job(client, run_id, lambda r: r["job_state"] == "paused")
        count = paused["result"]["puback_count"]
        time.sleep(0.4)
        frozen = client.get("/api/jobs/" + run_id).json()["result"]
        assert frozen["puback_count"] == count
        assert frozen["active_clients"] == 2
        assert control.MQTT_LOCK.locked()
        assert client.post("/api/jobs/" + run_id + "/resume").status_code == 202
        wait_job(client, run_id, lambda r: r["job_state"] == "running")
        assert client.post("/api/runs", json=body).status_code == 409
        client.post("/api/jobs/" + run_id + "/stop")
        stopped = wait_job(client, run_id, lambda r: r["job_state"] == "stopped")
        result = stopped["result"]
        assert 0 < result["puback_count"] < 6
        assert result["active_clients"] == 0
        assert result["connection_cleanup_confirmed"] is True
        assert result["forced_termination"] is False
        assert result["unsent_messages"] > 0
        assert not control.MQTT_LOCK.locked()
    finally:
        Path(str(profile_path) + ".done").write_text("done")
        try:
            output, _ = broker.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            broker.kill()
            output, _ = broker.communicate()
        assert broker.returncode == 0, output.decode(errors="replace")


def test_pause_resume_preserves_counters_and_timing(client):
    import time
    body = request()
    body["realtime"] = True
    body["config"].update(messages_per_vehicle=4, interval_ms=300)
    started = client.post("/api/jobs", json=body).json()
    run_id = started["id"]
    wait_job(client, run_id, lambda r: r["processed_messages"] > 0)
    assert client.post("/api/jobs/" + run_id + "/pause").status_code == 202
    paused = wait_job(client, run_id, lambda r: r["job_state"] == "paused")
    assert client.post("/api/jobs/" + run_id + "/pause").status_code == 202
    time.sleep(0.5)
    again = client.get("/api/jobs/" + run_id).json()
    assert again["result"]["processed_messages"] == paused["result"]["processed_messages"]
    assert again["result"]["simulation_time_ms"] == paused["result"]["simulation_time_ms"]
    assert again["result"]["paused_ms"] > paused["result"]["paused_ms"]
    assert client.post("/api/jobs", json=body).status_code == 409
    assert client.post("/api/jobs/" + run_id + "/resume").status_code == 202
    assert client.post("/api/jobs/" + run_id + "/resume").status_code == 202
    completed = wait_job(client, run_id, lambda r: r["job_state"] == "completed")
    assert completed["result"]["processed_messages"] == 12
    assert completed["result"]["paused_ms"] >= 450
    assert completed["result"]["simulation_time_ms"] == 900
    assert completed["result"]["assignments"] == paused["result"]["assignments"]
    assert client.post("/api/jobs/" + run_id + "/pause").status_code == 409


def test_stop_and_shutdown_while_paused(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "DB", tmp_path / "paused-shutdown.sqlite3")
    with TestClient(control.app) as c:
        body = automatic_request()
        body["realtime"] = True
        run_id = c.post("/api/jobs", json=body).json()["id"]
        c.post("/api/jobs/" + run_id + "/pause")
        wait_job(c, run_id, lambda r: r["job_state"] == "paused")
    with control.database() as db:
        import json
        result = json.loads(db.execute("SELECT result FROM runs WHERE id=?", (run_id,)).fetchone()[0])
    assert result["job_state"] == "stopped"
    assert not result["forced_termination"]
    assert result["connection_cleanup_confirmed"]
