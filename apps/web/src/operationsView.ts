/** 상시 운영 화면의 입력 확인·표시 문구. 서버 검사(operation_plan.OperationSettings, RegisterVehicles)와 같은 범위를 쓴다. */
export type OperationSettings={transport:"dry_run"|"mqtt";interval_ms:number;client_count:number;duration_cap_days:number;
  daily_variation_percent:number;departure_jitter_minutes:number;day_off_probability:number;weekend_activity_probability:number;
  anomaly_events_per_hour:number;anomaly_intensity:"low"|"medium"|"high";generation_workers:number};
export type RegisterInput={count:number;pattern_mix:{commute:number;daytime:number;flexible:number};
  road_mix:{urban:number;highway:number;congested:number;mixed:number};anomaly_vehicle_percent:number;trip_min_minutes:number;trip_max_minutes:number};
export type StatusCount={label:string;count:number};
export type RegistrySummary={active:number;max_active:number;status:Record<string,StatusCount>;
  patterns:Record<string,{count:number;percent:number}>;anomaly_eligible:number;serial_remaining:number;
  pseudonym_provider:string;provider_allows_real_transport:boolean;target:RegisterInput|null};
export type OperationStatus={state:"stopped"|"running"|"stopping";running:boolean;settings:OperationSettings|null;last_stop:null|"graceful"|"abnormal";
  stop_reason:string|null;error:string|null;virtual_checkpoint_ms:number|null;session_started_at:string|null;
  live:{state?:string;stopping?:boolean;stop_reason?:string|null;recovering?:boolean;vehicles?:number;virtual_anchor_ms?:number;virtual_now_ms?:number;
    mqtt?:Record<string,number|null>;result?:{processed_messages:number;confirmed_messages:number;trip_count:number;microtrip_count:number;
      recovered_trips:number;payload_bytes:number;simulation_time_ms:number;samples:{vehicle_alias:string;client_id:string;topic:string;message:string}[]}};
  registry:RegistrySummary};

export const defaultOperationSettings:OperationSettings={transport:"dry_run",interval_ms:2000,client_count:500,duration_cap_days:7,
  daily_variation_percent:25,departure_jitter_minutes:30,day_off_probability:.2,weekend_activity_probability:.5,
  anomaly_events_per_hour:2,anomaly_intensity:"medium",generation_workers:1};
export const defaultRegister:RegisterInput={count:100,pattern_mix:{commute:60,daytime:20,flexible:20},
  road_mix:{urban:50,highway:20,congested:10,mixed:20},anomaly_vehicle_percent:5,trip_min_minutes:3,trip_max_minutes:120};

export const patternLabels:Record<string,string>={commute:"출퇴근형",daytime:"주간 운행형",flexible:"비정기형"};
export const eventLabels:Record<string,string>={start:"운영 시작",stopping:"정상 중단 시작",graceful_stop:"정상 중단 완료",abnormal_stop:"비정상 종료",
  recover_trip:"운행 복구(중복 가능 구간)",removed:"차량 제외 완료",cleanup_loss:"손실 기록 후 제외"};

const within=(value:number,min:number,max:number,integer=true)=>Number.isFinite(value)&&value>=min&&value<=max&&(!integer||Number.isInteger(value));

export function settingIssues(s:OperationSettings):string[] {
  const issues:string[]=[];
  if(!within(s.interval_ms,1000,60000))issues.push("Microtrip 주기는 1~60초(1초 단위)입니다.");
  if(!within(s.client_count,1,25000))issues.push("Client 수는 1~25,000개입니다.");
  if(!within(s.duration_cap_days,1,30))issues.push("기간 상한은 1~30일입니다.");
  if(!within(s.daily_variation_percent,0,50))issues.push("날짜별 편차는 0~50%입니다.");
  if(!within(s.departure_jitter_minutes,0,120))issues.push("출발 편차는 0~120분입니다.");
  if(!within(s.day_off_probability,0,1,false)||!within(s.weekend_activity_probability,0,1,false))issues.push("미운행·주말 참여 확률은 0~100%입니다.");
  if(!within(s.anomaly_events_per_hour,0,60,false))issues.push("이상 이벤트 빈도는 0~60회입니다.");
  if(![1,2,4].includes(s.generation_workers))issues.push("생성 Worker는 1·2·4개입니다.");
  return issues;
}

export function registerIssues(r:RegisterInput,remaining:number):string[] {
  const issues:string[]=[];
  const sum=(o:Record<string,number>)=>Object.values(o).reduce((a,b)=>a+b,0);
  if(!within(r.count,1,25000))issues.push("등록 대수는 1~25,000대입니다.");
  else if(r.count>remaining)issues.push(`현재 ${remaining.toLocaleString()}대까지 더 등록할 수 있습니다.`);
  if(sum(r.pattern_mix)!==100)issues.push("생활패턴 비율의 합은 100%여야 합니다.");
  if(sum(r.road_mix)!==100)issues.push("도로 배정 비중의 합은 100%여야 합니다.");
  if(!within(r.anomaly_vehicle_percent,0,100,false))issues.push("이상 이벤트 대상 비율은 0~100%입니다.");
  if(!within(r.trip_min_minutes,2,240)||!within(r.trip_max_minutes,2,240)||r.trip_min_minutes>r.trip_max_minutes)issues.push("운행 시간은 2~240분이며 최소가 최대 이하여야 합니다.");
  return issues;
}

export function operationState(s:OperationStatus|null):string {
  if(!s)return "확인 중";
  if(s.running)return s.live.stopping||s.state==="stopping"?"정상 중단 중 · 진행 운행 마무리":"실행 중";
  return "중지됨";
}

/** 마지막 중단의 의미와 다음 시작 때 일어날 일을 설명한다. */
export function stopExplanation(s:OperationStatus|null):string {
  if(!s||s.running||!s.last_stop)return "";
  if(s.last_stop==="graceful")return `${s.stop_reason||"정상 중단"} · 진행 중 운행을 모두 보냈습니다. 다시 시작하면 현재 시각 기준으로 다음 운행부터 이어집니다(멈춘 동안의 운행은 건너뜀).`;
  return `${s.stop_reason||"비정상 종료"}${s.error?` · ${s.error}`:""} · 다시 시작하면 브로커 확인이 끝난 메시지 다음부터 같은 운행을 이어 보냅니다. 확인 전 메시지는 같은 내용으로 중복될 수 있습니다.`;
}

export function formatKST(ms:number|null|undefined):string {
  if(ms===null||ms===undefined||!Number.isFinite(ms))return "—";
  return new Date(ms).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",hour12:false});
}
