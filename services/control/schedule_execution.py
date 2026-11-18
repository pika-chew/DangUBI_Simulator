"""일정 실행: 선택 조회와 동일한 생성기를 사용하고 제한된 페이지와 샘플만 유지한다."""
import json
import time
from services.control.mqtt_bridge import MQTTBridge, MQTTFailure
from services.control.generation_session import GenerationFailure
from services.control.generation_pool import GenerationPool
from services.control.bounded_schedule import BoundedSchedule
from services.control.schedule_publisher import SchedulePublisher


def execute(manager, job, data, worker, timeout):
    started = time.monotonic()
    paused_seconds = 0.0
    result = job.result.copy()
    result.update(assignments=data["assignments"], model_version="schedule-obd-3/journey-8",
                  microtrip_count=0, trip_count=0, schedule_delay_ms=0)
    last_save = 0.0
    result["execution_timing"]={}
    mqtt=data["operation"]=="mqtt"
    bridge=None
    publisher=None
    generation=None
    bounded=None
    sequences={}
    cleanup=True
    result["transport"]="mqtt" if mqtt else "dry_run"
    if mqtt:
        result.update(unsent_messages=result["planned_messages"], connection_count=0, disconnect_count=0)

    def budget():
        if not mqtt:return
        g=bounded.stats if bounded else {}
        p=publisher.state if publisher else {}
        count=g.get("reserved_messages",0)+p.get("reserved_commands",0)
        size=g.get("reserved_bytes",0)+p.get("reserved_bytes",0)
        b=result.setdefault("buffer_budget",dict(max_messages=data.get("generation_queue_messages",2000)+100,
                    max_bytes=data.get("generation_queue_bytes",8388608)+1048576,
                    peak_reserved_messages=0,peak_reserved_bytes=0))
        if count>b["max_messages"] or size>b["max_bytes"]:raise GenerationFailure("생성·송신 통합 예약 예산을 초과했습니다.")
        b.update(reserved_messages=count,reserved_bytes=size,generation_reserved_messages=g.get("reserved_messages",0),
                 generation_reserved_bytes=g.get("reserved_bytes",0),transmission_reserved_commands=p.get("reserved_commands",0),
                 transmission_reserved_bytes=p.get("reserved_bytes",0))
        b["peak_reserved_messages"]=max(b["peak_reserved_messages"],count)
        b["peak_reserved_bytes"]=max(b["peak_reserved_bytes"],size)

    def sync_mqtt():
        if bridge:
            result.update(bridge.stats)
            result["unsent_messages"]=max(0,result["planned_messages"]-result["publish_attempts"])

    def finish(state):
        nonlocal cleanup
        cleanup_started=time.monotonic()
        if bridge:
            bridge.close(abort=job.stop_requested or state!="completed")
            sync_mqtt()
            cleanup=bridge.cleaned
            if publisher:publisher.discard()
            if (not cleanup or not bridge.close_acknowledged) and state=="completed":
                state="failed"
                result["errors"]=["MQTT 정상 종료 응답 또는 연결 정리의 최종 상태를 확인하지 못했습니다."]
        if generation:
            generation.close(abort=job.stop_requested or state!="completed")
            result["generation_session"]=dict(generation.stats,cleanup_confirmed=generation.cleaned,forced_termination=generation.forced or job.forced)
            if bounded:bounded.close()
            if bounded:result["generation_pool"]={"assignments":bounded.assignments,"queue":bounded.stats.copy(),"workers":[dict(s.stats,worker_index=i,work_ms=generation.work_ms[i]) for i,s in enumerate(generation.sessions)],"execution_checkpoints":data["execution_checkpoint_stats"]()}
            if state=="completed" and job.stop_requested:state="failed" if job.timed_out else "stopped"
            if state=="completed" and (not generation.cleaned or not generation.close_acknowledged):
                state="failed"
                result["errors"]=["생성 세션 종료 응답 또는 프로세스 정리를 확인하지 못했습니다."]
        result["execution_timing"]["cleanup_ms"]=(time.monotonic()-cleanup_started)*1000
        report(state,True)

    def report(state="running", final=False, throttle=False):
        nonlocal last_save
        # 연결 경계가 몰려도 DB에 차량 수만큼 연속 저장하지 않는다.
        # 최종 상태와 상태 전환은 제한하지 않으며 gate의 중단/일시정지 확인도 유지한다.
        if throttle and not final and result.get("job_state")==state and time.monotonic()-last_save<.1:return
        if bounded:result["generation_pool"]={"assignments":bounded.assignments,"queue":bounded.stats.copy(),"workers":[dict(s.stats,worker_index=i,work_ms=generation.work_ms[i]) for i,s in enumerate(generation.sessions)],"execution_checkpoints":data["execution_checkpoint_stats"]()}
        budget()
        elapsed = time.monotonic()-started
        result.update(elapsed_ms=int(elapsed*1000), paused_ms=int(paused_seconds*1000),
                      active_elapsed_ms=int((elapsed-paused_seconds)*1000),
                      remaining_messages=max(0,result["planned_messages"]-result["processed_messages"]),
                      job_state=state, state=("mqtt_completed" if mqtt else "dry_run_completed") if final and state=="completed" else "mqtt_failed" if mqtt and final and state=="failed" else state,
                      connection_cleanup_confirmed=final and cleanup,
                      execution_cleanup_confirmed=final and cleanup and (generation.cleaned if generation else True),
                      forced_termination=job.forced or bool(bridge and bridge.forced) or bool(generation and generation.forced))
        with manager.lock:
            job.result = result.copy()
            manager.save(job)
        last_save = time.monotonic()

    def gate(due=None):
        nonlocal paused_seconds
        while True:
            now=time.monotonic()
            if now-started >= timeout:
                job.timed_out=True
                job.stop_requested=True
            if job.stop_requested:
                return False
            if publisher:
                publisher.pump(allow_start=not job.pause_requested,elapsed_ms=(now-started-paused_seconds)*1000)
                sync_mqtt()
            if job.pause_requested:
                before=time.monotonic()
                state="pausing" if publisher and publisher.active else "paused"
                if result.get("job_state") != state or before-last_save >= .5:
                    report(state)
                time.sleep(.05)
                paused_seconds += time.monotonic()-before
                continue
            elapsed=now-started-paused_seconds
            result["simulation_time_ms"]=min(data["duration_ms"],int(elapsed*1000)) if data["realtime"] else result.get("simulation_time_ms",0)
            # 짧은 ACK 제한시간 안에도 중단 가능한 진행 상태를 확인한다.
            # 메시지마다 저장하지 않고 진행 중 MQTT 명령이 있을 때만 최대10회/초로 갱신한다.
            if now-last_save >= (.1 if publisher and publisher.active else .5):
                report()
            if due is None or not data["realtime"] or elapsed*1000 >= due:
                return True
            time.sleep(min(.05,max(0,(due-elapsed*1000)/1000)))

    due_offset=data.get("due_offset",lambda ts:ts-data["measurement_start_ms"])

    def consume(timestamp,vehicle,message,synthetic):
        if mqtt:
            if synthetic:raise MQTTFailure("시험용 Trip은 외부 전송할 수 없습니다.")
            data["validate_message"](message)
            if not publisher.enqueue("publish",gate,vehicle_id=vehicle,message=message):return
        due=due_offset(timestamp)
        sequences[vehicle]=sequences.get(vehicle,0)+1
        result["processed_messages"]+=1
        result["payload_bytes"]+=len(json.dumps(message,separators=(",",":"),ensure_ascii=False).encode())
        result["trip_count" if message["ty"]==3 else "microtrip_count"]+=1
        result["simulation_time_ms"]=int(due)
        if data["realtime"]:
            result["schedule_delay_ms"]=max(result["schedule_delay_ms"],int((time.monotonic()-started-paused_seconds)*1000-due))
        if len(result["samples"])<10:
            result["samples"].append(dict(vehicle_id=vehicle,client_id=data["client_by_vehicle"][vehicle],topic=f"{data["mqtt"]["topic_prefix"]}/{job.id}/{vehicle}/obd" if mqtt else "",sequence=sequences[vehicle],message=message,synthetic_trip=synthetic))

    try:
        report()
        generation=GenerationPool(worker,job,manager,data.get("generation_workers",1))
        if mqtt:
            bridge=MQTTBridge(worker,data,job)
            bridge.receive()
            publisher=SchedulePublisher(bridge,result,data["client_by_vehicle"],data["measurement_start_ms"],on_account=budget,due_offset=due_offset)
            if data["config"]["connection_lifetime"]=="always":
                for client in sorted(set(data["client_by_vehicle"].values())):
                    if not gate():break
                    publisher.enqueue("connect",gate,client_id=client)
                    sync_mqtt();report(throttle=True)
        bounded=BoundedSchedule(data,generation,gate,result,include_boundaries=mqtt and data["config"]["connection_lifetime"]=="while_driving",on_account=budget)
        replay_started=time.monotonic()
        result["execution_timing"]["preparation_ms"]=(replay_started-started)*1000
        if not mqtt:
            for timestamp,vehicle,message,synthetic in bounded:consume(timestamp,vehicle,message,synthetic)
        else:
            driving={}
            for event in bounded:
                timestamp,priority,vehicle,message,synthetic=event if bounded.include_boundaries else (event[0],1,*event[1:])
                if priority!=1:
                    client=data["client_by_vehicle"][vehicle]
                    before=driving.get(client,0)
                    driving[client]=before+(1 if priority==0 else -1)
                    if priority==0 and before==0:
                        publisher.enqueue("connect",gate,client_id=client)
                    elif priority==2 and driving[client]==0:
                        publisher.enqueue("disconnect",gate,client_id=client)
                    sync_mqtt();report(throttle=True)
                    continue
                consume(timestamp,vehicle,message,synthetic)
        result["execution_timing"]["replay_ms"]=(time.monotonic()-replay_started)*1000
        wait_started=time.monotonic()
        # 마지막 운행 뒤에도 측정 종료까지 주차 시간을 진행한다.
        if not job.stop_requested:
            gate(data["duration_ms"])
        result["execution_timing"]["measurement_wait_ms"]=(time.monotonic()-wait_started)*1000
        drain_started=time.monotonic()
        if publisher and not job.stop_requested:
            publisher.drain(gate)
        result["execution_timing"]["drain_ms"]=(time.monotonic()-drain_started)*1000
        if not job.stop_requested:
            result["simulation_time_ms"]=data["duration_ms"]
        if job.timed_out:
            result["errors"]=["실행 제한시간을 초과했습니다."]
        finish("failed" if job.timed_out else "stopped" if job.stop_requested else "completed")
    except Exception as error:
        if mqtt:sync_mqtt()
        result["errors"]=[str(error) if isinstance(error,(MQTTFailure,GenerationFailure)) else "일정 생성 또는 전송에 실패했습니다. 일부 결과만 보관하며 자동 재실행하지 않습니다."]
        finish("failed" if job.timed_out else "stopped" if job.stop_requested else "failed")
    finally:
        if generation:generation.close(abort=True)
        if bounded:bounded.close()
        with manager.lock:
            if manager.active is job:
                manager.active=None
            if job.mqtt:manager.mqtt_lock.release()
            job.finished.set()
