import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Message, TracePoint } from "./SimulationTools";
import { SpeedChart } from "./TelemetryChart";
import type {PlanRecord, PlanInput, View} from "./workspaceTypes";
import { FleetControls, defaultFleet, type FleetConfig } from "./FleetControls";

type Road = "urban" | "highway" | "congested" | "mixed";
type TripInput = { start: string; end: string; road_profile: Road };
type Slot = { start_time: string; duration_min_minutes: number; duration_max_minutes: number; departure_jitter_minutes: number; road_profile: Road };
type Vehicle = { vehicle_id: string; trips?: TripInput[]; pattern?: { weekdays: number[]; day_off_probability: number; baseline_jitter_minutes: number; slots: Slot[] }; overrides?: Record<string, TripInput[]> };
export type Schedule = { schedule_version: "schedule-plan-1"; mode: "manual" | "patterned"; seed: number; interval_ms: number; measurement: { start: string; end: string; timezone: "Asia/Seoul" }; vehicles: Vehicle[] };
type Trip = { vehicle_id: string; trip_key: string; start_ms: number; end_ms: number; observed_start_ms: number; observed_end_ms: number; road_profile: Road; started_before_window: boolean; continues_after_window: boolean };
export type Plan = { anomaly_target_count?: number; measurement_start_ms: number; measurement_end_ms: number; observed_trip_count: number; microtrip_count: number; trip_count: number; matched_trip_count: number; trips: Trip[]; has_more: boolean; vehicles: { vehicle_id: string; observed_trip_count: number; pattern?: string; anomaly_eligible?: boolean }[] };
type OBD = { status: "generated_local_only"; vehicle_assignment?: {pattern: string; anomaly_eligible: boolean}; messages: Message[]; trace: TracePoint[]; microtrip_count: number; has_more: boolean; trip: Message | null; synthetic_trip: boolean; initial_state: { lat: number; lon: number; odometer_m: number; coolant_c: number }; model: string };
type Restoration = {checkpoint_epoch:number; completed_preceding_trips: number; total_preceding_trips: number; computed_trips: number; reused_preceding_trips: number};
type Preparing = {status: "preparing"; restoration: Restoration};
type CacheStats = {epoch:number; entries:number; max_entries:number; ttl_seconds:number; database_bytes:number; max_database_bytes:number};
type Loaded = { data: OBD; start: number; end: number; offset: number };
const roads: Record<Road, string> = { urban: "도심", highway: "고속도로", congested: "정체", mixed: "혼합" };
const kst = (ms: number) => new Date(ms + 9 * 3600000).toISOString().slice(0, 23);
const iso = (local: string) => local ? local + (local.length === 16 ? ":00" : "") + "+09:00" : "";
const local = (value: string) => Number.isFinite(Date.parse(value)) ? kst(Date.parse(value)) : "";
const clock = (ms: number) => new Date(ms).toLocaleString("ko-KR", { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const day = () => kst(Date.now()).slice(0, 10);
const example = (): Schedule => {
  const d = day();
  return { schedule_version: "schedule-plan-1", mode: "manual", seed: 42, interval_ms: 2000, measurement: { start: d + "T13:00:00+09:00", end: d + "T18:00:00+09:00", timezone: "Asia/Seoul" }, vehicles: [
    { vehicle_id: "v000001", trips: [{ start: d + "T13:00:00+09:00", end: d + "T13:30:00+09:00", road_profile: "urban" }, { start: d + "T17:00:00+09:00", end: d + "T18:00:00+09:00", road_profile: "mixed" }] },
    { vehicle_id: "v000002", trips: [{ start: d + "T17:30:00+09:00", end: d + "T18:30:00+09:00", road_profile: "urban" }] },
    { vehicle_id: "v000003", trips: [] },
  ] };
};
const pattern = (): NonNullable<Vehicle["pattern"]> => ({ weekdays: [1, 2, 3, 4, 5], day_off_probability: 0.1, baseline_jitter_minutes: 10, slots: [{ start_time: "08:00", duration_min_minutes: 30, duration_max_minutes: 60, departure_jitter_minutes: 10, road_profile: "urban" }, { start_time: "18:00", duration_min_minutes: 30, duration_max_minutes: 60, departure_jitter_minutes: 10, road_profile: "mixed" }] });
async function post<T>(path: string, body: unknown, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map((e: string | { loc?: unknown[]; msg?: string }) => typeof e === "string" ? e : `${e.loc?.join(" · ")}: ${e.msg}`).join(" / ") : "요청을 처리하지 못했습니다.");
  return data;
}
function Hint({ text }: { text: string }) {
  return <span className="hint"><button type="button" className="info-button" aria-label={text}>i</button><span className="hint-content" role="tooltip">{text}</span></span>;
}
function RoadSelect({ value, onChange }: { value: Road; onChange: (road: Road) => void }) {
  return <label>도로 설정<select value={value} onChange={e => onChange(e.target.value as Road)}>{Object.entries(roads).map(([key, title]) => <option key={key} value={key}>{title}</option>)}</select></label>;
}

export function SchedulePanel({ renderMessages, view, record, draftToken, draftInput, onRecorded, onConfigure, onActivity }: {
  renderMessages: (messages: Message[], vehicle: string, offset?: number) => ReactNode;
  view: View | "none"; record: PlanRecord | null; draftToken:number; draftInput?:PlanInput;
  onRecorded:(record:PlanRecord)=>void; onConfigure:()=>void; onActivity:(message:string)=>void;
}) {
  const [title,setTitle]=useState("차량 시뮬레이션");
  const readOnly=!!record;
  const [setup, setSetup] = useState<"fleet" | "individual">("fleet");
  const [fleet, setFleet] = useState<FleetConfig>(defaultFleet);
  const [frozenFleet, setFrozenFleet] = useState<FleetConfig | null>(null);
  const [draft, setDraft] = useState<Schedule>(example);
  const [editor, setEditor] = useState(0);
  const [raw, setRaw] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [frozen, setFrozen] = useState<Schedule | null>(null);
  const [vehicle, setVehicle] = useState("");
  const [trips, setTrips] = useState<Trip[]>([]);
  const [tripKey, setTripKey] = useState("");
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [summary, setSummary] = useState<OBD | null>(null);
  const [rangeStart, setRangeStart] = useState("");
  const [rangeEnd, setRangeEnd] = useState("");
  const [force, setForce] = useState(false);
  const [restoration, setRestoration] = useState<Restoration | null>(null);
  const [cache, setCache] = useState<CacheStats | null>(null);
  const [cacheBusy, setCacheBusy] = useState(false);
  const [cacheError, setCacheError] = useState("");
  const cacheID = useRef(0);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const trip = trips.find(t => t.trip_key === tripKey);
  useEffect(() => {
    void cacheAction("GET");
    return () => { request.current?.abort(); cacheID.current++; };
  }, []);
  async function cacheAction(method: "GET" | "DELETE") {
    if (method === "DELETE") { request.current?.abort(); setBusy(""); setRestoration(null); }
    const id = ++cacheID.current;
    setCacheBusy(true); setCacheError("");
    try {
      const response = await fetch("/api/checkpoints", {method});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "중간 상태 관리 요청에 실패했습니다.");
      if (id === cacheID.current) setCache(data);
    } catch (e) { if (id === cacheID.current) setCacheError(e instanceof Error ? e.message : "조회 실패"); }
    finally { if (id === cacheID.current) setCacheBusy(false); }
  }
  function begin(label: string) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(label); setError(""); setRestoration(null);
    return controller;
  }
  function fail(e: unknown, c: AbortController) { if (!c.signal.aborted) setError(e instanceof Error ? e.message : "처리 실패"); }
  function done(c: AbortController) { if (request.current === c) { setBusy(""); setRestoration(null); } }
  function resetResults() {
    request.current?.abort(); setBusy(""); setError(""); setRestoration(null);
    setRaw(""); setPlan(null); setFrozen(null); setFrozenFleet(null); setTrips([]); setLoaded(null); setSummary(null); setVehicle(""); setTripKey("");
  }
  function edit(next: Schedule) { resetResults(); setDraft(next); }
  function editFleet(next: FleetConfig) { resetResults(); setFleet(next); }
  function editVehicle(next: Vehicle) { edit({ ...draft, vehicles: draft.vehicles.map((v, i) => i === editor ? next : v) }); }
  function clearOBD() { setLoaded(null); setSummary(null); setError(""); }
  function chooseTrip(t?: Trip) {
    clearOBD(); setForce(false); setTripKey(t?.trip_key || "");
    setRangeStart(t ? kst(t.observed_start_ms) : ""); setRangeEnd(t ? kst(t.observed_end_ms) : "");
  }
  async function vehicleTrips(schedule: Schedule | FleetConfig, id: string, c: AbortController, fleetSource = false) {
    const all: Trip[] = [];
    for (let offset = 0; ; offset += 100) {
      const page = await post<Plan>(`/api/${fleetSource ? "fleets" : "schedules"}/preview?vehicle_id=${id}&offset=${offset}`, schedule, c.signal);
      all.push(...page.trips);
      if (!page.has_more) break;
    }
    if (!c.signal.aborted) { setTrips(all); chooseTrip(all[0]); }
  }
  async function preview() {
    const c = begin("일정을 생성하고 작업 기록을 저장하고 있습니다.");
    setPlan(null); setLoaded(null); setSummary(null);
    try {
      const created=await post<PlanRecord>("/api/workspace/plans",{title:title.trim() || "차량 시뮬레이션",...(setup === "fleet" ? {fleet} : {schedule:draft})},c.signal);
      if (!c.signal.aborted) onRecorded(created);
    } catch (e) { fail(e,c); } finally { done(c); }
  }
  useEffect(() => {
    resetResults();
    const source=record?.request ?? draftInput;
    setTitle(source?.title || "차량 시뮬레이션");
    setSetup(source?.schedule ? "individual" : "fleet");
    setFleet(source?.fleet || defaultFleet());
    setDraft(source?.schedule || example()); setEditor(0);
    if (!record || !source) return;
    const c=begin("저장된 일정의 차량 목록을 불러오고 있습니다.");
    const input=source.fleet ?? source.schedule!;
    if (source.fleet) setFrozenFleet(source.fleet); else setFrozen(source.schedule!);
    void (async () => {
      try {
        const p=await post<Plan>(`/api/${source.fleet ? "fleets" : "schedules"}/preview`,input,c.signal);
        if (c.signal.aborted) return;
        setPlan(p); const id=p.vehicles[0].vehicle_id; setVehicle(id);
        await vehicleTrips(input,id,c,!!source.fleet);
      } catch (e) { fail(e,c); } finally { done(c); }
    })();
    return ()=>c.abort();
  },[record?.key,draftToken]);
  async function selectVehicle(id: string) {
    const source = frozenFleet ?? frozen;
    if (!source) return;
    const c = begin("차량의 운행을 불러오고 있습니다.");
    setVehicle(id); setTrips([]); chooseTrip();
    try { await vehicleTrips(source, id, c, !!frozenFleet); } catch (e) { fail(e, c); } finally { done(c); }
  }
  async function query(a?: number, b?: number, offset = 0, whole = false, forceValue = force) {
    const source = frozenFleet ?? frozen;
    if (!source || !trip) return;
    const start = whole ? trip.observed_start_ms : a ?? Date.parse(iso(rangeStart));
    const end = whole ? trip.observed_end_ms : b ?? Date.parse(iso(rangeEnd));
    if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start < trip.observed_start_ms || end > trip.observed_end_ms) { setError("조회 구간은 선택 운행의 관측 범위 안에서 시작 ≤ 종료로 지정하세요."); return; }
    const c = begin("선택 운행의 OBD를 생성하고 있습니다.");
    setLoaded(null);
    try {
      const stateResponse = await fetch("/api/checkpoints", {signal:c.signal});
      const stateInfo = await stateResponse.json();
      if (!stateResponse.ok) throw new Error(stateInfo.detail || "중간 상태 저장소를 확인할 수 없습니다.");
      if (c.signal.aborted) return;
      setCache(stateInfo);
      let previous = -1;
      let epoch: number = stateInfo.epoch;
      for (let batch = 0; batch < 100; batch++) {
        const data = await post<OBD | Preparing>(`/api/${frozenFleet ? "fleets" : "schedules"}/obd/${vehicle}/${trip.trip_key}?start_ms=${start}&end_ms=${end}&offset=${offset}`, { ...(frozenFleet ? {fleet: frozenFleet} : {schedule: frozen}), checkpoint_epoch: epoch, force_end_trip: forceValue && end === Date.parse(source.measurement.end) }, c.signal);
        if (c.signal.aborted) return;
        if (data.status === "preparing") {
          if (data.restoration.completed_preceding_trips <= previous) throw new Error("중간 상태 준비가 진행되지 않습니다. 다른 조회나 저장소 상태를 확인하고 다시 시작하세요.");
          previous = data.restoration.completed_preceding_trips;
          epoch = data.restoration.checkpoint_epoch;
          setRestoration(data.restoration);
          setBusy(`과거 운행 상태를 준비하고 있습니다. ${previous} / ${data.restoration.total_preceding_trips}회`);
          continue;
        }
        setLoaded({ data, start, end, offset }); setRangeStart(kst(start)); setRangeEnd(kst(end));
        if (whole) setSummary(data);
        void cacheAction("GET");
        onActivity(`${vehicle} 구간 조회 완료 · Microtrip ${data.microtrip_count.toLocaleString()}건 · 로컬 재생성`);
        return;
      }
      throw new Error("준비 요청 상한에 도달했습니다. 중간 상태는 보관되므로 다시 조회할 수 있습니다.");
    } catch (e) { fail(e, c); } finally { done(c); }
  }
  async function applyJSON() {
    const c = begin("일정 JSON을 확인하고 있습니다.");
    try {
      const next = JSON.parse(raw) as Schedule;
      await post<Plan>("/api/schedules/preview", next, c.signal);
      if (c.signal.aborted) return;
      next.seed ??= 42; next.interval_ms ??= 2000; next.schedule_version ??= "schedule-plan-1";
      next.measurement.timezone ??= "Asia/Seoul";
      next.vehicles = next.vehicles.map(v => next.mode === "manual" ? { ...v, trips: v.trips!.map(t => ({ ...t, road_profile: t.road_profile ?? "mixed" })) } : {
        ...v, overrides: v.overrides ?? {}, pattern: { ...v.pattern!, weekdays: v.pattern!.weekdays ?? [1, 2, 3, 4, 5], day_off_probability: v.pattern!.day_off_probability ?? 0, baseline_jitter_minutes: v.pattern!.baseline_jitter_minutes ?? 0, slots: v.pattern!.slots.map(slot => ({ ...slot, departure_jitter_minutes: slot.departure_jitter_minutes ?? 0, road_profile: slot.road_profile ?? "mixed" })) },
      });
      edit(next); setEditor(0);
    } catch (e) { fail(e, c); } finally { done(c); }
  }
  const current = draft.vehicles[editor] ?? draft.vehicles[0];
  const patchTrip = (i: number, value: Partial<TripInput>) => editVehicle({ ...current, trips: current.trips!.map((t, n) => n === i ? { ...t, ...value } : t) });
  const patchPattern = (value: Partial<NonNullable<Vehicle["pattern"]>>) => editVehicle({ ...current, pattern: { ...current.pattern!, ...value } });
  const patchSlot = (i: number, value: Partial<Slot>) => patchPattern({ slots: current.pattern!.slots.map((s, n) => n === i ? { ...s, ...value } : s) });
  return <section className="schedule-panel" aria-label="일정 기반 생성">
    <div hidden={view !== "settings"} className="banner"><b>차량 일정 생성</b><span>측정 기간 안에서 차량마다 다른 운행을 생성합니다. 설정과 요약을 기록하며, 전체 메시지 저장이나 MQTT 전송은 하지 않습니다.</span></div>
    <details className="card schedule-cache" open hidden={view !== "storage"}>
      <summary>차량 중간 상태 관리 · {cache ? `${cache.entries.toLocaleString()}건` : "확인 중"}</summary>
      <p className="note">전체 메시지가 아닌 운행 종료 상태만 보관합니다. 기본 위치는 .local/obd-state.sqlite3이며 실행 기록과 별개입니다. 설정·모델이 다르면 구분하고, 오래되거나 한도를 넘은 항목은 자동 정리합니다.</p>
      {cache && <p>보관 {cache.entries.toLocaleString()} / {cache.max_entries.toLocaleString()}건 · 마지막 사용 후 {cache.ttl_seconds / 3600}시간 · DB {(cache.database_bytes / 1024).toFixed(0)} KiB / {(cache.max_database_bytes / 1048576).toFixed(0)} MiB</p>}
      <div className="schedule-actions"><button disabled={cacheBusy} onClick={() => void cacheAction("GET")}>중간 상태 새로고침</button><button disabled={cacheBusy} onClick={() => void cacheAction("DELETE")}>중간 상태만 비우기</button></div>
      <p className="note">비워도 설정·실행 기록·이미 표시한 표/그래프는 유지합니다. 다음 조회에 필요한 상태는 다시 계산합니다. 진행 중인 준비 요청은 비우기 이전 상태를 다시 저장하지 않습니다.</p>
      {cacheError && <p className="error" role="alert">{cacheError}</p>}
    </details>
    <section className="card" hidden={view !== "settings"}>
      <div className="panel-heading"><div><h2>생성 조건</h2><p className="note">전체 조건을 지정한 뒤 일정을 기록하고 차량 데이터를 탐색합니다.</p></div><div className="config-actions"><button className="primary" disabled={!!busy || readOnly} onClick={()=>void preview()}>일정 생성·기록</button><button disabled={!!busy || readOnly} onClick={()=>{if(setup==="fleet") editFleet(defaultFleet()); else {edit(example());setEditor(0);}}}>초기값</button></div></div>
      <label>작업 이름<input value={title} disabled={!!busy || readOnly} maxLength={80} onChange={e=>setTitle(e.target.value)} /></label>
      {readOnly && <p className="context-note">저장된 설정은 고정됩니다. 상단의 ‘설정 복사’로 새 작업을 만들 수 있습니다.</p>}
      <label>설정 범위<select disabled={!!busy || readOnly} value={setup} onChange={e => { resetResults(); setSetup(e.target.value as "fleet" | "individual"); }}><option value="fleet">전체 조건 자동 배정 · 기본</option><option value="individual">개별 차량 설정 · 점검용</option></select></label>
      {setup === "fleet" && <FleetControls value={fleet} onChange={editFleet} disabled={!!busy || readOnly} />}
      <div hidden={setup !== "individual"}>
      <p className="note">모든 시각은 한국 시간(KST)입니다. 설정을 수정하면 이전 조회를 비우고 새 일정으로 다시 계획합니다.</p>
      <fieldset disabled={!!busy || readOnly} className="schedule-fieldset">
        <div className="fields">
          <label>측정 시작<input type="datetime-local" step="1" value={local(draft.measurement.start)} onChange={e => edit({ ...draft, measurement: { ...draft.measurement, start: iso(e.target.value) } })} /></label>
          <label>측정 종료<input type="datetime-local" step="1" value={local(draft.measurement.end)} onChange={e => edit({ ...draft, measurement: { ...draft.measurement, end: iso(e.target.value) } })} /></label>
          <label>일정 방식<select value={draft.mode} onChange={e => { const mode = e.target.value as Schedule["mode"]; edit({ ...draft, mode, vehicles: draft.vehicles.map(v => mode === "manual" ? { vehicle_id: v.vehicle_id, trips: [] } : { vehicle_id: v.vehicle_id, pattern: pattern(), overrides: {} }) }); }}><option value="manual">직접 지정</option><option value="patterned">자동 일정 · 개발 예시</option></select></label>
          <label>Microtrip 주기 (초)<input type="number" min="0.1" max="86400" step="0.1" value={draft.interval_ms / 1000} onChange={e => edit({ ...draft, interval_ms: Math.round(Number(e.target.value) * 1000) })} /></label>
          <label>seed <Hint text="같은 설정과 seed로 같은 일정을 재생성합니다. 날짜별 편차가 있어도 조회할 때마다 무작위로 바뀌지 않습니다." /><input type="number" value={draft.seed} onChange={e => edit({ ...draft, seed: Number(e.target.value) })} /></label>
        </div>
        <p className="note">방식을 바꾸면 차량 명부를 유지하고 운행 설정을 초기화합니다. 직접 지정의 운행 목록이 비어 있으면 해당 차량은 미운행입니다.</p>
        <div className="schedule-actions">
          <label>설정할 차량<select value={editor} onChange={e => setEditor(Number(e.target.value))}>{draft.vehicles.map((v, i) => <option value={i} key={v.vehicle_id}>{v.vehicle_id}</option>)}</select></label>
          <button disabled={draft.vehicles.length >= 100} onClick={() => { const id = Math.max(0, ...draft.vehicles.map(v => Number(v.vehicle_id.slice(1)))) + 1; edit({ ...draft, vehicles: [...draft.vehicles, draft.mode === "manual" ? { vehicle_id: `v${String(id).padStart(6, "0")}`, trips: [] } : { vehicle_id: `v${String(id).padStart(6, "0")}`, pattern: pattern(), overrides: {} }] }); setEditor(draft.vehicles.length); }}>차량 추가</button>
          <button disabled={draft.vehicles.length <= 1} onClick={() => { edit({ ...draft, vehicles: draft.vehicles.filter((_, i) => i !== editor) }); setEditor(0); }}>선택 차량 제거</button>
        </div>
        {draft.mode === "manual" ? <>
          <p className="note">{current.vehicle_id} · 운행 사이 최소 주차 30분 <Hint text="시동을 끄고 별도 운행을 시작할 때의 최소 간격입니다. 신호 대기나 정체 중 정차는 같은 운행에 포함됩니다." /> · 종료 시각에는 Trip만 생성합니다.</p>
          {!current.trips?.length && <p className="schedule-empty">설정한 운행이 없습니다. 이 차량은 미운행입니다.</p>}
          {current.trips?.map((t, i) => <div className="schedule-trip-edit" key={i}>
            <b>운행 {i + 1}</b>
            <label>출발<input type="datetime-local" step="1" value={local(t.start)} onChange={e => patchTrip(i, { start: iso(e.target.value) })} /></label>
            <label>종료<input type="datetime-local" step="1" value={local(t.end)} onChange={e => patchTrip(i, { end: iso(e.target.value) })} /></label>
            <RoadSelect value={t.road_profile} onChange={road_profile => patchTrip(i, { road_profile })} />
            <button onClick={() => editVehicle({ ...current, trips: current.trips!.filter((_, n) => n !== i) })}>운행 {i + 1} 제거</button>
          </div>)}
          <button onClick={() => { const last = current.trips?.at(-1); const start = last ? Date.parse(last.end) + 1800000 : Date.parse(draft.measurement.start); if (!Number.isFinite(start)) { setError("측정 시작 시각을 먼저 입력하세요."); return; } editVehicle({ ...current, trips: [...current.trips!, { start: iso(kst(start)), end: iso(kst(start + 1800000)), road_profile: "urban" }] }); }}>운행 추가</button>
        </> : <>
          <p className="note">자동 일정의 시간·확률·편차는 개발 예시입니다. 한국 운전자 대표 통계로 보정한 값은 아닙니다.</p>
          <div className="schedule-weekdays" role="group" aria-label="운행 요일">{["월", "화", "수", "목", "금", "토", "일"].map((title, i) => <label key={title}><input type="checkbox" checked={current.pattern!.weekdays.includes(i + 1)} onChange={e => patchPattern({ weekdays: e.target.checked ? [...current.pattern!.weekdays, i + 1].sort() : current.pattern!.weekdays.filter(n => n !== i + 1) })} />{title}</label>)}</div>
          <div className="fields">
            <label>하루 미운행 확률 (%) <Hint text="선택 요일에도 이 확률로 하루 운행을 쉬도록 계획합니다. 실제 통계가 아닌 설정값이며 날짜 예외가 있으면 예외가 우선합니다." /><input type="number" min="0" max="100" value={current.pattern!.day_off_probability * 100} onChange={e => patchPattern({ day_off_probability: Number(e.target.value) / 100 })} /></label>
            <label>차량별 고정 출발 편차 (±분) <Hint text="차량별로 한 번 정해져 여러 날짜에 유지되는 출발 시각 차이입니다. 아래의 날짜별 편차와 함께 적용됩니다." /><input type="number" min="0" max="120" value={current.pattern!.baseline_jitter_minutes} onChange={e => patchPattern({ baseline_jitter_minutes: Number(e.target.value) })} /></label>
          </div>
          {current.pattern!.slots.map((s, i) => <div className="schedule-slot" key={i}>
            <label>기준 출발<input type="time" value={s.start_time} onChange={e => patchSlot(i, { start_time: e.target.value })} /></label>
            <label>최소 운행 (분)<input type="number" min="1" max="1440" value={s.duration_min_minutes} onChange={e => patchSlot(i, { duration_min_minutes: Number(e.target.value) })} /></label>
            <label>최대 운행 (분)<input type="number" min="1" max="1440" value={s.duration_max_minutes} onChange={e => patchSlot(i, { duration_max_minutes: Number(e.target.value) })} /></label>
            <label>날짜별 출발 편차 (±분)<input type="number" min="0" max="120" value={s.departure_jitter_minutes} onChange={e => patchSlot(i, { departure_jitter_minutes: Number(e.target.value) })} /></label>
            <RoadSelect value={s.road_profile} onChange={road_profile => patchSlot(i, { road_profile })} /><button onClick={() => patchPattern({ slots: current.pattern!.slots.filter((_, n) => n !== i) })}>시간대 {i + 1} 제거</button>
          </div>)}
          <button disabled={current.pattern!.slots.length >= 16} onClick={() => patchPattern({ slots: [...current.pattern!.slots, { start_time: "12:00", duration_min_minutes: 30, duration_max_minutes: 30, departure_jitter_minutes: 0, road_profile: "urban" }] })}>운행 시간대 추가</button>
          <p className="note">설정한 시간대가 측정 범위 밖에 있거나 미운행으로 계획되면 관측 운행이 0회일 수 있습니다. 편차를 포함한 운행 사이 주차가 30분 미만이면 설정 오류로 안내합니다. 날짜별 예외는 아래 고급 JSON 설정의 overrides로 지정할 수 있습니다.</p>
        </>}
        <details className="schedule-advanced"><summary>고급 설정 · 일정 JSON 가져오기/수정</summary>
          <p className="note">날짜별 예외를 포함한 일정 설정입니다. OBD 전송 데이터가 아닙니다. ‘입력 JSON 적용’을 눌러야 설정에 반영됩니다.</p>
          <button onClick={() => setRaw(JSON.stringify(draft, null, 2))}>현재 설정 불러오기</button>
          <textarea aria-label="일정 설정 JSON" value={raw} onChange={e => setRaw(e.target.value)} spellCheck={false} />
          <button onClick={() => void applyJSON()}>입력 JSON 적용</button>
        </details>
      </fieldset>
      </div>
    </section>
    {busy && <p role="status" className="schedule-status">{busy}</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {!plan && !busy && view === "data" && <div className="empty-state"><h2>조회할 일정을 선택하세요</h2><p>왼쪽 작업 기록을 열거나 새 조건으로 일정을 만드세요.</p><button onClick={onConfigure}>조건 설정으로 이동</button></div>}
    {plan && <section className="card" hidden={view !== "data"}>
      <h2>차량·운행 탐색</h2>
      <p>전체 {plan.vehicles.length}대 · 관측 운행 {plan.observed_trip_count.toLocaleString()}회 · 예상 Microtrip {plan.microtrip_count.toLocaleString()}건 · 정상 종료 Trip {plan.trip_count.toLocaleString()}건</p>
      {plan.anomaly_target_count !== undefined && <p>이상 이벤트 대상 {plan.anomaly_target_count}대 · 실제 발생 차량 수와 다를 수 있습니다.</p>}
      <p className="note">예상 건수는 시험용 종료 Trip을 제외한 계획값입니다. 운행·주차 시간선은 선택 차량의 전체 관측 일정입니다.</p>
      <div className="fields">
        <label>조회 차량<select disabled={!!busy} value={vehicle} onChange={e => void selectVehicle(e.target.value)}>{plan.vehicles.map(v => <option key={v.vehicle_id} value={v.vehicle_id}>{v.vehicle_id} · 운행 {v.observed_trip_count}회</option>)}</select></label>
        <label>조회 운행<select disabled={!!busy || !trips.length} value={tripKey} onChange={e => chooseTrip(trips.find(t => t.trip_key === e.target.value))}>{!trips.length && <option value="">관측 운행 없음</option>}{trips.map((t, i) => <option key={t.trip_key} value={t.trip_key}>운행 {i + 1} · {clock(t.start_ms)} → {clock(t.end_ms)}</option>)}</select></label>
      </div>
      <div className="schedule-timeline-label"><span>■ 운행 · 회색은 주차</span><span>{clock(plan.measurement_start_ms)} → {clock(plan.measurement_end_ms)}</span></div>
      <div className="schedule-timeline" aria-label={`${vehicle} 운행·주차 시간선`}>
        {trips.map((t, i) => <button disabled={!!busy} className={t.trip_key === tripKey ? "selected" : ""} key={t.trip_key} style={{ left: `${(t.observed_start_ms - plan.measurement_start_ms) / (plan.measurement_end_ms - plan.measurement_start_ms) * 100}%`, width: `${Math.max(0.15, (t.observed_end_ms - t.observed_start_ms) / (plan.measurement_end_ms - plan.measurement_start_ms) * 100)}%` }} aria-label={`운행 ${i + 1} · ${clock(t.start_ms)}부터 ${clock(t.end_ms)}까지`} title={`운행 ${i + 1} · ${clock(t.start_ms)} → ${clock(t.end_ms)}`} onClick={() => chooseTrip(t)} />)}
      </div>
      {!trips.length && !busy && <p className="schedule-empty">이 차량은 측정 기간에 관측 운행이 없습니다. 주차 중 Microtrip이나 종료 Trip을 만들지 않습니다.</p>}
      {trip && <>
        {plan.vehicles.find(v => v.vehicle_id === vehicle)?.pattern && <p className="note">배정 패턴: {({commute:"출퇴근형",daytime:"주간 운행형",flexible:"비정기형"} as Record<string,string>)[plan.vehicles.find(v => v.vehicle_id === vehicle)!.pattern!]} · 이상 이벤트 {plan.vehicles.find(v => v.vehicle_id === vehicle)?.anomaly_eligible ? "대상" : "비대상"} 차량</p>}
        <p>{roads[trip.road_profile]} · 실제 운행 {clock(trip.start_ms)} → {clock(trip.end_ms)}</p>
        <p className="note">관측 범위 {clock(trip.observed_start_ms)} → {clock(trip.observed_end_ms)}{trip.started_before_window && " · 측정 시작 전 출발"}{trip.continues_after_window && " · 측정 종료 후에도 운행 중"}</p>
        <label className="schedule-check"><input type="checkbox" disabled={!!busy || !trip.continues_after_window} checked={force} onChange={e => { setForce(e.target.checked); clearOBD(); }} />측정 종료 시 시험용 Trip 생성 <Hint text="운행이 측정 끝 이후까지 계속될 때 그 시각까지의 시험 요약을 만듭니다. 정상 종료와 구분하며 이 화면에서는 전송하지 않습니다. 이후 실제 운행 상태를 변경하지 않습니다." /></label>
        {!trip.continues_after_window && <p className="note">선택 운행은 측정 기간 안에 정상 종료하므로 시험용 Trip 옵션이 비활성화됩니다.</p>}
        <button className="primary" disabled={!!busy} onClick={() => void query(undefined, undefined, 0, true)}>선택 운행 OBD 생성·조회</button>
        <p className="note">먼저 가까운 중간 상태에서 이어 계산합니다. 처음 조회하는 먼 구간은 요청당 최대16회·48시간의 운행을 나누어 준비한 뒤 자동으로 조회합니다.</p>
        {restoration && <div className="schedule-status" role="status">
          <p>중간 상태 준비 · {restoration.completed_preceding_trips} / {restoration.total_preceding_trips}회 완료</p>
          <progress aria-label="중간 상태 준비 진행률" max={Math.max(1,restoration.total_preceding_trips)} value={restoration.completed_preceding_trips} />
          <button onClick={() => { request.current?.abort(); setBusy(""); setRestoration(null); void cacheAction("GET"); }}>이후 준비 중단</button>
          <p className="note">추가 요청을 중단합니다. 이미 서버에 전달한 한 회차의 계산은 끝날 수 있으며 완료한 중간 상태는 다음 조회에 재사용합니다.</p>
        </div>}
      </>}
    </section>}
    {summary && trip && <section className="card" hidden={view !== "data"}>
      <h2>차량 데이터</h2>
      <p>{vehicle} · {clock(trip.start_ms)} 출발 · 모델 {summary.model}</p>
      <p className="note">출발 위치 {summary.initial_state.lat.toFixed(6)}, {summary.initial_state.lon.toFixed(6)} · 초기 총주행거리 {(summary.initial_state.odometer_m / 1000).toFixed(3)} km.</p>
      <div className={`schedule-trip-status ${summary.synthetic_trip ? "synthetic" : ""}`}><b>{summary.trip ? summary.synthetic_trip ? "시험용 종료 Trip" : "정상 종료 Trip" : "종료 Trip 없음"}</b><p>{summary.trip ? summary.synthetic_trip ? "측정 종료까지의 값만 집계한 시험 요약입니다. 실제 운행 종료가 아닙니다." : "실제 출발부터 종료까지의 전체 운행 요약입니다. 아래 조회 구간을 좁혀도 이 요약은 바뀌지 않습니다." : "이 운행은 측정 종료 이후에도 계속됩니다. 종료 전에는 정상 Trip을 생성하지 않습니다."}</p></div>
      {summary.trip && <details><summary>Trip 요약 · 표/JSON 보기</summary>{renderMessages([summary.trip], vehicle)}</details>}
      <h3>Microtrip 구간 조회</h3>
      <fieldset disabled={!!busy} className="schedule-fieldset"><div className="fields">
        <label>조회 시작 (KST)<input type="datetime-local" step="0.001" value={rangeStart} onChange={e => setRangeStart(e.target.value)} /></label>
        <label>조회 종료 (KST)<input type="datetime-local" step="0.001" value={rangeEnd} onChange={e => setRangeEnd(e.target.value)} /></label>
      </div><div className="schedule-actions"><button onClick={() => void query()}>입력 구간 조회</button><button onClick={() => void query(undefined, undefined, 0, true)}>전체 관측 구간 보기</button></div></fieldset>
      <p className="note">그래프를 좌우로 드래그하면 그래프·상태표·Microtrip 표/JSON이 함께 바뀝니다. 그래프는 최대 120점 요약이며 Trip 계산은 내부 전체 운행 기준입니다. 짧은 변화는 구간을 좁혀 확인하세요.</p>
      {loaded && <>
        <p className="schedule-range">조회 완료 · {clock(loaded.start)} → {clock(loaded.end)} · Microtrip {loaded.data.microtrip_count.toLocaleString()}건</p>
        <SpeedChart trace={loaded.data.trace} start={loaded.start} end={loaded.end} departure={trip.start_ms} disabled={!!busy} onRange={(a, b) => void query(a, b)} />
        {!!loaded.data.trace.length && <details><summary>주행 상태표 · 최대 120점</summary><div className="table-scroll"><table className="schedule-trace"><thead><tr><th>시각 (KST)</th><th>속도 (km/h)</th><th>RPM</th><th>상태</th><th>도로·상황</th></tr></thead><tbody>{loaded.data.trace.map(p => <tr key={p.elapsed_seconds}><td>{clock(trip.start_ms + p.elapsed_seconds * 1000)}</td><td>{p.speed_kmh.toFixed(1)}</td><td>{p.rpm.toFixed(0)}</td><td>{p.state}</td><td>{p.context?.road} · {p.context?.reason}</td></tr>)}</tbody></table></div></details>}
        <div className="schedule-actions"><b>Microtrip · {loaded.data.microtrip_count ? loaded.offset + 1 : 0}~{loaded.offset + loaded.data.messages.length} / {loaded.data.microtrip_count.toLocaleString()}건</b><button disabled={!!busy || loaded.offset === 0} onClick={() => void query(loaded.start, loaded.end, Math.max(0, loaded.offset - 100))}>이전 100건</button><button disabled={!!busy || !loaded.data.has_more} onClick={() => void query(loaded.start, loaded.end, loaded.offset + 100)}>다음 100건</button></div>
        <div className="interval-messages">{renderMessages(loaded.data.messages, vehicle, loaded.offset)}</div>
        <p className="note">조회 결과는 설정으로 재생성한 합성 데이터이며 브로커 수신 기록이 아닙니다. 표/JSON에는 OBD 본문만 표시하고 도로 상황과 모델 정보는 별도로 표시합니다.</p>
      </>}
    </section>}
  </section>;
}
