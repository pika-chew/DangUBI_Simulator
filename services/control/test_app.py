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
    monkeypatch.setattr(control, "CHECKPOINTS", control.CheckpointStore(tmp_path / "states.sqlite3"))
    with TestClient(control.app) as c:
        yield c


def request(mode="shared", clients=2, ty="4"):
    return {"config": {"vehicle_count": 3, "mapping_mode": mode, "client_count": clients, "messages_per_vehicle": 2, "interval_ms": 2000, "connection_lifetime": "always", "publish_mode": "serial", "max_in_flight": 1, "queue_capacity": 100}, "message": copy.deepcopy(control.CATALOG["profiles"][ty]["example"]), "transport": "dry_run", "confirm_mqtt": False, "realtime": False}


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


@pytest.mark.parametrize("publish_mode", ["serial", "parallel"])
def test_async_real_tls_mqtt_stop(client, monkeypatch, tmp_path, publish_mode):
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
        body["config"].update(publish_mode=publish_mode, max_in_flight=2 if publish_mode=="parallel" else 1, queue_capacity=2)
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
        assert result["performance"]["ack"]["count"] == result["publish_attempts"]
        assert result["performance"]["connect"]["count"] >= result["connected_clients"]
        assert result["performance"]["generation"]["count"] == result["processed_messages"]
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


def test_delete_finished_records_confirmation_and_atomicity(client):
    first=client.post("/api/runs",json=request()).json()["id"]
    second=client.post("/api/runs",json=request()).json()["id"]
    assert client.post("/api/runs/delete",json={"ids":[first]}).status_code==409
    assert client.post("/api/runs/delete",json={"ids":[first,"missing"],"confirm":True}).status_code==404
    assert client.get("/api/runs/"+first).status_code==200
    deleted=client.post("/api/runs/delete",json={"ids":[first,second,first],"confirm":True})
    assert deleted.status_code==200 and deleted.json()["deleted_count"]==2
    assert client.get("/api/runs/"+first).status_code==404
    assert client.get("/api/runs").json()==[]


def test_delete_cannot_remove_active_or_paused_job(client):
    body=automatic_request();body["realtime"]=True
    run_id=client.post("/api/jobs",json=body).json()["id"]
    assert client.post("/api/runs/delete",json={"ids":[run_id],"confirm":True}).status_code==409
    client.post("/api/jobs/"+run_id+"/pause")
    wait_job(client,run_id,lambda r:r["job_state"]=="paused")
    assert client.post("/api/runs/delete",json={"ids":[run_id],"confirm":True}).status_code==409
    client.post("/api/jobs/"+run_id+"/stop")
    wait_job(client,run_id,lambda r:r["job_state"]=="stopped")
    assert client.post("/api/runs/delete",json={"ids":[run_id],"confirm":True}).status_code==200


def test_scenario_model_schema_and_vehicle_query(client):
    body=automatic_request()
    body["generation"].update(model_version="combustion-scenario-2",profile="mixed",duration_seconds=600,anomaly_vehicle_percent=50.0,anomaly_events_per_hour=10.0)
    response=client.post("/api/runs",json=body)
    assert response.status_code==201,response.text
    run=response.json()
    assert run["result"]["model_version"]=="combustion-scenario-2"
    for sample in run["result"]["samples"]+run["result"]["trip_summaries"]:
        control.validate_message(sample["message"])
    trace=client.get("/api/runs/"+run["id"]+"/trace/2?start_seconds=100&end_seconds=200")
    assert trace.status_code==200,trace.text
    points=trace.json()["trace"]
    assert points[0]["elapsed_seconds"]>=100 and points[-1]["elapsed_seconds"]<=200
    assert len(points)<=120 and points[0]["context"]["road"]
    assert client.get("/api/runs/"+run["id"]+"/trace/0").status_code==422
    assert client.get("/api/runs/"+run["id"]+"/trace/4").status_code==422
    assert client.get("/api/runs/"+run["id"]+"/trace/2?end_seconds=601").status_code==422
    assert client.get("/api/runs/"+run["id"]).json()==run


def test_trace_does_not_show_future_for_partial_job(client):
    body=automatic_request();body["realtime"]=True
    body["generation"].update(model_version="combustion-scenario-2")
    run_id=client.post("/api/jobs",json=body).json()["id"]
    wait_job(client,run_id,lambda r:r["processed_messages"]>0)
    assert client.get("/api/runs/"+run_id+"/trace/2?end_seconds=60").status_code==422
    client.post("/api/jobs/"+run_id+"/stop")
    wait_job(client,run_id,lambda r:r["job_state"]=="stopped")


def test_route_model_and_replay(client):
    body=automatic_request()
    body["generation"].update(model_version="combustion-route-3",profile="highway",duration_seconds=3600)
    body["config"]["interval_ms"]=4000
    response=client.post("/api/runs",json=body)
    assert response.status_code==201,response.text
    run=response.json()
    assert run["result"]["model_version"]=="combustion-route-3"
    trace=client.get("/api/runs/"+run["id"]+"/trace/2?start_seconds=900&end_seconds=1000")
    assert trace.status_code==200,trace.text
    assert all(p["context"]["road"]=="고속 도로" for p in trace.json()["trace"])
    for sample in run["result"]["samples"]+run["result"]["trip_summaries"]:
        control.validate_message(sample["message"])


@pytest.mark.parametrize("version", ["combustion-demo-1", "combustion-scenario-2", "combustion-route-3"])
def test_interval_microtrip_pagination_and_wire_separation(client,version):
    body=automatic_request()
    body["generation"].update(model_version=version,profile="urban",duration_seconds=600)
    run=client.post("/api/runs",json=body).json()
    path="/api/runs/"+run["id"]+"/trace/2"
    pages=[client.get(path+"?start_seconds=10&end_seconds=420&message_offset="+str(n)).json() for n in (0,100,200,300)]
    assert [len(p["samples"]) for p in pages]==[100,100,6,0]
    assert all(p["message_total"]==206 for p in pages)
    samples=[s for page in pages for s in page["samples"]]
    assert len(set(s["sequence"] for s in samples))==206
    assert samples[0]["message"]["ts"]==body["generation"]["start_time_ms"]+10000
    assert samples[-1]["message"]["ts"]==body["generation"]["start_time_ms"]+420000
    for sample in samples:
        assert sample["vehicle_id"]=="v000002"
        assert set(sample["message"])=={"ty","ts","pld"}
        assert sample["message"]["ty"]==4
        control.validate_message(sample["message"])
        assert not ({"context","road","reason","driver_style","weather"} & sample["message"]["pld"].keys())
    repeat=client.get(path+"?start_seconds=10&end_seconds=420").json()
    assert repeat["samples"]==pages[0]["samples"]
    assert len(repeat["trace"])<=120
    ending=client.get(path+"?start_seconds=598&end_seconds=600").json()["samples"]
    assert len(ending)==2 and all(s["message"]["ty"]==4 for s in ending)
    assert client.get(path+"?start_seconds=1&end_seconds=1.5").json()["samples"]==[]
    tiny=client.get(path+"?start_seconds=10.0001&end_seconds=10.0002")
    assert tiny.status_code==200 and tiny.json()["message_total"]==0
    assert client.get(path+"?message_offset=-1").status_code==422
    assert client.get(path+"?message_offset=10001").status_code==422
    assert client.get("/api/runs/"+run["id"]).json()==run


def test_user_analysis_endpoint_removed(client):
    run=client.post("/api/runs",json=automatic_request()).json()
    assert client.get("/api/runs/"+run["id"]+"/analysis").status_code==404


@pytest.mark.parametrize("weather", ["dry", "rain"])
def test_korean_defaults_and_weather_payload(client,weather):
    body=automatic_request()
    body["generation"].update(model_version="combustion-korea-4",weather=weather,profile="urban",duration_seconds=600)
    response=client.post("/api/runs",json=body)
    assert response.status_code==201,response.text
    run=response.json()
    trace=client.get("/api/runs/"+run["id"]+"/trace/2").json()
    for point in trace["trace"]:
        assert point["speed_kmh"]<=point["context"]["speed_limit_kmh"]+1e-5
        assert point["context"]["weather"]==("건조" if weather=="dry" else "강우·젖은 노면")
    for sample in run["result"]["samples"]+run["result"]["trip_summaries"]:
        assert set(sample["message"])=={"ty","ts","pld"}
        control.validate_message(sample["message"])
    body["generation"]["weather"]="snow"
    assert client.post("/api/runs",json=body).status_code==422


@pytest.mark.parametrize("engine_start", ["cold", "warm"])
def test_powertrain_obd_values_and_segment_replay(client,engine_start):
    body=automatic_request()
    body["generation"].update(model_version="combustion-powertrain-5",engine_start=engine_start,weather="dry",profile="urban",duration_seconds=600)
    response=client.post("/api/runs",json=body)
    assert response.status_code==201,response.text
    run=response.json()
    assert run["result"]["model_version"]=="combustion-powertrain-5"
    for sample in run["result"]["samples"]+run["result"]["trip_summaries"]:
        control.validate_message(sample["message"])
        assert set(sample["message"])=={"ty","ts","pld"}
        assert not ({"gear","engine_state","fuel_rate_lph","coolant_c","wheel_power_kw"}&sample["message"]["pld"].keys())
    path="/api/runs/"+run["id"]+"/trace/2?start_seconds=580&end_seconds=600"
    trace=client.get(path)
    assert trace.status_code==200,trace.text
    data=trace.json()
    assert data==client.get(path).json()
    last=data["samples"][-1]
    trip=run["result"]["trip_summaries"][1]
    assert last["message"]["pld"]["fc"]==trip["message"]["pld"]["fc"]
    assert last["message"]["pld"]["rpm"]==0
    assert last["context"]["engine_state"]=="시동 OFF"
    assert (trip["message"]["pld"]["wut"]==0) if engine_start=="warm" else (trip["message"]["pld"]["wut"]>0)
    assert client.get("/api/runs/"+run["id"]).json()==run
    body["generation"]["engine_start"]="other"
    assert client.post("/api/runs",json=body).status_code==422
    body["generation"].update(model_version="combustion-korea-4",engine_start="warm")
    assert client.post("/api/runs",json=body).status_code==422

@pytest.mark.parametrize("intensity", ["low", "medium", "high"])
def test_timed_events_configuration_and_replay(client,intensity):
    body=automatic_request()
    body["generation"].update(model_version="combustion-events-6",anomaly_intensity=intensity,engine_start="warm",weather="rain",profile="urban",duration_seconds=600,anomaly_vehicle_percent=100,anomaly_events_per_hour=60)
    response=client.post("/api/runs",json=body)
    assert response.status_code==201,response.text
    run=response.json()
    assert run["result"]["model_version"]=="combustion-events-6"
    trace_path="/api/runs/"+run["id"]+"/trace/2?start_seconds=100&end_seconds=200"
    trace=client.get(trace_path)
    assert trace.status_code==200
    assert trace.json()==client.get(trace_path).json()
    for sample in trace.json()["samples"]+run["result"]["trip_summaries"]:
        control.validate_message(sample["message"])
        assert "anomaly_intensity" not in sample["message"]["pld"]
    assert client.get("/api/runs/"+run["id"]).json()==run
    body["generation"]["anomaly_intensity"]="other"
    assert client.post("/api/runs",json=body).status_code==422
    body["generation"].update(model_version="combustion-powertrain-5",anomaly_intensity="high")
    assert client.post("/api/runs",json=body).status_code==422

def schedule_example(name="manual"):
    import json
    return json.loads((control.ROOT / f"configs/schedule-{name}.example.json").read_text(encoding="utf-8"))


def test_schedule_manual_counts_boundaries_and_no_storage(client):
    body=schedule_example()
    before=client.get("/api/runs").json()
    response=client.post("/api/schedules/preview",json=body)
    assert response.status_code==200,response.text
    data=response.json()
    assert data["planned_messages"]==2702
    assert data["microtrip_count"]==2700 and data["trip_count"]==2
    assert data["inactive_vehicle_ids"]==["v000003"]
    assert len({r["tid"] for r in data["trips"]})==2
    assert data==client.post("/api/schedules/preview",json=body).json()
    page=client.post("/api/schedules/preview?offset=1&limit=1",json=body).json()
    assert page["trips"]==data["trips"][1:2] and page["planned_messages"]==2702
    assert client.get("/api/runs").json()==before
    assert client.post("/api/schedules/preview?limit=101",json=body).status_code==422


def test_schedule_partial_trip_and_minimum_parking(client):
    body=schedule_example()
    body["vehicles"]=body["vehicles"][:1]
    trip=body["vehicles"][0]["trips"][0]
    trip.update(start="2026-11-17T12:30:00+09:00",end="2026-11-17T13:30:00+09:00")
    rows=client.post("/api/schedules/preview",json=body).json()["trips"]
    assert rows[0]["started_before_window"] and rows[0]["microtrip_count"]==900 and rows[0]["trip_count"]==1
    body["vehicles"][0]["trips"].append(dict(start="2026-11-17T14:00:00+09:00",end="2026-11-17T18:30:00+09:00"))
    data=client.post("/api/schedules/preview",json=body).json()
    assert data["trips"][1]["continues_after_window"] and data["trips"][1]["trip_count"]==0
    assert data["trips"][1]["microtrip_count"]==7201
    body["vehicles"][0]["trips"][1]["start"]="2026-11-17T13:59:59.999+09:00"
    assert client.post("/api/schedules/preview",json=body).status_code==422


def test_schedule_pattern_exceptions_midnight_and_replay(client):
    body=schedule_example("patterned")
    body["vehicles"][0]["pattern"]["day_off_probability"]=1.0
    body["vehicles"][0]["overrides"]["2026-11-17"]=[{"start":"2026-11-17T23:30:00+09:00","end":"2026-11-18T00:30:00+09:00"}]
    response=client.post("/api/schedules/preview",json=body)
    assert response.status_code==200,response.text
    data=response.json()
    assert data["observed_trip_count"]==1 and data["trip_count"]==1
    original=data["trips"][0]
    body["measurement"]["start"]="2026-11-18T00:00:00+09:00"
    clipped=client.post("/api/schedules/preview",json=body).json()["trips"][0]
    assert clipped["trip_key"]==original["trip_key"] and clipped["tid"]==original["tid"]
    assert clipped["started_before_window"] and clipped["microtrip_count"]==900
    normal=schedule_example("patterned")
    a=client.post("/api/schedules/preview",json=normal)
    assert a.status_code==200,a.text
    assert a.json()==client.post("/api/schedules/preview",json=normal).json()
    from services.control.schedule import instant
    off_start=instant("2026-11-18T00:00:00+09:00")
    off_end=instant("2026-11-19T00:00:00+09:00")
    assert all(not (off_start <= r["start_ms"] < off_end) for r in a.json()["trips"])


@pytest.mark.parametrize("change", ["no_offset","duplicate","mode","overlap","too_long","wrong_exception"])
def test_schedule_invalid_settings(client,change):
    body=schedule_example()
    if change=="no_offset":body["measurement"]["start"]="2026-11-17T13:00:00"
    elif change=="duplicate":body["vehicles"].append(copy.deepcopy(body["vehicles"][0]))
    elif change=="mode":body["mode"]="patterned"
    elif change=="overlap":body["vehicles"][0]["trips"]*=2
    elif change=="too_long":body["measurement"]["end"]="2027-01-17T18:00:00+09:00"
    else:
        body=schedule_example("patterned")
        body["vehicles"][0]["overrides"]["2026-11-18"]=[{"start":"2026-11-17T13:00:00+09:00","end":"2026-11-17T14:00:00+09:00"}]
    assert client.post("/api/schedules/preview",json=body).status_code==422

def test_schedule_30_days_bounded_page_and_counts(client):
    body=schedule_example("patterned")
    body["measurement"].update(start="2026-11-01T00:00:00+09:00",end="2026-12-01T00:00:00+09:00")
    prototype=body["vehicles"][0]
    prototype["overrides"]={}
    prototype["pattern"].update(weekdays=[1,2,3,4,5,6,7],day_off_probability=0.0,baseline_jitter_minutes=0)
    for slot in prototype["pattern"]["slots"]:
        slot.update(duration_min_minutes=30,duration_max_minutes=30,departure_jitter_minutes=0)
    body["vehicles"]=[dict(copy.deepcopy(prototype),vehicle_id=f"v{i:06d}") for i in range(1,101)]
    response=client.post("/api/schedules/preview?limit=3",json=body)
    assert response.status_code==200,response.text
    data=response.json()
    assert data["observed_trip_count"]==6000 and len(data["trips"])==3 and data["has_more"]
    assert data["microtrip_count"]==5400000 and data["trip_count"]==6000
    assert len(data["vehicles"])==100 and data["vehicles"][0]["parking_ms"]==2484000000

def scheduled_obd_body():
    body=schedule_example()
    body["vehicles"]=body["vehicles"][:1]
    body["vehicles"][0]["trips"]=[{"start":"2026-11-17T13:00:00+09:00","end":"2026-11-17T13:01:00+09:00","road_profile":"urban"},{"start":"2026-11-17T13:31:00+09:00","end":"2026-11-17T13:32:00+09:00","road_profile":"urban"}]
    return {"schedule":body}


def test_schedule_obd_continuity_and_pages(client):
    body=scheduled_obd_body()
    plan=client.post("/api/schedules/preview",json=body["schedule"]).json()
    urls=["/api/schedules/obd/v000001/"+r["trip_key"] for r in plan["trips"]]
    before=client.get("/api/runs").json()
    first=client.post(urls[0],json=body)
    assert first.status_code==200,first.text
    a=first.json()
    assert len(a["messages"])==30 and a["trip"]["ty"]==3
    assert a["messages"][-1]["ts"]<a["trip"]["ts"]
    second=client.post(urls[1],json=body)
    assert second.status_code==200,second.text
    b=second.json()
    assert b["initial_state"]["lat"]==a["trip"]["pld"]["edlat"]
    assert b["initial_state"]["lon"]==a["trip"]["pld"]["edlon"]
    assert b["initial_state"]["odometer_m"]==a["trip"]["pld"]["tdis"]
    assert 20<b["initial_state"]["coolant_c"]<90
    assert b["messages"][0]["pld"]["fc"]==0 and b["messages"][0]["pld"]["cdit"]==0
    assert a["trip"]["pld"]["tid"]!=b["trip"]["pld"]["tid"]
    for m in a["messages"]+[a["trip"]]+b["messages"]+[b["trip"]]: control.validate_message(m)
    page=client.post(urls[1]+"?offset=10&limit=5",json=body).json()
    assert page["messages"]==b["messages"][10:15] and page["initial_state"]==b["initial_state"]
    assert b==client.post(urls[1],json=body).json()
    assert client.get("/api/runs").json()==before


def test_schedule_obd_partial_and_synthetic_trip(client):
    body=scheduled_obd_body()
    key=client.post("/api/schedules/preview",json=body["schedule"]).json()["trips"][0]["trip_key"]
    url="/api/schedules/obd/v000001/"+key
    full=client.post(url,json=body).json()
    body["schedule"]["measurement"].update(start="2026-11-17T13:00:10+09:00",end="2026-11-17T13:00:30+09:00")
    partial=client.post(url,json=body).json()
    assert partial["trip"] is None and partial["microtrip_count"]==11
    assert partial["messages"]==full["messages"][5:16]
    body["force_end_trip"]=True
    response=client.post(url,json=body)
    assert response.status_code==200,response.text
    synthetic=response.json()
    assert synthetic["synthetic_trip"] and synthetic["microtrip_count"]==10
    assert synthetic["trip"]["ts"]==partial["messages"][-1]["ts"]
    assert synthetic["trip"]["pld"]["fc"]<full["trip"]["pld"]["fc"]
    assert synthetic["trip"]["pld"]["stt"]==full["trip"]["pld"]["stt"]
    assert synthetic["messages"][-1]["ts"]<synthetic["trip"]["ts"]
    control.validate_message(synthetic["trip"])
    body["schedule"]["measurement"]["end"]="2026-11-17T13:01:00+09:00"
    natural=client.post(url,json=body).json()
    assert not natural["synthetic_trip"] and natural["trip"]["ts"]==full["trip"]["ts"]


def test_schedule_obd_rejects_unknown_and_subsecond(client):
    body=scheduled_obd_body()
    assert client.post("/api/schedules/obd/v000001/missing",json=body).status_code==422
    body["schedule"]["vehicles"][0]["trips"][0]["end"]="2026-11-17T13:01:00.001+09:00"
    body["schedule"]["vehicles"][0]["trips"]=body["schedule"]["vehicles"][0]["trips"][:1]
    plan=client.post("/api/schedules/preview",json=body["schedule"]).json()
    url="/api/schedules/obd/v000001/"+plan["trips"][0]["trip_key"]
    assert client.post(url,json=body).status_code==422

def test_schedule_obd_non_grid_end_has_only_trip(client):
    body=scheduled_obd_body()
    body["schedule"]["vehicles"][0]["trips"]=body["schedule"]["vehicles"][0]["trips"][:1]
    body["schedule"]["vehicles"][0]["trips"][0]["end"]="2026-11-17T13:01:01+09:00"
    key=client.post("/api/schedules/preview",json=body["schedule"]).json()["trips"][0]["trip_key"]
    data=client.post("/api/schedules/obd/v000001/"+key,json=body).json()
    assert len(data["messages"])==31
    assert data["trip"]["ts"]-data["messages"][-1]["ts"]==1000
    assert data["messages"][-1]["pld"]["fc"]<=data["trip"]["pld"]["fc"]
    control.validate_message(data["trip"])

def test_schedule_obd_two_hours_is_bounded(client):
    body=scheduled_obd_body()
    body["schedule"]["vehicles"][0]["trips"]=body["schedule"]["vehicles"][0]["trips"][:1]
    body["schedule"]["vehicles"][0]["trips"][0]["end"]="2026-11-17T15:00:00+09:00"
    key=client.post("/api/schedules/preview",json=body["schedule"]).json()["trips"][0]["trip_key"]
    response=client.post("/api/schedules/obd/v000001/"+key+"?limit=3",json=body)
    assert response.status_code==200,response.text
    data=response.json()
    assert data["microtrip_count"]==3600 and len(data["messages"])==3 and data["has_more"]
    assert 0<len(data["trace"])<=120 and data["trace"][0]["state"]
    control.validate_message(data["trip"])


def test_schedule_vehicle_filter_keeps_global_counts_and_pages(client):
    body=scheduled_obd_body()["schedule"]
    body["vehicles"].append({"vehicle_id":"v000002","trips":[]})
    whole=client.post("/api/schedules/preview",json=body).json()
    page=client.post("/api/schedules/preview?vehicle_id=v000001&limit=1",json=body).json()
    assert page["matched_trip_count"]==2 and page["has_more"]
    assert page["observed_trip_count"]==whole["observed_trip_count"]==2
    assert page["microtrip_count"]==whole["microtrip_count"]
    second=client.post("/api/schedules/preview?vehicle_id=v000001&limit=1&offset=1",json=body).json()
    assert second["trips"]==whole["trips"][1:] and not second["has_more"]
    inactive=client.post("/api/schedules/preview?vehicle_id=v000002",json=body).json()
    assert inactive["trips"]==[] and inactive["matched_trip_count"]==0 and not inactive["has_more"]
    assert client.post("/api/schedules/preview?vehicle_id=v000003",json=body).status_code==422


def fleet_example():
    return {"vehicle_count":20,"measurement":{"start":"2026-11-17T00:00:00+09:00","end":"2026-11-20T00:00:00+09:00"},"seed":42,"daily_driving_minutes":60,"day_off_probability":0.0,"anomaly_vehicle_percent":25.0}


def test_fleet_allocation_and_reproducible_pages(client):
    body=fleet_example()
    before=client.get("/api/runs").json()
    full=client.post("/api/fleets/preview",json=body)
    assert full.status_code==200,full.text
    a=full.json()
    assert len(a["vehicles"])==20 and a["anomaly_target_count"]==5
    assert sum(v["pattern"]=="commute" for v in a["vehicles"])==12
    assert sum(v["pattern"]=="daytime" for v in a["vehicles"])==4
    assert sum(v["pattern"]=="flexible" for v in a["vehicles"])==4
    assert len({t["start_ms"] for t in a["trips"]})>3
    assert a==client.post("/api/fleets/preview",json=body).json()
    page=client.post("/api/fleets/preview?offset=2&limit=3",json=body).json()
    assert page["trips"]==a["trips"][2:5] and page["vehicles"]==a["vehicles"]
    selected=client.post("/api/fleets/preview?vehicle_id=v000001",json=body).json()
    assert selected["trips"]==[t for t in a["trips"] if t["vehicle_id"]=="v000001"]
    changed=dict(body,anomaly_vehicle_percent=0.0)
    b=client.post("/api/fleets/preview",json=changed).json()
    assert b["trips"]==a["trips"] and b["anomaly_target_count"]==0
    assert client.get("/api/runs").json()==before


def test_fleet_parking_duration_and_calendar():
    from services.control.fleet import FleetRequest, expand
    from services.control.schedule import instant
    body=fleet_example()
    body.update(vehicle_count=8,daily_driving_minutes=240,vehicle_variation_percent=50,daily_variation_percent=50,weekend_activity_probability=1.0)
    req=FleetRequest(**body)
    schedule, assigned=expand(req)
    for vehicle in schedule.vehicles:
        trips=sorted(vehicle.trips,key=lambda t: t.start)
        for previous,current in zip(trips,trips[1:]):
            assert instant(current.start)-instant(previous.end)>=1800000
        days={}
        for t in trips:
            assert t.start[:10]==t.end[:10] or t.end[11:19]=="00:00:00"
            days[t.start[:10]]=days.get(t.start[:10],0)+(instant(t.end)-instant(t.start))/60000
        target=assigned[vehicle.vehicle_id]["daily_target_minutes"]
        assert all(target*.5-1<=n<=target*1.5+1 for n in days.values())
    # 같은 날짜의 일정은 관측창 확장만으로 달라지지 않는다(물리 초기 문맥 제한은 별도).
    other=FleetRequest(**dict(body,measurement={"start":"2026-11-16T00:00:00+09:00","end":"2026-11-22T00:00:00+09:00"}))
    wider,_=expand(other)
    for a,b in zip(schedule.vehicles,wider.vehicles):
        original=[t for t in a.trips if t.start.startswith("2026-11-18")]
        assert original==[t for t in b.trips if t.start.startswith("2026-11-18")]


def test_fleet_probability_extremes_limits_and_weekends(client):
    body=fleet_example()
    off=client.post("/api/fleets/preview",json=dict(body,day_off_probability=1.0)).json()
    assert off["planned_messages"]==0 and off["observed_trip_count"]==0
    weekend=dict(body,measurement={"start":"2026-11-21T00:00:00+09:00","end":"2026-11-23T00:00:00+09:00"},weekend_activity_probability=0.0)
    assert client.post("/api/fleets/preview",json=weekend).json()["observed_trip_count"]==0
    for invalid in [dict(body,vehicle_count=101),dict(body,pattern_mix={"commute":100,"daytime":20,"flexible":0}),dict(body,road_mix={"urban":1}),dict(body,daily_driving_minutes=480,vehicle_variation_percent=50,daily_variation_percent=50)]:
        assert client.post("/api/fleets/preview",json=invalid).status_code==422
    large=dict(body,vehicle_count=100,measurement={"start":"2026-11-01T00:00:00+09:00","end":"2026-12-01T00:00:00+09:00"})
    response=client.post("/api/fleets/preview?limit=3",json=large)
    assert response.status_code==200,response.text
    assert len(response.json()["trips"])==3 and response.json()["has_more"]


def test_fleet_obd_uses_fixed_anomaly_assignment(client):
    body=dict(fleet_example(),vehicle_count=1,pattern_mix={"commute":100,"daytime":0,"flexible":0},anomaly_vehicle_percent=100.0,anomaly_events_per_hour=60.0,road_mix={"urban":100,"highway":0,"congested":0,"mixed":0})
    plan=client.post("/api/fleets/preview",json=body).json()
    trip=plan["trips"][0]
    url="/api/fleets/obd/v000001/"+trip["trip_key"]
    response=client.post(url+"?limit=3",json={"fleet":body})
    assert response.status_code==200,response.text
    a=response.json()
    assert a["vehicle_assignment"]["anomaly_eligible"]
    assert all(p["context"]["eligible"] for p in a["trace"])
    assert a["events_until_query_end"]>0
    for message in a["messages"]+[a["trip"]]: control.validate_message(message)
    assert a==client.post(url+"?limit=3",json={"fleet":body}).json()
    body["anomaly_vehicle_percent"]=0.0
    b=client.post(url+"?limit=3",json={"fleet":body}).json()
    assert not b["vehicle_assignment"]["anomaly_eligible"] and b["events_until_query_end"]==0
    assert all(not p["context"]["eligible"] and not p["context"].get("event") for p in b["trace"])
    assert client.post("/api/fleets/obd/v999999/missing",json={"fleet":body}).status_code==422


def long_schedule_body():
    from datetime import date, timedelta
    trips=[]
    for n in range(25):
        day=(date(2026,11,1)+timedelta(days=n)).isoformat()
        trips.append({"start":day+"T08:00:00+09:00","end":day+"T11:00:00+09:00","road_profile":"urban"})
    return {"schedule":{"mode":"manual","seed":42,"interval_ms":2000,
            "measurement":{"start":"2026-11-01T00:00:00+09:00","end":"2026-12-01T00:00:00+09:00"},
            "vehicles":[{"vehicle_id":"v000001","trips":trips}]}}


def finish_obd(client, url, body):
    progress=[]
    for _ in range(100):
        response=client.post(url,json=body)
        assert response.status_code in (200,202),response.text
        if response.status_code==200:
            return response.json(),progress
        progress.append(response.json()["restoration"]["completed_preceding_trips"])
    pytest.fail("상태 준비가 완료되지 않았습니다.")


def test_checkpoint_long_restore_matches_full_and_reuses(client,monkeypatch):
    import services.control.schedule_obd as engine
    body=long_schedule_body()
    rows=client.post("/api/schedules/preview",json=body["schedule"]).json()["trips"]
    url="/api/schedules/obd/v000001/"+rows[-1]["trip_key"]
    calls=[]
    original=engine.subprocess.run
    def measured(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(engine.subprocess,"run",measured)
    a,progress=finish_obd(client,url,body)
    assert progress==[16] and len(calls)==25  # 25회 운행·75시간을 분할 복원
    initial_calls=len(calls)
    assert a==client.post(url,json=body).json() and len(calls)==initial_calls+1
    # 캐시 없이 실제 처음부터 계산한 결과와 완전히 일치한다.
    reference=engine.generate(engine.ScheduledOBDRequest(**body),"v000001",rows[-1]["trip_key"],control.WORKER,None,None,0,100)
    assert a==reference
    page=client.post(url+"?offset=10&limit=5",json=body).json()
    assert page["messages"]==a["messages"][10:15]
    early=client.post("/api/schedules/obd/v000001/"+rows[3]["trip_key"],json=body)
    assert early.status_code==200
    assert client.get("/api/checkpoints").json()["entries"]==25
    # 생성 모델 식별자가 바뀌면 이전 상태를 쓰지 않고 다시 준비한다.
    monkeypatch.setattr(engine,"SOURCE_ID",engine.SOURCE_ID+"-changed")
    again,new_progress=finish_obd(client,url,body)
    assert again==a and new_progress==[16]
    before=client.get("/api/runs").json()
    assert client.delete("/api/checkpoints").json()["entries"]==0
    after,deleted_progress=finish_obd(client,url,body)
    assert after==a and deleted_progress==[16]
    assert client.get("/api/runs").json()==before


def test_checkpoint_store_limits_expiry_restart_and_clear_fence(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from services.control.checkpoints import CheckpointStore,CheckpointReset,CheckpointError
    now=[1000.0]
    path=tmp_path/"states.sqlite3"
    store=CheckpointStore(path,max_entries=3,ttl_seconds=10,clock=lambda:now[0])
    state={"lat":37.5,"lon":127.0,"odometer_m":500000.0,"coolant_c":20.0,"heading_rad":0.0}
    epoch,_=store.resume("a",99)
    for i in range(4):
        now[0]+=1
        store.put("a",i,1000+i,state,epoch)
    assert store.stats()["entries"]==3
    assert store.resume("a",1)[1] is None
    restarted=CheckpointStore(path,max_entries=3,ttl_seconds=10,clock=lambda:now[0])
    assert restarted.resume("a",4)[1]==(3,1003,state)
    assert store.stats()["database_bytes"]<=4194304
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA auto_vacuum").fetchone()[0]==1
        con.execute("UPDATE checkpoints SET state='{}' WHERE seq=3")
    assert store.resume("a",4)[1][0]==2
    now[0]+=11
    assert store.stats()["entries"]==0
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i:store.put("b",i,1000+i,state,epoch),range(10)))
    assert store.stats()["entries"]==3
    store.clear()
    with pytest.raises(CheckpointReset): restarted.put("a",10,2000,state,epoch)
    with pytest.raises(CheckpointError): store.put("a",10,2000,dict(state,lat=float("nan")),epoch+1)
    assert store.stats()["entries"]==0


def test_fleet_lazy_lookup_and_late_checkpoint(client,monkeypatch):
    import services.control.fleet as fleet_engine
    body=dict(fleet_example(),vehicle_count=2,daily_driving_minutes=10,vehicle_variation_percent=0,daily_variation_percent=0,
              measurement={"start":"2026-11-01T00:00:00+09:00","end":"2026-12-01T00:00:00+09:00"},weekend_activity_probability=1.0)
    plan=client.post("/api/fleets/preview?vehicle_id=v000001",json=body).json()
    calls=[]
    original=fleet_engine.iter_vehicle_days
    def watched(req,vehicle,fixed):
        calls.append(vehicle)
        yield from original(req,vehicle,fixed)
    monkeypatch.setattr(fleet_engine,"iter_vehicle_days",watched)
    trip=plan["trips"][-1]
    url="/api/fleets/obd/v000001/"+trip["trip_key"]
    a,progress=finish_obd(client,url,{"fleet":body})
    assert progress and progress==sorted(set(progress))
    assert set(calls)=={"v000001"}  # 다른 차량의 날짜별 일정은 펼치지 않는다.
    reference=fleet_engine.obd(fleet_engine.FleetOBDRequest(fleet=fleet_engine.FleetRequest(**body)),"v000001",trip["trip_key"],control.WORKER,None,None,0,100)
    assert a==reference and client.post(url,json={"fleet":body}).status_code==200
    count=client.get("/api/checkpoints").json()["entries"]
    changed=dict(body,anomaly_events_per_hour=3.0)
    _,new_progress=finish_obd(client,url,{"fleet":changed})
    assert new_progress and client.get("/api/checkpoints").json()["entries"]>count


def test_checkpoint_clear_during_request_and_synthetic_state(client,monkeypatch):
    import json
    import sqlite3
    store=control.CHECKPOINTS
    original=store.put
    once=[True]
    def cleared(*args):
        if once[0]:
            once[0]=False
            store.clear()
        return original(*args)
    monkeypatch.setattr(store,"put",cleared)
    body=scheduled_obd_body()
    key=client.post("/api/schedules/preview",json=body["schedule"]).json()["trips"][0]["trip_key"]
    url="/api/schedules/obd/v000001/"+key
    interrupted=client.post(url,json=body)
    assert interrupted.status_code==409 and client.get("/api/checkpoints").json()["entries"]==0
    old_epoch=0
    assert client.post(url,json=dict(body,checkpoint_epoch=old_epoch)).status_code==409
    body["checkpoint_epoch"]=client.get("/api/checkpoints").json()["epoch"]
    body["force_end_trip"]=True
    body["schedule"]["measurement"]["end"]="2026-11-17T13:00:30+09:00"
    result=client.post(url,json=body).json()
    assert result["synthetic_trip"]
    with sqlite3.connect(store.path) as con:
        end_ms,state=con.execute("SELECT end_ms,state FROM checkpoints").fetchone()
    assert end_ms>result["trip"]["ts"]
    assert json.loads(state)["odometer_m"]>result["trip"]["pld"]["tdis"]


def test_workspace_plan_and_existing_run_share_history(client):
    old=client.post("/api/runs",json=request()).json()
    settings=dict(fleet_example(),vehicle_count=1)
    response=client.post("/api/workspace/plans",json={"title":"도심 출퇴근 확인","fleet":settings})
    assert response.status_code==201,response.text
    plan=response.json()
    assert plan["kind"]=="plan" and plan["state"]=="planned" and plan["transport"]=="local"
    assert "trips" not in plan["result"] and "messages" not in plan["result"]
    assert plan["request"]["fleet"]["seed"]==42
    records=client.get("/api/workspace/records").json()["records"]
    assert {r["key"] for r in records}=={plan["key"],"run:"+old["id"]}
    assert all("request" not in r and "result" not in r for r in records)
    assert client.get("/api/workspace/records/"+plan["key"]).json()==plan
    legacy=client.get("/api/workspace/records/run:"+old["id"]).json()
    assert legacy["request"]==old["request"] and legacy["result"]==old["result"]
    copied=client.post("/api/workspace/plans",json={"title":"새 조건","fleet":dict(settings,seed=123)}).json()
    assert copied["key"]!=plan["key"]
    assert client.get("/api/workspace/records/"+plan["key"]).json()["request"]["fleet"]["seed"]==42
    assert client.get("/api/runs/"+old["id"]).json()==old


def test_workspace_record_delete_is_confirmed_atomic_and_protects_active(client,monkeypatch):
    run=client.post("/api/runs",json=request()).json()
    plan=client.post("/api/workspace/plans",json={"title":"시험 일정","schedule":scheduled_obd_body()["schedule"]}).json()
    keys=[plan["key"],"run:"+run["id"]]
    assert client.post("/api/workspace/delete",json={"keys":keys}).status_code==409
    assert client.post("/api/workspace/delete",json={"keys":keys+["plan:missing"],"confirm":True}).status_code==404
    assert len(client.get("/api/workspace/records").json()["records"])==2
    monkeypatch.setattr(control.JOBS,"active_id",lambda:run["id"])
    assert client.post("/api/workspace/delete",json={"keys":keys,"confirm":True}).status_code==409
    assert client.get("/api/workspace/records/"+plan["key"]).status_code==200
    monkeypatch.setattr(control.JOBS,"active_id",lambda:None)
    assert client.post("/api/workspace/delete",json={"keys":keys,"confirm":True}).json()["deleted_count"]==2
    assert client.get("/api/workspace/records").json()["records"]==[]
    assert client.get("/api/runs/"+run["id"]).status_code==404


def test_workspace_plan_rejects_ambiguous_sources_and_can_replay(client):
    assert client.post("/api/workspace/plans",json={"title":" "}).status_code==422
    assert client.post("/api/workspace/plans",json={"fleet":fleet_example(),"schedule":scheduled_obd_body()["schedule"]}).status_code==422
    assert client.get("/api/workspace/records").json()["records"]==[]
    body=scheduled_obd_body()["schedule"]
    saved=client.post("/api/workspace/plans",json={"title":"다회 운행","schedule":body}).json()
    reloaded=client.get("/api/workspace/records/"+saved["key"]).json()
    plan=client.post("/api/schedules/preview",json=reloaded["request"]["schedule"]).json()
    key=plan["trips"][0]["trip_key"]
    first=client.post("/api/schedules/obd/v000001/"+key,json={"schedule":reloaded["request"]["schedule"]}).json()
    assert first["microtrip_count"]==30 and first["trip"]["ty"]==3


def wait_workspace_job(client, run_id):
    import time
    for _ in range(500):
        run=client.get('/api/jobs/'+run_id).json()
        if run['result']['job_state'] not in control.ACTIVE:
            return run
        time.sleep(.02)
    raise AssertionError('일정 실행이 완료되지 않았습니다.')


def test_workspace_schedule_execution_matches_query_and_mapping(client):
    schedule=scheduled_obd_body()['schedule']
    schedule['measurement']['end']='2026-11-17T13:01:00+09:00'
    saved=client.post('/api/workspace/plans',json={'title':'전체 자동 실행','schedule':schedule}).json()
    rows=client.post('/api/schedules/preview',json=schedule).json()['trips']
    expected=client.post('/api/schedules/obd/v000001/'+rows[0]['trip_key'],json={'schedule':schedule}).json()
    started=client.post('/api/workspace/execute',json={'plan_key':saved['key'],'realtime':False,'mapping_mode':'dedicated'})
    assert started.status_code==202,started.text
    result=wait_workspace_job(client,started.json()['id'])['result']
    assert result['job_state']=='completed',result
    assert result['processed_messages']==result['planned_messages']==31
    assert result['microtrip_count']==30 and result['trip_count']==1
    assert result['samples'][0]['message']==expected['messages'][0]
    assert result['mqtt_published']==result['puback_count']==result['connected_clients']==0
    assert len(result['samples'])<=10
    assert result['assignments'][0]['vehicle_id']=='v000001'
    assert client.get('/api/workspace/records/'+saved['key']).json()==saved


def test_workspace_schedule_execution_rejects_long_plan(client):
    saved=client.post('/api/workspace/plans',json={'title':'장기 일정','fleet':fleet_example()}).json()
    response=client.post('/api/workspace/execute',json={'plan_key':saved['key']})
    assert response.status_code==422
    assert client.get('/api/health').json()['active_job_id'] is None


def test_workspace_schedule_pause_stop_and_no_fake_trip(client):
    import time
    schedule=scheduled_obd_body()['schedule']
    schedule['measurement']['end']='2026-11-17T13:01:00+09:00'
    saved=client.post('/api/workspace/plans',json={'title':'제어 시험','schedule':schedule}).json()
    started=client.post('/api/workspace/execute',json={'plan_key':saved['key']}).json()
    run_id=started['id']
    client.post('/api/jobs/'+run_id+'/pause')
    for _ in range(100):
        result=client.get('/api/jobs/'+run_id).json()['result']
        if result['job_state']=='paused':break
        time.sleep(.02)
    assert result['job_state']=='paused'
    count=result['processed_messages']
    time.sleep(.1)
    assert client.get('/api/jobs/'+run_id).json()['result']['processed_messages']==count
    assert client.post('/api/workspace/delete',json={'keys':['run:'+run_id],'confirm':True}).status_code==409
    client.post('/api/jobs/'+run_id+'/resume')
    client.post('/api/jobs/'+run_id+'/stop')
    result=wait_workspace_job(client,run_id)['result']
    assert result['job_state']=='stopped' and result['trip_count']==0
    assert control.JOBS.active_id() is None


def test_workspace_schedule_interleaves_vehicles_and_forced_boundary(client):
    schedule=scheduled_obd_body()['schedule']
    schedule['measurement']['end']='2026-11-17T13:00:30+09:00'
    schedule['vehicles'].append({'vehicle_id':'v000002','trips':[{'start':'2026-11-17T13:00:04+09:00','end':'2026-11-17T13:01:00+09:00','road_profile':'highway'}]})
    saved=client.post('/api/workspace/plans',json={'title':'경계 시험','schedule':schedule}).json()
    for forced,expected in [(False,30),(True,30)]:
        response=client.post('/api/workspace/execute',json={'plan_key':saved['key'],'realtime':False,'force_end_trip':forced,'client_count':1})
        assert response.status_code==202,response.text
        result=wait_workspace_job(client,response.json()['id'])['result']
        assert result['job_state']=='completed',result
        assert result['planned_messages']==result['processed_messages']==expected
        assert result['trip_count']==(2 if forced else 0)
        assert len({a['client_id'] for a in result['assignments']})==1
        assert len({s['vehicle_id'] for s in result['samples']})==2
        stamps=[s['message']['ts'] for s in result['samples']]
        assert stamps==sorted(stamps)
