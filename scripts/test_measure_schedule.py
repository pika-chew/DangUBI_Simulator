"""측정 범위·자료형·언어 간 지문과 하위 프로세스 정리를 검사한다."""
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import measure_schedule as measurement
from scripts.measurement_process import ProcessTree


class ScheduleMeasurementTest(unittest.TestCase):
    def setUp(self):
        self.config=json.loads((measurement.ROOT/"configs/schedule-measurement.example.json").read_text())

    def test_matrix_and_invalid_workloads(self):
        cases,repeats=measurement.validate_config(self.config)
        self.assertEqual((len(cases),repeats),(18,2))
        self.assertEqual({(c["workers"],c["clients"],c["lifetime"]) for c in cases[:12]},
                         {(w,c,l) for w in [1,2,4] for c in [2,10] for l in ["always","while_driving"]})
        for field,value in [("broker_url","ssl://example.com:8883"),("workers",True),("workers",3),("vehicles",21),("clients",11),("history_minutes",121),("history_minutes",1.5),("fault","bad")]:
            bad=copy.deepcopy(self.config);bad["cases"][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):measurement.validate_config(bad)
        bad=copy.deepcopy(self.config);bad["cases"][1]["name"]=bad["cases"][0]["name"]
        with self.assertRaises(ValueError):measurement.validate_config(bad)

    def test_profile_restricts_fixture_to_normal_loopback(self):
        profile=measurement.local_profile(dict(broker_url="ssl://127.0.0.1:9999",local_benchmark=True))
        self.assertNotIn("local_benchmark",profile)
        self.assertEqual((profile["max_clients"],profile["max_messages"],profile["max_duration_seconds"]),(20,100,30))
        for url in ["ssl://localhost:9999","ssl://example.com:8883","tcp://127.0.0.1:9999","ssl://user@127.0.0.1:9999","ssl://127.0.0.1:9999/path"]:
            with self.subTest(url=url),self.assertRaises(ValueError):measurement.local_profile(dict(broker_url=url))

    def test_internal_child_cannot_modify_user_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/"profile.json";p.write_text("original")
            with self.assertRaises(ValueError):measurement.child(self.config["cases"][0],p,Path(sys.executable),Path(folder))
            self.assertEqual(p.read_text(),"original")

    def test_scale_is_explicit_and_public_contracts_stay_limited(self):
        from services.control.schedule import ScheduleRequest
        from services.control.app import MQTTProfile,ScheduleExecutionRequest
        from pydantic import ValidationError
        c=dict(name="scale",local_scale=True,vehicles=1000,clients=1000,workers=4)
        measurement.validate_config(dict(cases=[c],repeats=1))
        with self.assertRaises(ValueError):measurement.validate_config(dict(cases=[dict(c,local_scale=False)]))
        with self.assertRaises(ValueError):measurement.validate_config(dict(cases=[dict(c,vehicles=1001)]))
        with self.assertRaises(ValidationError):ScheduleExecutionRequest(plan_key="test",client_count=1000)
        with self.assertRaises(ValidationError):MQTTProfile(broker_url="ssl://127.0.0.1:1",ca_file="a",cert_file="b",key_file="c",max_clients=1000)
        self.assertEqual(ScheduleRequest.model_fields["vehicles"].metadata[1].max_length,100)

    def test_sustained_local_case_respects_existing_message_and_time_limits(self):
        case=dict(name="sustained",vehicles=160,clients=160,workers=2,local_scale=True,driving_seconds=90)
        measurement.validate_config(dict(cases=[case]))
        for changes in (dict(vehicles=218),dict(driving_seconds=91),dict(driving_seconds=True),
                        dict(vehicles=10,clients=10,local_scale=False)):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                measurement.validate_config(dict(cases=[dict(case,**changes)]))

    def test_staggered_plan_preserves_one_trip_per_vehicle_and_observation_tail(self):
        cases,_=measurement.validate_config(json.loads(
            (measurement.ROOT/"configs/schedule-staggered.example.json").read_text()))
        pooled,dedicated=map(measurement.schedule_source,cases)
        self.assertEqual(pooled,dedicated)
        starts=[v["trips"][0]["start"] for v in pooled["vehicles"]]
        self.assertEqual([starts.count(start) for start in sorted(set(starts))],[40]*4)
        from services.control.schedule import instant
        base=instant(pooled["measurement"]["start"])
        self.assertEqual([instant(at)-base for at in sorted(set(starts))],[0,20000,40000,60000])
        self.assertEqual(instant(pooled["measurement"]["end"])-base,74000)
        rejected=(dict(departure_groups=True),dict(departure_groups=6),dict(driving_seconds=16),
                  dict(history_minutes=1),dict(vehicles=10,clients=10,local_scale=False))
        for changes in rejected:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                measurement.validate_config(dict(cases=[dict(cases[0],**changes)]))

    def test_real_parking_case_bounds_and_same_vehicle_schedule(self):
        from services.control.schedule import instant
        config=json.loads((measurement.ROOT/"configs/schedule-parking.example.json").read_text())
        cases,_=measurement.validate_config(config)
        first,second=map(measurement.schedule_source,cases)
        self.assertEqual(first,second)
        for vehicle in first["vehicles"]:
            morning,afternoon=vehicle["trips"]
            self.assertEqual(instant(afternoon["start"])-instant(morning["end"]),1802000)
        self.assertEqual(instant(first["measurement"]["end"])-instant(first["measurement"]["start"]),1828000)
        rejected=(dict(vehicles=9),dict(local_scale=False),dict(parking_replay=1),dict(lifetime="always"),
                  dict(history_minutes=1),dict(departure_groups=2),dict(driving_seconds=13),dict(fault="delay_ack"))
        for changes in rejected:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                measurement.validate_config(dict(cases=[dict(cases[0],**changes)],repeats=1))
        with self.assertRaises(ValueError):measurement.validate_config(dict(cases=cases,repeats=2))

    def test_fingerprint_is_numeric_and_key_order_independent(self):
        m=dict(ty=4,ts=1,pld=dict(sp=1.5,flags=[True,None,"한"]))
        self.assertEqual(hashlib.sha256(measurement.canonical(m)).hexdigest(),"bd88e197fcbbc651353e7db126e41c922dd7544b86a08c0082f45eda8aae25ce")
        self.assertEqual(measurement.canonical(1),measurement.canonical(1.0))
        self.assertEqual(measurement.canonical(-0.0),measurement.canonical(0))
        self.assertNotEqual(measurement.canonical(True),measurement.canonical(1))

    def test_comparison_rejects_worker_dependent_payloads(self):
        def row(name,digest):
            return dict(case=dict(name=name,vehicles=2,clients=1,workers=1),fingerprint=digest,accepted_messages=10,
                        integrity_passed=True,cleanup_passed=True,resources=dict(cpu_seconds=1,observed_peak_process_rss_sum_bytes=1048576),
                        result=dict(schedule_delay_ms=100,performance=dict(ack=dict(mean_ms=2))))
        rows=[row("w1","same"),row("w2","same")]
        comparison=measurement.compare(dict(results=rows))
        self.assertTrue(comparison["determinism_passed"])
        self.assertFalse(comparison["scale_expansion_approved"])
        rows[1]["fingerprint"]="different"
        self.assertFalse(measurement.compare(dict(results=rows))["determinism_passed"])

    @unittest.skipUnless(os.name=="nt","Windows Job Object 시험")
    def test_tree_cleanup_terminates_grandchild(self):
        from ctypes import wintypes as w
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
        kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.WaitForSingleObject.restype=w.DWORD
        kernel.CloseHandle.argtypes=[w.HANDLE]
        with tempfile.TemporaryDirectory(dir=measurement.ROOT/".local") as folder:
            gate=Path(folder)/"ready";pidfile=Path(folder)/"pid"
            code="import subprocess,sys,time;from pathlib import Path\nwhile not Path(sys.argv[1]).exists():time.sleep(.01)\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path(sys.argv[2]).write_text(str(p.pid));time.sleep(60)"
            tree=ProcessTree();handle=None
            try:
                tree.start([sys.executable,"-c",code,str(gate),str(pidfile)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);gate.touch()
                deadline=time.monotonic()+5
                while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(pidfile.exists())
                handle=kernel.OpenProcess(0x100000|0x1000,False,int(pidfile.read_text()))
                self.assertTrue(handle)
                tree.close()
                self.assertEqual(kernel.WaitForSingleObject(handle,2000),0)
            finally:
                tree.close()
                if handle:kernel.CloseHandle(handle)

    def test_resources_measure_current_process(self):
        resources=measurement.Resources([])
        started=time.monotonic()
        while time.monotonic()-started<.04:hashlib.sha256(b"test").digest()
        result=resources.finish(time.monotonic()-started)
        self.assertIsNotNone(result["cpu_seconds"])
        self.assertGreater(result["observed_peak_process_rss_sum_bytes"],0)
        self.assertTrue(result["all_children_exited"])
        self.assertFalse(resources.thread.is_alive())


if __name__=="__main__":unittest.main()
