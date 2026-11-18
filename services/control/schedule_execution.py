"""일정 실행: 선택 조회와 동일한 생성기를 사용하고 제한된 페이지와 샘플만 유지한다."""
import heapq
import json
import time
from services.control.mqtt_bridge import MQTTBridge, MQTTFailure
from services.control.generation_session import GenerationFailure
from services.control.generation_pool import GenerationPool
from services.control.bounded_schedule import BoundedSchedule


def execute(manager, job, data, worker, timeout):
    started = time.monotonic()
    paused_seconds = 0.0
    result = job.result.copy()
    result.update(assignments=data["assignments"], model_version="schedule-obd-3/journey-8",
                  microtrip_count=0, trip_count=0, schedule_delay_ms=0)
    last_save = 0.0
    mqtt=data["operation"]=="mqtt"
    bridge=None
    generation=None
    bounded=None
    sequences={}
    cleanup=True
    result["transport"]="mqtt" if mqtt else "dry_run"
    if mqtt:
        result.update(unsent_messages=result["planned_messages"], connection_count=0, disconnect_count=0,
                      transmission={"mode":"serial","max_in_flight":1,"queue_capacity":1,"queued_messages":0,"in_flight":0,"peak_queued":0,"peak_in_flight":0,"backpressure_count":0,"discarded_queued":0})

    def sync_mqtt():
        if bridge:
            result.update(bridge.stats)
            result["unsent_messages"]=max(0,result["planned_messages"]-result["publish_attempts"])

    def finish(state):
        nonlocal cleanup
        if bridge:
            bridge.close()
            sync_mqtt()
            cleanup=bridge.cleaned
            result["transmission"]["in_flight"]=0
            if not cleanup and state=="completed":
                state="failed"
                result["errors"]=["MQTT 연결 정리의 최종 상태를 확인하지 못했습니다."]
        if generation:
            generation.close(abort=job.stop_requested or state!="completed")
            result["generation_session"]=dict(generation.stats,cleanup_confirmed=generation.cleaned,forced_termination=generation.forced or job.forced)
            if bounded:bounded.close()
            if bounded:result["generation_pool"]={"assignments":bounded.assignments,"queue":bounded.stats.copy(),"workers":[dict(s.stats,worker_index=i,work_ms=generation.work_ms[i]) for i,s in enumerate(generation.sessions)],"execution_checkpoints":data["execution_checkpoint_stats"]()}
            if state=="completed" and job.stop_requested:state="failed" if job.timed_out else "stopped"
            if state=="completed" and (not generation.cleaned or not generation.close_acknowledged):
                state="failed"
                result["errors"]=["생성 세션 종료 응답 또는 프로세스 정리를 확인하지 못했습니다."]
        report(state,True)

    def report(state="running", final=False):
        nonlocal last_save
        if bounded:result["generation_pool"]={"assignments":bounded.assignments,"queue":bounded.stats.copy(),"workers":[dict(s.stats,worker_index=i,work_ms=generation.work_ms[i]) for i,s in enumerate(generation.sessions)],"execution_checkpoints":data["execution_checkpoint_stats"]()}
        elapsed = time.monotonic()-started
        result.update(elapsed_ms=int(elapsed*1000), paused_ms=int(paused_seconds*1000),
                      active_elapsed_ms=int((elapsed-paused_seconds)*1000),
                      remaining_messages=max(0,result["planned_messages"]-result["processed_messages"]),
                      job_state=state, state=("mqtt_completed" if mqtt else "dry_run_completed") if final and state=="completed" else "mqtt_failed" if mqtt and final and state=="failed" else state,
                      connection_cleanup_confirmed=final and cleanup, forced_termination=bridge.forced if bridge else (generation.forced or job.forced if generation else False))
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
            if job.pause_requested:
                before=time.monotonic()
                if result.get("job_state") != "paused" or before-last_save >= .5:
                    report("paused")
                time.sleep(.05)
                paused_seconds += time.monotonic()-before
                continue
            elapsed=now-started-paused_seconds
            result["simulation_time_ms"]=min(data["duration_ms"],int(elapsed*1000)) if data["realtime"] else result.get("simulation_time_ms",0)
            if now-last_save >= .5:
                report()
            if due is None or not data["realtime"] or elapsed*1000 >= due:
                return True
            time.sleep(min(.05,max(0,(due-elapsed*1000)/1000)))

    def messages(vehicle):
        for trip in data["trips_by_vehicle"].get(vehicle,[]):
            offset=0
            while gate():
                output=data["generate"](vehicle,trip["trip_key"],offset,session=generation) if generation else data["generate"](vehicle,trip["trip_key"],offset)
                if output["status"]=="preparing":
                    continue
                for message in output["messages"]:
                    yield message, False
                if not output["has_more"]:
                    if output.get("trip"):
                        yield output["trip"], output["synthetic_trip"]
                    break
                offset += len(output["messages"])

    def consume(timestamp,vehicle,message,synthetic):
        due=timestamp-data["measurement_start_ms"]
        sequences[vehicle]=sequences.get(vehicle,0)+1
        result["processed_messages"]+=1
        result["payload_bytes"]+=len(json.dumps(message,separators=(",",":"),ensure_ascii=False).encode())
        result["trip_count" if message["ty"]==3 else "microtrip_count"]+=1
        result["simulation_time_ms"]=int(due)
        if data["realtime"]:
            result["schedule_delay_ms"]=max(result["schedule_delay_ms"],int((time.monotonic()-started-paused_seconds)*1000-due))
        if len(result["samples"])<10:
            result["samples"].append(dict(vehicle_id=vehicle,client_id=data["client_by_vehicle"][vehicle],topic=f"{data["mqtt"]["topic_prefix"]}/{job.id}/{vehicle}/obd" if mqtt else "",sequence=sequences[vehicle],message=message,synthetic_trip=synthetic))
        if mqtt:
            if synthetic:raise MQTTFailure("시험용 Trip은 외부 전송할 수 없습니다.")
            data["validate_message"](message)
            result["transmission"].update(in_flight=1,peak_in_flight=1)
            report()
            publish_started=time.monotonic()
            bridge.command("publish",vehicle_id=vehicle,message=message)
            sync_mqtt()
            result["transmission"]["in_flight"]=0
            if time.monotonic()-publish_started > max(data["config"]["interval_ms"]/1000,1/data["mqtt"]["publish_per_second"]):
                result["transmission"]["backpressure_count"]+=1
            report()

    try:
        report()
        if mqtt:
            bridge=MQTTBridge(worker,data,job)
            bridge.receive()
            if data["config"]["connection_lifetime"]=="always":
                for client in sorted(set(data["client_by_vehicle"].values())):
                    if not gate():break
                    bridge.command("connect",client_id=client)
                    sync_mqtt();report()
        else:
            generation=GenerationPool(worker,job,manager,data.get("generation_workers",1))
        if not mqtt:
            bounded=BoundedSchedule(data,generation,gate,result)
            for timestamp,vehicle,message,synthetic in bounded:consume(timestamp,vehicle,message,synthetic)
        else:
            pending=[]
            driving={}
            if mqtt and data["config"]["connection_lifetime"]=="while_driving":
                boundaries={}
                for vehicle,trips in data["trips_by_vehicle"].items():
                    client=data["client_by_vehicle"][vehicle]
                    for trip in trips:
                        for timestamp,priority in [(trip["observed_start_ms"],0),(trip["observed_end_ms"],2)]:
                            key=(timestamp,priority,client)
                            boundaries[key]=boundaries.get(key,0)+1
                for (timestamp,priority,client),count in boundaries.items():
                    heapq.heappush(pending,(timestamp,priority,client,count,False))
            streams={}
            sequences={}
            # 차량별 순서를 유지하고 차량 사이는 시각순 heap으로 처리한다.
            for assignment in data["assignments"]:
                if not gate():
                    break
                vehicle=assignment["vehicle_id"]
                stream=messages(vehicle)
                streams[vehicle]=stream
                message=next(stream,None)
                if message is not None:
                    heapq.heappush(pending,(message[0]["ts"],1,vehicle,message[0],message[1]))
            while pending and not job.stop_requested:
                timestamp,priority,vehicle,message,synthetic=heapq.heappop(pending)
                due=timestamp-data["measurement_start_ms"]
                if not gate(due):
                    break
                if priority==3:
                    next_message=next(streams[vehicle],None)
                    if next_message is not None:
                        heapq.heappush(pending,(next_message[0]["ts"],1,vehicle,next_message[0],next_message[1]))
                    continue
                if priority!=1:
                    client=vehicle
                    before=driving.get(client,0)
                    driving[client]=before+(message if priority==0 else -message)
                    if priority==0 and before==0:
                        bridge.command("connect",client_id=client)
                    elif priority==2 and driving[client]==0:
                        bridge.command("disconnect",client_id=client)
                    sync_mqtt();report()
                    continue
                consume(timestamp,vehicle,message,synthetic)
                heapq.heappush(pending,(timestamp,3,vehicle,None,False))
        # 마지막 운행 뒤에도 측정 종료까지 주차 시간을 진행한다.
        if not job.stop_requested:
            gate(data["duration_ms"])
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
