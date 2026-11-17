"""측정 상한과 주소 입력 경로의 경계를 확인한다."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("measure_mqtt", Path(__file__).with_name("measure_mqtt.py"))
measurement = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measurement)


class MeasurementConfigTest(unittest.TestCase):
    def setUp(self):
        self.example = json.loads((measurement.ROOT / "configs/measurement.example.json").read_text(encoding="utf-8"))

    def test_example_and_repeats(self):
        cases, repeats = measurement.validate_config(self.example)
        self.assertEqual((len(cases), repeats), (8, 1))
        self.assertEqual(cases[2]["vehicles"], cases[3]["vehicles"])
        self.assertEqual(cases[2]["interval_ms"], cases[3]["interval_ms"])

    def test_remote_address_and_workload_limits(self):
        for key, value in [("broker_url", "ssl://example.com:8883"), ("vehicles", 101), ("vehicles", True), ("clients", 101), ("duration_seconds", 61), ("interval_ms", 7), ("mode", "dynamic"), ("fault", "unknown")]:
            with self.subTest(key=key, value=value):
                bad = copy.deepcopy(self.example)
                bad["cases"][0][key] = value
                with self.assertRaises(ValueError):
                    measurement.validate_config(bad)

    def test_budget_and_bad_shapes(self):
        for value in [[], {}, {"cases": [1]}, {"cases": self.example["cases"], "repeats": 4}]:
            with self.assertRaises(ValueError):
                measurement.validate_config(value)
        bad = copy.deepcopy(self.example)
        bad["cases"][0].update(vehicles=100, clients=100, mode="dedicated", duration_seconds=60, interval_ms=100)
        with self.assertRaises(ValueError):
            measurement.validate_config(bad)


if __name__ == "__main__":
    unittest.main()
