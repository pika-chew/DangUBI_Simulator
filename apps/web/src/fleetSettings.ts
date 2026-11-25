import type {FleetConfig} from "./FleetControls";

export type ExecutionProfile="small_test"|"operations";
export const trialMaxDays=7;
export const profileGuide={
  small_test:{name:"시험 실행",maxVehicles:1000,maxDays:trialMaxDays,defaultClients:1,
    summary:`생성·전송 확인 · 최대 1,000대 / ${trialMaxDays}일`,
    info:"차량별 출발 시각과 운행 횟수, 쉬는 날을 자동 배정합니다.",
    range:`최대 1,000대·${trialMaxDays}일. 실제 MQTT는 서버 한도를 적용합니다.`},
  // 이전 운영 프로필 기록 조회·복사 변환용. 새 일정은 상시 운영 탭을 사용한다.
  operations:{name:"운영(이전)",maxVehicles:25000,maxDays:30,defaultClients:500,
    summary:"일정 생성·전송 · 최대25,000대 / 30일",
    info:"일정 생성·전송용입니다. 전환 시 기본값을 적용합니다.",
    range:"25,000대·30일 이하. 생성 건수 최대1조 건. 실제 MQTT는 서버의 Client·메시지·시간 상한도 적용"},
};

/** 화면 초기값만 결정한다. 일정/데이터 생성 규칙은 변경하지 않는다. */
export function defaultFleet(profile:ExecutionProfile="small_test",now=Date.now()):FleetConfig {
  const day=new Date((Number.isFinite(now)?now:Date.now())+9*3600000).toISOString().slice(0,10);
  const start=`${day}T00:00:00+09:00`;
  const end=new Date(Date.parse(start)+(profile==="operations"?3:1)*86400000);
  const endKST=new Date(end.getTime()+9*3600000).toISOString().slice(0,19)+"+09:00";
  return {execution_profile:profile,fleet_version:"fleet-plan-4",vehicle_count:profile==="operations"?20:3,
    measurement:{start,end:endKST,timezone:"Asia/Seoul"},interval_ms:2000,seed:42,
    trip_min_minutes:3,trip_max_minutes:120,
    daily_variation_percent:25,departure_jitter_minutes:30,
    day_off_probability:0.2,weekend_activity_probability:0.5,
    anomaly_vehicle_percent:5,anomaly_events_per_hour:2,anomaly_intensity:"medium",
    pattern_mix:{commute:60,daytime:20,flexible:20},road_mix:{urban:35,highway:20,congested:5,mixed:40}};
}

export function profileExecutionDefaults(_profile:ExecutionProfile) {
  return {realtime:true,mapping:"shared",clients:0,workers:1,lifetime:"always",transport:"dry_run" as const,force:false};
}

/** 복사한 초안은 현재 시간 배정으로 만들고 원본 기록은 수정하지 않는다. */
export function currentFleet(value:FleetConfig):FleetConfig {
  return {...value,fleet_version:"fleet-plan-4",daily_driving_minutes:undefined,vehicle_variation_percent:undefined,
          trip_min_minutes:value.trip_min_minutes??3,trip_max_minutes:value.trip_max_minutes??120};
}
