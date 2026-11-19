import copy
import pytest
from services.control.transmission_goal import assess


def complete(count=100):
    return dict(transport="mqtt", job_state="completed", planned_messages=count, processed_messages=count,
                publish_attempts=count, mqtt_published=count, puback_count=count, active_clients=0,
                unconfirmed_messages=0, unsent_messages=0, connection_cleanup_confirmed=True,
                performance={"schedule":dict(count=count, within_1s_count=count, within_2s_count=count, over_5s_count=0)})


def test_threshold_equality_uses_counts_not_histogram_upper_bound():
    result=complete()
    result["performance"]["schedule"].update(within_1s_count=95, within_2s_count=99, over_5s_count=1,
                                            p95_upper_ms=1048.576, p99_upper_ms=2097.152)
    assert assess(result)["status"]=="passed"
    result["performance"]["schedule"]["within_1s_count"]=94
    assert assess(result)["status"]=="exceeded"
    result["performance"]["schedule"].update(within_1s_count=95, within_2s_count=98)
    assert assess(result)["status"]=="exceeded"


@pytest.mark.parametrize("change", [
    {"job_state":"stopped"},{"job_state":"failed"},{"job_state":"interrupted"},
    {"unconfirmed_messages":1},{"unsent_messages":1},{"puback_count":99},
    {"publish_attempts":99},{"processed_messages":99},{"mqtt_published":99},
    {"active_clients":1},{"connection_cleanup_confirmed":False},{"execution_cleanup_confirmed":False},
    {"forced_termination":True},{"errors":["확인 필요"]},
])
def test_partial_or_unclean_execution_never_passes(change):
    result=complete()
    result.update(change)
    assert assess(result)["status"]=="incomplete"


def test_missing_old_and_inconsistent_metrics_are_unavailable():
    result=complete()
    del result["performance"]["schedule"]["within_1s_count"]
    assert assess(result)["status"]=="unavailable"
    for field,value in [("count",99),("within_1s_count",True),("within_1s_count",101),("over_5s_count",1)]:
        result=complete()
        result["performance"]["schedule"][field]=value
        assert assess(result)["status"]=="unavailable"
    assert assess(complete(0))["status"]=="unavailable"


def test_live_and_dry_run_are_separate_from_final_assessment():
    result=complete()
    result["job_state"]="running"
    before=copy.deepcopy(result)
    assert assess(result)["status"]=="pending"
    assert result==before
    result["transport"]="dry_run"
    assert assess(result)["status"]=="not_applicable"


def test_small_samples_and_large_counts_use_integer_thresholds():
    result=complete(1)
    result["performance"]["schedule"]["within_1s_count"]=0
    assert assess(result)["status"]=="exceeded"
    count=32_400_000_000
    result=complete(count)
    result["performance"]["schedule"].update(within_1s_count=count*95//100, within_2s_count=count*99//100)
    assert assess(result)["status"]=="passed"
    result["performance"]["schedule"]["within_2s_count"]-=1
    assert assess(result)["status"]=="exceeded"
