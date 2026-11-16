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
    return {"config": {"vehicle_count": 3, "mapping_mode": mode, "client_count": clients, "messages_per_vehicle": 2, "interval_ms": 2000, "connection_lifetime": "always"}, "message": copy.deepcopy(control.CATALOG["profiles"][ty]["example"]), "transport": "dry_run"}


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
    lambda x: x.update(transport="mqtt"),
    lambda x: x["config"].update(mapping_mode="dedicated"),
    lambda x: x["config"].update(messages_per_vehicle=10000),
    lambda x: x["config"].update(vehicle_count=True),
])
def test_invalid_never_stored(client, mutate):
    body = request()
    mutate(body)
    assert client.post("/api/runs", json=body).status_code == 422
    assert client.get("/api/runs").json() == []
