"""일정 실행: 선택 조회와 동일한 생성기를 사용하고 제한된 페이지와 샘플만 유지한다."""
import heapq
import json
import time


def execute(manager, job, data, worker, timeout):
    started = time.monotonic()
    paused_seconds = 0.0
    result = job.result.copy()
    result.update(assignments=data["assignments"], model_version="schedule-obd-2/traffic-7",
                  microtrip_count=0, trip_count=0, schedule_delay_ms=0)
    last_save = 0.0

    def report(state="running", final=False):
        nonlocal last_save
        elapsed = time.monotonic()-started
        result.update(elapsed_ms=int(elapsed*1000), paused_ms=int(paused_seconds*1000),
                      active_elapsed_ms=int((elapsed-paused_seconds)*1000),
                      remaining_messages=max(0,result["planned_messages"]-result["processed_messages"]),
                      job_state=state, state="dry_run_completed" if final and state=="completed" else state,
                      connection_cleanup_confirmed=final, forced_termination=False)
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
                output=data["generate"](vehicle,trip["trip_key"],offset)
                if output["status"]=="preparing":
                    continue
                for message in output["messages"]:
                    yield message, False
                if not output["has_more"]:
                    if output.get("trip"):
                        yield output["trip"], output["synthetic_trip"]
                    break
                offset += len(output["messages"])

    try:
        report()
        pending=[]
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
                heapq.heappush(pending,(message[0]["ts"],vehicle,message[0],message[1]))
        while pending and not job.stop_requested:
            timestamp,vehicle,message,synthetic=heapq.heappop(pending)
            due=timestamp-data["measurement_start_ms"]
            if not gate(due):
                break
            sequences[vehicle]=sequences.get(vehicle,0)+1
            result["processed_messages"]+=1
            result["payload_bytes"]+=len(json.dumps(message,separators=(",",":"),ensure_ascii=False).encode())
            result["trip_count" if message["ty"]==3 else "microtrip_count"]+=1
            result["simulation_time_ms"]=int(due)
            if data["realtime"]:
                result["schedule_delay_ms"]=max(result["schedule_delay_ms"],int((time.monotonic()-started-paused_seconds)*1000-due))
            if len(result["samples"])<10:
                result["samples"].append(dict(vehicle_id=vehicle,client_id=data["client_by_vehicle"][vehicle],topic="",sequence=sequences[vehicle],message=message,synthetic_trip=synthetic))
            message=next(streams[vehicle],None)
            if message is not None:
                heapq.heappush(pending,(message[0]["ts"],vehicle,message[0],message[1]))
        # 마지막 운행 뒤에도 측정 종료까지 주차 시간을 진행한다.
        if not job.stop_requested:
            gate(data["duration_ms"])
        if not job.stop_requested:
            result["simulation_time_ms"]=data["duration_ms"]
        if job.timed_out:
            result["errors"]=["실행 제한시간을 초과했습니다."]
        report("failed" if job.timed_out else "stopped" if job.stop_requested else "completed",True)
    except Exception:
        result["errors"]=["일정 데이터 생성에 실패했습니다. 일부 결과만 보관하며 자동 재실행하지 않습니다."]
        report("failed",True)
    finally:
        with manager.lock:
            if manager.active is job:
                manager.active=None
            job.finished.set()
