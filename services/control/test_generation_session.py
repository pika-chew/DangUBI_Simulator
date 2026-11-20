from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess

import pytest
from services.control.generation_session import GenerationSession,GenerationFailure
from services.control.runtime import RuntimePaths

WORKER=RuntimePaths.from_env(Path(__file__).resolve().parents[2]).worker


def payload():
    start=1794960000000
    return dict(start_ms=start,end_ms=start+600000,from_ms=start,until_ms=start+600000,interval_ms=2000,offset=0,limit=100,tid=1,seed=42,profile="mixed",lat=37.5,lon=127,odometer_m=50000,continuity=dict(vehicle_seed=42,coolant_c=80,heading_rad=0))


def test_worker_identity_is_cached_but_rejects_file_changes(tmp_path):
    import hashlib
    p=tmp_path/"worker";p.write_bytes(b"original")
    s=GenerationSession.__new__(GenerationSession)
    s.worker_path=p.resolve();s.worker_signature=s._signature();s.worker_hash=hashlib.sha256(p.read_bytes()).hexdigest();s.stats={}
    assert s.worker_digest(p)==s.worker_hash
    p.write_bytes(b"modified-longer")
    with pytest.raises(GenerationFailure,match="변경"):s.worker_digest(p)


def test_identity_fast_path_checks_file_without_resolving_again(tmp_path,monkeypatch):
    p=tmp_path/"engine";p.write_bytes(b"engine")
    s=GenerationSession.__new__(GenerationSession)
    s.worker_path=p.resolve();s.worker_signature=s._signature();s.worker_hash="trusted";s.stats={}
    def unexpected(*args,**kwargs):raise AssertionError("동일 절대 경로를 재해석함")
    monkeypatch.setattr(Path,"resolve",unexpected)
    assert s.worker_digest(s.worker_path)=="trusted"
    p.write_bytes(b"changed-engine")
    with pytest.raises(GenerationFailure,match="변경"):s.worker_digest(s.worker_path)


def test_session_matches_all_pages_and_closes():
    s=GenerationSession(WORKER)
    try:
        p=payload();messages=[]
        while True:
            old=json.loads(subprocess.run([str(WORKER)],input=json.dumps(dict(operation="schedule_obd",scheduled=p)).encode(),stdout=subprocess.PIPE,check=True).stdout)["scheduled"]
            page=s.page(p)
            assert "trace" not in page
            assert page["messages"]==old["messages"]
            messages.extend(page["messages"])
            p["offset"]+=len(page["messages"])
            if p["offset"]>=page["microtrip_count"]:
                assert page["trip"]==old["trip"]
                break
            assert page["trip"] is None
        assert len(messages)==300 and len({m["ts"] for m in messages})==300
        assert s.stats["process_starts"]==1 and s.stats["plan_builds"]==1 and s.stats["cache_hits"]==2
    finally:s.close()
    assert s.cleaned and s.close_acknowledged and s.process.poll()==0 and not s.reader.is_alive()
    with pytest.raises(GenerationFailure):s.page(payload())


def test_session_bad_reply_and_timeout_cleanup():
    for failure in ["id","timeout","input"]:
        s=GenerationSession(WORKER)
        try:
            if failure=="id":
                s.responses.put(dict(id=99,ok=True))
                with pytest.raises(GenerationFailure):s._receive(1,.1)
            elif failure=="timeout":
                with pytest.raises(GenerationFailure,match="제한시간"):s._receive(1,.05)
            else:
                p=payload();p["limit"]=101
                with pytest.raises(GenerationFailure):s.page(p)
        finally:s.close(abort=True)
        assert s.cleaned and s.process.poll() is not None and not s.reader.is_alive()


def test_session_stop_during_command_cleans_process():
    job=SimpleNamespace(stop_requested=False)
    s=GenerationSession(WORKER,job)
    job.stop_requested=True
    with pytest.raises(GenerationFailure,match="중단"):s.page(payload())
    assert s.cleaned and not s.forced and not s.reader.is_alive()


def test_session_eof_and_unresponsive_stop_cleanup(monkeypatch):
    s=GenerationSession(WORKER)
    s.process.stdin.close()
    s.process.wait(timeout=2)
    with pytest.raises(GenerationFailure):s._receive(1,.2)
    s.close(abort=True)
    assert s.cleaned and not s.reader.is_alive()
    s=GenerationSession(WORKER)
    monkeypatch.setattr(s,"_write",lambda command:None)
    s.close(abort=True)
    assert s.cleaned and s.forced and s.process.poll() is not None and not s.reader.is_alive()


def test_byte_limited_pages_keep_every_message_and_final_trip():
    s=GenerationSession(WORKER)
    try:
        p=payload();observed=[];sizes=[]
        while True:
            page=s.page(p,byte_limit=16384);sizes.append(len(page["messages"]))
            observed.extend(page["messages"]);p["offset"]+=len(page["messages"])
            if p["offset"]>=page["microtrip_count"]:
                final=page["trip"];break
        assert len(sizes)>3 and max(sizes)<100
        expected=[];p=payload()
        for offset in range(0,300,100):
            p["offset"]=offset
            old=json.loads(subprocess.run([str(WORKER)],input=json.dumps(dict(operation="schedule_obd",scheduled=p)).encode(),stdout=subprocess.PIPE,check=True).stdout)["scheduled"]
            expected.extend(old["messages"])
        assert observed==expected and final==old["trip"]
    finally:s.close()
    assert s.cleaned and s.close_acknowledged
