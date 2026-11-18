"""로컬 계측 전용 입구. 공개 API의 계획/송신 상한을 변경하지 않는다."""
from functools import partial
from types import SimpleNamespace
from uuid import uuid4


def prepare(source,case,worker,profile,validate):
    from scripts.measure_schedule import local_profile
    local_profile(profile) # 외부 주소는 확장 플래그와 무관하게 먼저 거절한다.
    from services.control.schedule import ScheduleRequest,plan,instant
    from services.control.schedule_cursor import provider_for
    from services.control.schedule_obd import ScheduledOBDRequest,generate
    from services.control.execution_checkpoints import ExecutionCheckpoints
    from services.control.schedule_execution import execute
    if case.get("local_scale") is not True or not 1<=len(source["vehicles"])<=1000:raise ValueError("로컬 확장 계측 범위를 확인하세요.")
    if sum(len(v.get("trips",[])) for v in source["vehicles"])>10000:raise ValueError("문맥 운행은10000회 이하입니다.")
    # 차량별 기존 계약/주차 검증을 유지한다. 전체 계획을 매 페이지마다 다시 검사하지 않는다.
    requests={};providers={};planned=0
    for vehicle in source["vehicles"]:
        req=ScheduleRequest.model_validate(dict(source,vehicles=[vehicle]))
        planned+=plan(req)["planned_messages"]
        v=vehicle["vehicle_id"]
        if v in requests:raise ValueError("중복 차량입니다.")
        requests[v]=ScheduledOBDRequest(schedule=req,checkpoint_epoch=0)
        providers[v]=provider_for(SimpleNamespace(schedule=req,fleet=None))
    if planned>10000:raise ValueError("로컬 확장 계측은10000건 이하입니다.")
    states=ExecutionCheckpoints();run_id=uuid4().hex
    assignments=[dict(vehicle_id=v,client_id=f"sim-{run_id}-c{i%case['clients']+1:04d}") for i,v in enumerate(requests)]
    def page(v,key,offset,session=None,limit=100):
        return generate(requests[v],v,key,worker,None,None,offset,limit,checkpoint_store=states,session=session)
    start=instant(source["measurement"]["start"]);end=instant(source["measurement"]["end"])
    config=dict(vehicle_count=len(requests),client_count=case["clients"],mapping_mode="dedicated" if case["clients"]==len(requests) else "shared",messages_per_vehicle=0,interval_ms=source["interval_ms"],connection_lifetime=case.get("lifetime","always"))
    data=dict(operation="mqtt",config=config,planned_messages=planned,assignments=assignments,
              client_by_vehicle={a["vehicle_id"]:a["client_id"] for a in assignments},generate=page,
              trip_provider=lambda v:providers[v](v),measurement_start_ms=start,duration_ms=end-start,realtime=True,
              validate_message=validate,mqtt=profile,generation_workers=case["workers"],generation_queue_messages=2000,
              generation_queue_bytes=8388608,execution_checkpoint_stats=states.stats)
    def start_run(manager):
        return manager.start(run_id,dict(local_scale=True,config=config),data,worker,120,runner=partial(execute,manager))
    return start_run
