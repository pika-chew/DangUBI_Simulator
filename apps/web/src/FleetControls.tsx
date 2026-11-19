type Mix = { commute: number; daytime: number; flexible: number };
export type FleetConfig = {
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
export function defaultFleet(): FleetConfig {
  const day = localTime(Date.now()).slice(0, 10);
  const start = `${day}T00:00:00+09:00`;
  return {
    fleet_version: "fleet-plan-4", vehicle_count: 20,
    measurement: { start, end: iso(localTime(Date.parse(start) + 3 * 86400000)), timezone: "Asia/Seoul" },
    interval_ms: 2000, seed: 42, trip_min_minutes: 3, trip_max_minutes: 120,
    daily_variation_percent: 25, departure_jitter_minutes: 30,
    day_off_probability: 0.2, weekend_activity_probability: 0.5,
    anomaly_vehicle_percent: 5, anomaly_events_per_hour: 2, anomaly_intensity: "medium",
    pattern_mix: { ...presets.mixed }, road_mix: { urban: 35, highway: 20, congested: 5, mixed: 40 },
  };
}
function Help({ text }: { text: string }) {
  return <span className="hint"><button className="info-button" type="button" aria-label={text}>i</button><span className="hint-content" role="tooltip">{text}</span></span>;
}

export function FleetControls({ value: v, onChange, disabled }: { value: FleetConfig; onChange: (next: FleetConfig) => void; disabled: boolean }) {
  const patch = (p: Partial<FleetConfig>) => onChange({ ...v, ...p });
  const numeric = (key: keyof FleetConfig, title: string, min: number, max: number, hint?: string, step = 1) => <label key={key}>
    <span className="label-line">{title} {hint && <Help text={hint} />}</span>
    <input aria-label={title} type="number" min={min} max={max} step={step} value={(v[key] ?? "") as number | string} onChange={e => patch({ [key]: Number(e.target.value) })} />
  </label>;
  const preset = Object.entries(presets).find(([, m]) => JSON.stringify(m) === JSON.stringify(v.pattern_mix))?.[0] ?? "custom";
  return <fieldset disabled={disabled} className="schedule-fieldset">
    <p className="note">차량을 한 대씩 등록할 필요가 없습니다. 전체 조건으로 명부·생활패턴·날짜별 운행과 도로 성격을 자동 배정합니다. 아래 기본값은 한국 실차 통계가 아닌 개발 예시입니다.</p>
    {v.fleet_version!=="fleet-plan-4" && <p className="context-note">이전 시간 배정의 기록입니다. ‘설정 복사’ 후 ‘최소·최대 시간 배정’을 선택하면 하루 목표 없이 새 일정을 만듭니다. 원본은 보존합니다.</p>}
    <div className="fields">
      <label>운행 시간 배정<select value={v.fleet_version} onChange={e=>patch(e.target.value==="fleet-plan-4" ? {fleet_version:"fleet-plan-4",vehicle_variation_percent:undefined,daily_driving_minutes:undefined,trip_min_minutes:v.trip_min_minutes??3,trip_max_minutes:v.trip_max_minutes??120} : {fleet_version:e.target.value as FleetConfig["fleet_version"],daily_driving_minutes:v.daily_driving_minutes??120,trip_min_minutes:undefined,trip_max_minutes:undefined})}><option value="fleet-plan-4">최소·최대 시간 배정 · 기본</option>{v.fleet_version!=="fleet-plan-4" && <option value={v.fleet_version}>이전 배정 · 기록 확인</option>}</select></label>
      <label>실행 프로필<select value={v.execution_profile ?? "small_test"} onChange={e=>patch({execution_profile:e.target.value as "small_test"|"operations"})}><option value="small_test">소량 시험</option><option value="operations">운영 · 최대25,000대·30일</option></select></label>
      {numeric("vehicle_count", "차량 수", 1, v.execution_profile==="operations"?25000:100, "운영은 순차 계획·생성을 사용합니다. 입력 가능 범위이며 실제 AWS 처리량과 장기 안정성 보장이 아닙니다.")}
      <label>측정 시작 (KST)<input type="datetime-local" step="1" value={local(v.measurement.start)} onChange={e => patch({ measurement: { ...v.measurement, start: iso(e.target.value) } })} /></label>
      <label>측정 종료 (KST)<input type="datetime-local" step="1" value={local(v.measurement.end)} onChange={e => patch({ measurement: { ...v.measurement, end: iso(e.target.value) } })} /></label>
      <label>Microtrip 주기 (초)<input type="number" min="0.1" max="86400" step="0.1" value={v.interval_ms / 1000} onChange={e => patch({ interval_ms: Math.round(Number(e.target.value) * 1000) })} /></label>
      <label>운행 성격<select value={preset} onChange={e => patch({ pattern_mix: { ...presets[e.target.value] } })}>
        <option value="mixed">혼합 · 출퇴근60% / 주간20% / 비정기20%</option>
        <option value="commute">출퇴근 중심 · 80% / 10% / 10%</option>
        <option value="flexible">비정기 중심 · 20% / 30% / 50%</option>
        {preset === "custom" && <option value="custom" disabled>고급 설정의 사용자 비율</option>}
      </select></label>
      {(v.fleet_version==="fleet-plan-3" || v.fleet_version==="fleet-plan-4") ? <>
        {numeric("trip_min_minutes", "최소 운행 시간 (분/회)", 2, 240, "시동을 켜고 출발한 뒤 종료할 때까지 한 번의 전체 운행 시간 하한입니다. 측정 기간과 겹친 관측 부분은 더 짧을 수 있습니다.")}
        {numeric("trip_max_minutes", "최대 운행 시간 (분/회)", 2, 240, "한 번의 전체 운행 시간 상한입니다. 용무·출퇴근·긴 이동을 범위 안에서 선택하며 하루 합계나 최댓값을 채우지 않습니다. 최소값 이상이어야 합니다.")}
      </> : numeric("daily_driving_minutes", "이전 운행일 하루 목표 시간 (분)", 10, 480, "저장된 이전 배정의 설정입니다. 목표 없이 배정하려면 설정 복사 후 최소·최대 시간 배정을 선택하세요.")}

      <label><span className="label-line">하루 미운행 확률 (%) <Help text="각 차량이 하루 전체 운행을 쉬는 확률입니다. 기간 전체 미운행 차량 비율과 다릅니다. 주말 참여 여부와 별도로 적용됩니다." /></span><input aria-label="하루 미운행 확률 (%)" type="number" min="0" max="100" step="1" value={v.day_off_probability * 100} onChange={e => patch({ day_off_probability: Number(e.target.value) / 100 })} /></label>
      {numeric("anomaly_vehicle_percent", "이상 이벤트 대상 차량 (%)", 0, 100, "대상 차량을 명부에서 고정 선정합니다. 차량 수×비율을 가장 가까운 정수로 반올림합니다. 실제 이벤트 관측 차량 비율과는 다릅니다.", 0.1)}
      {numeric("anomaly_events_per_hour", "이상 이벤트 후보 빈도 (회/운행 1시간)", 0, 60, "대상 차량에 적용하는 운행1시간당 후보 발생 기댓값입니다. 속도·도로·회복 조건에 맞지 않으면 발생하지 않으므로 실제 횟수를 보장하지 않습니다.", 0.1)}
      <label>이상 이벤트 강도<select value={v.anomaly_intensity} onChange={e => patch({ anomaly_intensity: e.target.value as FleetConfig["anomaly_intensity"] })}><option value="low">낮음</option><option value="medium">보통</option><option value="high">높음</option></select></label>
    </div>
    <details className="schedule-advanced"><summary>고급 전체 조건 · 패턴·주말·도로·재현</summary>
      <div className="fields">
        {v.fleet_version!=="fleet-plan-4" && numeric("vehicle_variation_percent", "차량 간 운행 시간 편차 (±%)", 0, 50, "차량마다 선호 운행 길이의 편차를 정해 여러 날짜에 유지합니다. 새 배정에서는 최소·최대 시간 밖으로 나가지 않습니다.")}
      {numeric("daily_variation_percent", "같은 차량의 날짜별 편차 (±%)", 0, 50, "차량별로 날짜마다 공통 시간 계수를 정합니다. 최소·최대는 유지합니다. 0%는 이 계수만 고정하며, 목적별 길이 추첨과 출퇴근의 운행별 ±10% 변화는 남습니다.")}
        {numeric("departure_jitter_minutes", "날짜별 출발 편차 (±분)", 0, 120, "차량의 기본 첫 출발 시각에 더하는 날짜별 편차입니다. 후속 운행은 최소 주차30분과 자정까지 배치 가능한 시간을 고려합니다.")}
        <label><span className="label-line">주말 운행 참여 확률 (%) <Help text="주말은 이 확률로 운행 후보일이 되며, 후보일에도 하루 미운행 확률을 별도로 적용합니다. 출퇴근형도 주말에는 비정기 시간 배치를 사용합니다." /></span><input aria-label="주말 운행 참여 확률 (%)" type="number" min="0" max="100" value={v.weekend_activity_probability * 100} onChange={e => patch({ weekend_activity_probability: Number(e.target.value) / 100 })} /></label>
        {numeric("seed", "재현 seed", -2147483648, 2147483647, "같은 전체 조건과 seed는 차량 배정·요일별 일정·조회 결과를 재현합니다. 페이지나 조회 순서로 배정이 바뀌지 않습니다.")}
        {(Object.entries({ commute: "출퇴근형", daytime: "주간 운행형", flexible: "비정기형" }) as [keyof Mix, string][]).map(([key, title]) => <label key={key}>{title} 비율 (%)<input type="number" min="0" max="100" value={v.pattern_mix[key]} onChange={e => patch({ pattern_mix: { ...v.pattern_mix, [key]: Number(e.target.value) } })} /></label>)}
        {(Object.entries({ urban: "도심", highway: "고속도로", congested: "정체", mixed: "혼합" }) as [keyof FleetConfig["road_mix"], string][]).map(([key, title]) => <label key={key}>{title} 도로 배정 비중 (%)<input type="number" min="0" max="100" value={v.road_mix[key]} onChange={e => patch({ road_mix: { ...v.road_mix, [key]: Number(e.target.value) } })} /></label>)}
      </div>
      <p className="note">생활패턴 비율과 도로 배정 비중은 각각 합계100%로 지정합니다. 생활패턴은 정수 차량 수로 배정하고 도로 비중은 차량별 도로 성격을 추첨하는 가중치입니다. 다양한 생활 운행에서는 짧은 용무·운행 길이에 맞게 개별 도로 성격도 달라질 수 있습니다. 실제 경로·목적지·전국 교통을 수입한 모델은 아닙니다.</p>
    </details>
    <p className="note">계획 최대100대·30일·문맥 포함10,000운행, 장기 OBD 상태는 요청당 최대16회·48시간씩 나누어 준비합니다. 최소·최대는 운행 1회 기준이며 주차는 최소30분입니다. 이전 배정의 목표 시간×두 편차는 하루600분 이하여야 합니다. 설정만 바꿔도 이 상한이 확대되지는 않습니다.</p>
  </fieldset>;
}
