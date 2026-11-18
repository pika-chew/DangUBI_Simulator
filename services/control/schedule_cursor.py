"""저장된 일정의 차량별 현재·다음 운행을 순차 조회한다."""
from services.control.schedule import iter_vehicle_trips,observed_row,instant
from services.control.fleet import allocation,iter_vehicle_days

def provider_for(source):
    req=source.fleet or source.schedule
    start,end=instant(req.measurement.start),instant(req.measurement.end)
    if source.fleet:
        assigned=allocation(req)
        def provide(vehicle):
            for t in iter_vehicle_days(req,vehicle,assigned[vehicle]):
                row=observed_row(vehicle,instant(t["start"]),instant(t["end"]),t["road_profile"],start,end,req.interval_ms)
                if row is not None:yield row
    else:
        vehicles={v.vehicle_id:v for v in req.vehicles}
        def provide(vehicle):
            for a,b,road in iter_vehicle_trips(req,vehicles[vehicle]):
                row=observed_row(vehicle,a,b,road,start,end,req.interval_ms,req.schedule_version)
                if row is not None:yield row
    return provide
