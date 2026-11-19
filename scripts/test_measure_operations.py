"""계측 조건과 피크 일정의 경계·원상복원을 검증한다."""
import unittest
from scripts.measure_operations import validate,feasibility,peak_schedule
from services.control.fleet import FleetRequest,preview
from services.control import fleet,fleet_execution

def case(**patch):
    value=dict(stage="generation",vehicles=25000,clients=500,workers=1,window_seconds=60,
        wall_seconds=60,rss_mib=4096,publish_per_second=15000,phase="aligned")
    value.update(patch);return value

class OperationsMeasurementTests(unittest.TestCase):
    def test_rejects_unbounded_or_external_settings(self):
        validate(case())
        for patch in [dict(vehicles=25001),dict(workers=3),dict(wall_seconds=121),
                      dict(rss_mib=4097),dict(clients=25001),dict(broker="ssl://external:8883"),
                      dict(window_seconds=0),dict(stage="aws"),dict(phase="unknown")]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):validate(case(**patch))

    def test_rate_limit_feasibility_is_separate_from_measurement(self):
        report=feasibility(case())
        self.assertEqual(report["target_microtrips_per_second"],12500)
        self.assertGreater(report["aligned_burst_p95_service_floor_seconds"],1)
        self.assertIsNone(feasibility(case(phase="spread"))["aligned_burst_p95_service_floor_seconds"])

    def test_peak_schedule_uses_operating_plan_and_restores_functions(self):
        originals=fleet.iter_vehicle_days,fleet_execution.iter_vehicle_days
        req=FleetRequest(execution_profile="operations",fleet_version="fleet-plan-4",vehicle_count=3,
            measurement=dict(start="2026-11-19T08:00:00+09:00",end="2026-11-19T08:00:04+09:00"))
        for phase,want in [("aligned",9),("spread",7)]:
            with peak_schedule(case(phase=phase)):
                result=preview(req)
                self.assertEqual(result["microtrip_count"],want)
                self.assertEqual(result["trip_count"],0)
            self.assertEqual((fleet.iter_vehicle_days,fleet_execution.iter_vehicle_days),originals)
        with self.assertRaises(RuntimeError):
            with peak_schedule(case()):raise RuntimeError("중단")
        self.assertEqual((fleet.iter_vehicle_days,fleet_execution.iter_vehicle_days),originals)

if __name__=="__main__":unittest.main()
