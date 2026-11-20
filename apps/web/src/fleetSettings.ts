import type {FleetConfig} from "./FleetControls";

export type ExecutionProfile="small_test"|"operations";
export const profileGuide={
  small_test:{name:"소량 시험",maxVehicles:100,maxDays:1/24,defaultClients:1,
    summary:"생성 확인 · 최대100대 / 1시간 / 10,000건",
    info:"생성 확인용입니다. 전환 시 기본값을 적용합니다.",
    range:"전체 실행: 100대·1시간·10,000건 이하. 실제 MQTT: 20Client·100건 이하·측정30초 미만. 서버 상한이 더 작으면 그 값을 적용"},
  operations:{name:"운영",maxVehicles:25000,maxDays:30,defaultClients:500,
    summary:"일정 생성·전송 · 최대25,000대 / 30일",
    info:"일정 생성·전송용입니다. 전환 시 기본값을 적용합니다.",
    range:"25,000대·30일 이하. 생성 건수 최대1조 건. 실제 MQTT는 서버의 Client·메시지·시간 상한도 적용"},
};

/** 화면 초기값만 결정한다. 일정/데이터 생성 규칙은 변경하지 않는다. */
export function defaultFleet(profile:ExecutionProfile="small_test",now=Date.now()):FleetConfig {
  const day=new Date((Number.isFinite(now)?now:Date.now())+9*3600000).toISOString().slice(0,10);
  const start=`${day}T${profile==="operations"?"00:00:00":"07:30:00"}+09:00`;
  const end=new Date(Date.parse(start)+(profile==="operations"?3*86400000:3600000));
  const endKST=new Date(end.getTime()+9*3600000).toISOString().slice(0,19)+"+09:00";
  return {execution_profile:profile,fleet_version:"fleet-plan-4",vehicle_count:profile==="operations"?20:3,
    measurement:{start,end:endKST,timezone:"Asia/Seoul"},interval_ms:2000,seed:42,
    trip_min_minutes:3,trip_max_minutes:profile==="operations"?120:15,
    daily_variation_percent:25,departure_jitter_minutes:30,
    day_off_probability:profile==="operations"?0.2:0,weekend_activity_probability:profile==="operations"?0.5:1,
    anomaly_vehicle_percent:5,anomaly_events_per_hour:2,anomaly_intensity:"medium",
    pattern_mix:{commute:60,daytime:20,flexible:20},road_mix:{urban:35,highway:20,congested:5,mixed:40}};
}

export function profileExecutionDefaults(profile:ExecutionProfile) {
  return {realtime:profile==="operations",mapping:"shared",clients:0,workers:1,lifetime:"always",transport:"dry_run" as const,force:false};
}

/** 복사한 초안은 현재 시간 배정으로 만들고 원본 기록은 수정하지 않는다. */
export function currentFleet(value:FleetConfig):FleetConfig {
  return {...value,fleet_version:"fleet-plan-4",daily_driving_minutes:undefined,vehicle_variation_percent:undefined,
          trip_min_minutes:value.trip_min_minutes??3,trip_max_minutes:value.trip_max_minutes??120};
}
