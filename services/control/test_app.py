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
