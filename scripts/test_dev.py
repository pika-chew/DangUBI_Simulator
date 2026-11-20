"""검사가 사용자 Worker·DB·접속 설정을 재사용하거나 덮어쓰지 않는지 확인한다."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import dev


class IsolatedTestRunnerTests(unittest.TestCase):
    def check_isolation(self,fail):
        with tempfile.TemporaryDirectory() as name:
            local=Path(name)/"local";local.mkdir()
            original=local/("worker.exe" if os.name=="nt" else "worker")
            original.write_bytes(b"user-worker")
            captured=[]
            def run(args,**kwargs):
                if args[:2]==["go","build"]:Path(args[3]).write_bytes(b"test-worker")
                if "pytest" in args:
                    env=kwargs["env"].copy();captured.append(env)
                    self.assertNotEqual(Path(env["DANGUBI_WORKER"]),original)
                    for key in ("DANGUBI_DB","DANGUBI_CHECKPOINT_DB","DANGUBI_MQTT_PROFILE"):
                        self.assertTrue(Path(env[key]).is_relative_to(Path(env["DANGUBI_WORKER"]).parent))
                    if fail:raise RuntimeError("expected failure")
            with patch.object(dev,"LOCAL",local),patch.object(dev,"run",run),patch.dict(os.environ,{"DANGUBI_DB":"user-db","DANGUBI_MQTT_PROFILE":"user-profile"}):
                if fail:
                    with self.assertRaises(RuntimeError):dev.test()
                else:dev.test()
                self.assertEqual(os.environ["DANGUBI_DB"],"user-db")
                self.assertEqual(os.environ["DANGUBI_MQTT_PROFILE"],"user-profile")
            self.assertEqual(original.read_bytes(),b"user-worker")
            self.assertFalse(Path(captured[0]["DANGUBI_WORKER"]).parent.exists())

    def test_success_keeps_user_files_and_cleans_temporary_files(self):self.check_isolation(False)
    def test_failure_also_cleans_only_temporary_files(self):self.check_isolation(True)
