/** 일정 실행의 Worker 설정과 단일 운행 생성 설정을 구분한다. */
export function runView(request: {source_plan_key?: string; schedule_plan?: unknown; generation?: unknown}): "schedule" | "single_journey" | "samples" {
  if(request.source_plan_key || request.schedule_plan)return "schedule";
  const g=request.generation;
  return g!==null&&typeof g==="object"&&"duration_seconds" in g&&typeof g.duration_seconds==="number"&&Number.isFinite(g.duration_seconds)&&g.duration_seconds>0 ? "single_journey" : "samples";
}
