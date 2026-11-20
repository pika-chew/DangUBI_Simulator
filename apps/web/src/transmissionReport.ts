import type {Result} from "./SimulationTools";

const measured=(value:number|undefined)=>typeof value==="number"&&Number.isFinite(value)&&value>=0?value:null;

/** 기존 집계만 사용한다. 원문 메시지·접속 정보·추가 요청은 포함하지 않는다. */
export function transmissionReport(result:Result,exportedAt=new Date().toISOString()) {
  const mqtt=result.transport==="mqtt" || result.state.startsWith("mqtt_") || (result.performance?.ack.count ?? 0)>0;
  const elapsed=measured(result.elapsed_ms);
  const ended=result.job_state?result.job_state==="completed":result.state==="mqtt_completed";
  const complete=mqtt&&ended
    &&result.puback_count===result.planned_messages&&result.unconfirmed_messages===0
    &&!result.errors?.length&&result.connection_cleanup_confirmed===true&&result.execution_cleanup_confirmed===true;
  return {
    report_version:"transmission-report-1",exported_at:exportedAt,run_id:result.run_id,
    transport:mqtt?"mqtt":"dry_run",state:result.state,job_state:result.job_state ?? null,
    completed_and_confirmed:complete,model_version:result.model_version ?? null,
    vehicles:result.assignment_count,allocated_clients:result.allocated_clients,
    counts:{planned:result.planned_messages,processed:result.processed_messages,
      publish_attempts:result.publish_attempts ?? null,puback:result.puback_count,
      unsent:result.unsent_messages ?? null,unconfirmed:result.unconfirmed_messages ?? null},
    timing:{elapsed_ms:elapsed,paused_ms:measured(result.paused_ms),active_elapsed_ms:measured(result.active_elapsed_ms),
      initial_connect_ms:measured(result.initial_connect_ms),phases_ms:result.execution_timing ?? null},
    average_puback_per_second:mqtt&&elapsed!==null&&elapsed>0?result.puback_count/(elapsed/1000):null,
    average_scope:"PUBACK 건수 / 실행 전체 경과 초. 준비·접속·예정 시각 대기·일시정지·정리 포함. 순간/최대 처리량 아님",
    latency:result.performance?{generation:result.performance.generation,schedule:result.performance.schedule,
      connect:result.performance.connect,ack:result.performance.ack}:null,
    transmission_goal:result.transmission_goal ?? null,
    connection_cleanup_confirmed:result.connection_cleanup_confirmed ?? null,
    execution_cleanup_confirmed:result.execution_cleanup_confirmed ?? null,
    errors:result.errors ?? [],
  };
}
