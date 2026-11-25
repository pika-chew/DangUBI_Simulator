import {InfoHint} from "./InfoHint";
import {defaultFleet,profileGuide,trialMaxDays} from "./fleetSettings";
export {defaultFleet} from "./fleetSettings";
type Mix = { commute: number; daytime: number; flexible: number };
export type FleetConfig = {
  include_em?: boolean;
  execution_profile?: "small_test" | "operations";
  fleet_version: "fleet-plan-1" | "fleet-plan-2" | "fleet-plan-3" | "fleet-plan-4";
  vehicle_count: number;
  measurement: { start: string; end: string; timezone: "Asia/Seoul" };
  interval_ms: number;
  seed: number;
  daily_driving_minutes?: number | null;
  trip_min_minutes?: number | null;
  trip_max_minutes?: number | null;
  vehicle_variation_percent?: number | null;
  daily_variation_percent: number;
  departure_jitter_minutes: number;
  day_off_probability: number;
  weekend_activity_probability: number;
  anomaly_vehicle_percent: number;
  anomaly_events_per_hour: number;
  anomaly_intensity: "low" | "medium" | "high";
  pattern_mix: Mix;
  road_mix: { urban: number; highway: number; congested: number; mixed: number };
};

const localTime = (ms: number) => new Date(ms + 9 * 3600000).toISOString().slice(0, 19);
const local = (s: string) => Number.isFinite(Date.parse(s)) ? localTime(Date.parse(s)) : "";
const iso = (s: string) => s ? `${s}${s.length === 16 ? ":00" : ""}+09:00` : "";
const presets: Record<string, Mix> = {
  mixed: { commute: 60, daytime: 20, flexible: 20 },
  commute: { commute: 80, daytime: 10, flexible: 10 },
  flexible: { commute: 20, daytime: 30, flexible: 50 },
};
const Help=InfoHint;
const units:Partial<Record<keyof FleetConfig,string>>={vehicle_count:"대",trip_min_minutes:"분/회",trip_max_minutes:"분/회",daily_driving_minutes:"분/일",vehicle_variation_percent:"%",daily_variation_percent:"%",departure_jitter_minutes:"분",anomaly_vehicle_percent:"%",anomaly_events_per_hour:"회/운행 1시간"};

export function FleetControls({ value: v, onChange, disabled }: { value: FleetConfig; onChange: (next: FleetConfig) => void; disabled: boolean }) {
  const profile=v.execution_profile??"small_test";
  const guide=profileGuide[profile];
  const patch = (p: Partial<FleetConfig>) => onChange({ ...v, ...p });
  const numeric = (key: keyof FleetConfig, title: string, min: number, max: number, hint?: string, step = 1) => <label key={key}>
    <span className="label-line">{title} <Help label={title} text={hint??"값을 높이면 적용량이 늘어납니다."} range={min.toLocaleString("ko-KR")+"~"+max.toLocaleString("ko-KR")+(units[key]??" · 정수")} /></span>
    <input aria-label={title} type="number" min={min} max={max} step={step} value={(v[key] ?? "") as number | string} onChange={e => patch({ [key]: Number(e.target.value) })} />
  </label>;
  const preset = Object.entries(presets).find(([, m]) => JSON.stringify(m) === JSON.stringify(v.pattern_mix))?.[0] ?? "custom";
  return <fieldset disabled={disabled} className="schedule-fieldset fleet-settings">
    {v.fleet_version!=="fleet-plan-4" && <p className="context-note">이전 시간 배정의 기록입니다. ‘설정 복사’를 하면 현재 최소·최대 시간 배정 방식으로 새 일정을 작성합니다. 원본은 보존합니다.</p>}
    <div className="fields">
      {profile==="operations"
        ?<div className="context-note legacy-profile"><b>이전 운영 프로필 기록입니다.</b> 운영 프로필은 상시 운영 탭으로 이전되었습니다. 시험 실행은 최대 1,000대·{trialMaxDays}일을 지원합니다.<button type="button" onClick={()=>onChange(defaultFleet("small_test",Date.parse(v.measurement.start)))}>시험 조건으로 바꾸기</button></div>
        :<span className="profile-limits"><b>시험 실행 상한 <Help label="시험 실행 상한" text={guide.info} range={guide.range}/></b><span>최대 1,000대 · {guide.maxDays}일</span><span>실제 MQTT · 서버 상한 적용</span><span>대규모·장기 운영은 상시 운영 탭에서 합니다.</span></span>}
      {numeric("vehicle_count", "차량 수", 1, v.execution_profile==="operations"?25000:1000, "데이터를 만들 차량 수입니다. 늘리면 생성량과 처리 부하가 커집니다.")}
      <label><span className="label-line">측정 시작 (KST) <Help label="측정 시작" text="데이터를 관측할 시작 시각입니다. 차량의 실제 출발 시각과는 다릅니다." range={`시작 < 종료 · 최대 ${guide.maxDays}일 · 1970~2100년`} /></span><input type="datetime-local" step="1" value={local(v.measurement.start)} onChange={e => patch({ measurement: { ...v.measurement, start: iso(e.target.value) } })} /></label>
      <label><span className="label-line">측정 종료 (KST) <Help label="측정 종료" text="데이터를 관측할 마지막 시각입니다. 늦출수록 관측량이 늘어납니다." range={`측정 기간 최대 ${guide.maxDays}일`} /></span><input type="datetime-local" step="1" min={local(v.measurement.start)} max={Number.isFinite(Date.parse(v.measurement.start))?localTime(Date.parse(v.measurement.start)+guide.maxDays*86400000):undefined} value={local(v.measurement.end)} onChange={e => patch({ measurement: { ...v.measurement, end: iso(e.target.value) } })} /></label>
      <label><span className="label-line">Microtrip 주기 (초) <Help label="Microtrip 주기" text="운행 중 메시지를 만드는 간격입니다. 짧을수록 데이터와 전송량이 늘어납니다." range="0.1~86,400초" /></span><input type="number" min="0.1" max="86400" step="0.1" value={v.interval_ms / 1000} onChange={e => patch({ interval_ms: Math.round(Number(e.target.value) * 1000) })} /></label>
      <label><span className="label-line">운행 성격 <Help label="운행 성격" text="출퇴근·주간·비정기 차량의 구성을 바꿉니다. 차량별 시간표는 자동 배정합니다." /></span><select value={preset} onChange={e => patch({ pattern_mix: { ...presets[e.target.value] } })}>
        <option value="mixed">혼합 · 출퇴근60% / 주간20% / 비정기20%</option>
        <option value="commute">출퇴근 중심 · 80% / 10% / 10%</option>
        <option value="flexible">비정기 중심 · 20% / 30% / 50%</option>
        {preset === "custom" && <option value="custom" disabled>고급 설정의 사용자 비율</option>}
      </select></label>
      {(v.fleet_version==="fleet-plan-3" || v.fleet_version==="fleet-plan-4") ? <>
        {numeric("trip_min_minutes", "최소 운행 시간 (분/회)", 2, 240, "한 번의 운행이 지속될 최소 시간입니다. 최대 운행 시간 이하로 정합니다.")}
        {numeric("trip_max_minutes", "최대 운행 시간 (분/회)", 2, 240, "한 번의 운행이 지속될 최대 시간입니다. 최소 운행 시간 이상으로 정합니다.")}
      </> : numeric("daily_driving_minutes", "이전 운행일 하루 목표 시간 (분)", 10, 480, "이전 방식의 하루 운행 목표입니다. 새 일정은 최소·최대 시간을 사용합니다.")}

      <label><span className="label-line">하루 미운행 확률 (%) <Help label="하루 미운행 확률" text="차량이 하루를 쉬는 확률입니다. 높이면 운행 없는 날이 늘어납니다." range="0~100%" /></span><input aria-label="하루 미운행 확률 (%)" type="number" min="0" max="100" step="1" value={v.day_off_probability * 100} onChange={e => patch({ day_off_probability: Number(e.target.value) / 100 })} /></label>
      {numeric("anomaly_vehicle_percent", "이상 이벤트 대상 차량 (%)", 0, 100, "급기동 발생이 가능한 차량 비율입니다. 높여도 실제 발생은 보장하지 않습니다. 과속은 별도 판정입니다.", 0.1)}
      {numeric("anomaly_events_per_hour", "이상 이벤트 후보 빈도 (회/운행 1시간)", 0, 60, "급기동을 시도할 기회 수입니다. 높일수록 자주 시도하며 실제 횟수는 달라집니다. 과속은 별도 판정입니다.", 0.1)}
      <label><span className="label-line">이상 이벤트 강도 <Help label="이상 이벤트 강도" text="급기동의 가감속·회전을 강하게 만듭니다. 빈도를 바꾸는 설정은 아닙니다." range="낮음 / 보통 / 높음" /></span><select value={v.anomaly_intensity} onChange={e => patch({ anomaly_intensity: e.target.value as FleetConfig["anomaly_intensity"] })}><option value="low">낮음</option><option value="medium">보통</option><option value="high">높음</option></select></label>
    </div>
    <details className="schedule-advanced"><summary>고급 전체 조건 · 패턴·주말·도로·재현</summary>
      <div className="fields">
        {v.fleet_version!=="fleet-plan-4" && numeric("vehicle_variation_percent", "차량 간 운행 시간 편차 (±%)", 0, 50, "이전 방식의 차량 간 운행 시간 차이입니다. 높이면 차량마다 차이가 커집니다.")}
      {numeric("daily_variation_percent", "같은 차량의 날짜별 편차 (±%)", 0, 50, "같은 차량의 날짜별 운행 시간 차이입니다. 높이면 차이가 커지며 최소·최대 시간은 유지합니다.")}
        {numeric("departure_jitter_minutes", "날짜별 출발 편차 (±분)", 0, 120, "날짜마다 첫 출발을 앞뒤로 바꿉니다. 높이면 출발 시각이 더 넓게 퍼집니다.")}
        <label><span className="label-line">주말 운행 참여 확률 (%) <Help label="주말 운행 참여 확률" text="주말을 운행 후보일로 정할 확률입니다. 이후 하루 미운행 확률도 적용합니다." range="0~100%" /></span><input aria-label="주말 운행 참여 확률 (%)" type="number" min="0" max="100" value={v.weekend_activity_probability * 100} onChange={e => patch({ weekend_activity_probability: Number(e.target.value) / 100 })} /></label>
        {numeric("seed", "재현 seed", -2147483648, 2147483647, "무작위 배정의 기준값입니다. 같은 버전·조건·seed는 같은 결과를 만듭니다.")}
        {(Object.entries({ commute: "출퇴근형", daytime: "주간 운행형", flexible: "비정기형" }) as [keyof Mix, string][]).map(([key, title]) => <label key={key}><span className="label-line">{title} 비율 (%) <Help label={title+" 비율"} text="이 생활패턴에 배정할 차량 비율입니다. 높이면 해당 유형의 차량이 늘어납니다." range="각0~100% · 세 유형 합계100%" /></span><input type="number" min="0" max="100" value={v.pattern_mix[key]} onChange={e => patch({ pattern_mix: { ...v.pattern_mix, [key]: Number(e.target.value) } })} /></label>)}
        {(Object.entries({ urban: "도심", highway: "고속도로", congested: "정체", mixed: "혼합" }) as [keyof FleetConfig["road_mix"], string][]).map(([key, title]) => <label key={key}><span className="label-line">{title} 도로 배정 비중 (%) <Help label={title+" 도로 비중"} text="차량의 도로 성격을 고르는 가중치입니다. 실제 도로별 주행거리 비율은 아닙니다." range="각0~100% · 네 도로 합계100%" /></span><input type="number" min="0" max="100" value={v.road_mix[key]} onChange={e => patch({ road_mix: { ...v.road_mix, [key]: Number(e.target.value) } })} /></label>)}
      </div>
      <p className="note">생활패턴 비율과 도로 배정 비중은 각각 합계100%로 지정합니다. 생활패턴은 정수 차량 수로 배정하고 도로 비중은 차량별 도로 성격을 추첨하는 가중치입니다. 다양한 생활 운행에서는 짧은 용무·운행 길이에 맞게 개별 도로 성격도 달라질 수 있습니다. 실제 경로·목적지·전국 교통을 수입한 모델은 아닙니다.</p>
    </details>
    <p className="note">운행 시간은 1회 기준 · 최소 주차30분.</p>
  </fieldset>;
}
