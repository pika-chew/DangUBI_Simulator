"""이번 실행의 전체 발행 지연 목표. 추정 백분위 대신 임계값별 건수로 판정한다."""


def assess(result):
    output = {"status": "unavailable", "p95_limit_ms": 1000, "p99_limit_ms": 2000,
              "scope": "whole_run", "reason": "이 기록에는 목표 판정에 필요한 계측값이 없습니다."}

    def state(status, reason):
        return {**output, "status": status, "reason": reason}

    if result.get("transport") != "mqtt":
        return state("not_applicable", "송신 없는 실행은 실제 발행 지연을 판정하지 않습니다.")
    if result.get("transmission_pacing") == "fast":
        return state("not_applicable", "빠른 전송은 일정 대기를 생략하므로 1배속 발행 지연 목표를 적용하지 않습니다.")
    status = result.get("job_state", result.get("state"))
    if status in {"starting", "running", "pausing", "paused", "resuming", "stopping"}:
        return state("pending", "실행이 끝난 뒤 전체 전송 결과와 지연을 함께 판정합니다.")
    if status not in {"completed", "mqtt_completed"}:
        return state("incomplete", "실행이 정상 완료되지 않아 지연 목표 통과로 판정하지 않습니다.")

    planned = result.get("planned_messages")
    if type(planned) is not int or planned <= 0:
        return output
    if (any(type(result.get(key)) is not int or result.get(key) != planned for key in ("processed_messages", "publish_attempts", "mqtt_published", "puback_count"))
            or any(result.get(key, 0) != 0 for key in ("remaining_messages", "unsent_messages", "unconfirmed_messages", "active_clients"))
            or result.get("errors") or result.get("forced_termination")
            or result.get("connection_cleanup_confirmed") is False
            or result.get("execution_cleanup_confirmed") is False):
        return state("incomplete", "미전송·미확인·처리 건수 또는 종료 정리 상태를 확인해야 합니다.")

    performance = result.get("performance") or {}
    schedule = performance.get("schedule") or {}
    keys = ("count", "within_1s_count", "within_2s_count", "over_5s_count")
    values = [schedule.get(key) for key in keys]
    if any(type(value) is not int or value < 0 for value in values):
        return output
    count, one, two, over = values
    if count != planned or not 0 <= one <= two <= count or over > count-two:
        return state("unavailable", "발행 건수와 지연 계측 건수가 일치하지 않습니다.")
    p95 = one*100 >= count*95
    p99 = two*100 >= count*99
    output.update(sample_count=count, within_1s_count=one, within_2s_count=two, over_5s_count=over,
                  p95_threshold_met=p95, p99_threshold_met=p99)
    return state("passed" if p95 and p99 else "exceeded",
                 "이번 실행의 전체 발행 지연 목표를 충족했습니다." if p95 and p99 else "이번 실행의 전체 발행 지연 목표를 초과했습니다.")
