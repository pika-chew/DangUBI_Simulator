import {useEffect,useRef,useState,type ReactNode} from "react";
import {InfoHint} from "./InfoHint";
import {EventFinder} from "./EventFinder";
import {SpeedChart} from "./TelemetryChart";
import {TripTimeline} from "./TripTimeline";
import {readJSONResponse} from "./http";
import type {Message,TracePoint} from "./SimulationTools";
import {historyStatusLabels,kstDay,dayRange,patternLabels,roadLabels} from "./operationsView";

type PublicVehicle={id:number;vin?:string|null;vin_masked:string;pseudonym:string|null;status_label:string;pattern:string;anomaly_eligible:boolean};
export type HistoryTrip={vehicle_id:number;trip_key:string;start_ms:number;end_ms:number;observed_start_ms:number;observed_end_ms:number;
  road_profile:string;status:keyof typeof historyStatusLabels;transport:"dry_run"|"mqtt";session:string|null;confirmed_ts:number|null;
  resume_after_ms:number|null;recoveries:number;started_at:string;finished_at:string|null};
type History={vehicle:PublicVehicle;trips:HistoryTrip[];has_more:boolean;from_ms:number;to_ms:number;total_trips:number;
  first_start_ms:number|null;last_end_ms:number|null;history_since:string;last_completed_end_ms:number|null};
type Delivery={status:string;status_label:string;transport:string;confirmed_ts:number|null;description:string;recoveries:number;duplicate_risk_after_ms:number|null};
type OBD={vehicleId?:string|null;messages:Message[];trace:TracePoint[];microtrip_count:number;has_more:boolean;trip:Message|null;model:string;pseudonym:string|null;
  initial_state:{lat:number;lon:number;odometer_m:number;coolant_c:number};same_worker:boolean|null;delivery:Delivery;warnings:string[];
  settings:{interval_ms:number;transport:string}};
type Loaded={data:OBD;start:number;end:number;offset:number};

const clock=(ms:number)=>new Date(ms).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const length=(ms:number)=>{const s=Math.max(0,Math.round(ms/1000));return `${Math.floor(s/3600)}시간 ${Math.floor(s%3600/60)}분 ${s%60}초`;};
const kst=(ms:number)=>new Date(ms+9*3600000).toISOString().slice(0,23);
const fromLocal=(value:string)=>Date.parse(value+(value.length===16?":00":"")+"+09:00");
/** 재생성한 종료 Trip이 실제로 확인됐는지 구분한다. 확인 전 운행의 Trip은 보낼 예정 값일 뿐 전송 내역이 아니다. */
const tripEvidence=(t:HistoryTrip)=>t.status==="completed"?(t.transport==="mqtt"?"PUBACK 확인된 Trip의 재생성":"생성 완료된 Trip의 재생성(송신 없음)")
  :t.status==="lost"?"보내지 않은 Trip · 손실 기록":"아직 확인되지 않은 Trip · 운행 종료 때 보낼 값";

/** 상시 운영 차량 운행 조회. 운행 이력의 초기 상태·당시 설정으로 선택 운행을 다시 생성해 보여 준다(메시지 원문 저장 없음). */
function withIdentity(message:Message, vehicleId?:string|null):Message {
  return vehicleId ? {vehicleId,...message} : message;
}

export function OperationVehicleView({request,renderMessages,onActivity}:{request:{id:number;token:number}|null;
  renderMessages:(messages:Message[],vehicle:string,offset?:number)=>ReactNode;onActivity:(text:string)=>void}) {
  const [query,setQuery]=useState("");
  const [history,setHistory]=useState<History|null>(null);
  const [fromDay,setFromDay]=useState(""),[toDay,setToDay]=useState("");
  const [tripKey,setTripKey]=useState("");
  const [summary,setSummary]=useState<OBD|null>(null);
  const [loaded,setLoaded]=useState<Loaded|null>(null);
  const [rangeStart,setRangeStart]=useState(""),[rangeEnd,setRangeEnd]=useState("");
  const [eventTime,setEventTime]=useState<number|undefined>();
  const [busy,setBusy]=useState(""),[error,setError]=useState("");
  const active=useRef<AbortController|null>(null);
  const top=useRef<HTMLElement|null>(null);
  const trips=history?.trips??[];
  const trip=trips.find(t=>t.trip_key===tripKey);
  const label=history?.vehicle.pseudonym??(history?`차량 ${history.vehicle.id}`:"");

  function begin(text:string){active.current?.abort();const c=new AbortController();active.current=c;setBusy(text);setError("");return c;}
  function finish(c:AbortController){if(active.current===c)setBusy("");}
  function fail(e:unknown,c:AbortController){if(!c.signal.aborted)setError(e instanceof Error?e.message:"조회 실패");}
  function clearDetail(){setSummary(null);setLoaded(null);setEventTime(undefined);}

  async function load(id:number,range?:{from:number;to:number},keep?:string){
    const c=begin("운행 이력을 불러오고 있습니다.");
    try{
      const params=new URLSearchParams(range?{from_ms:String(range.from),to_ms:String(range.to)}:{});
      const data=await readJSONResponse<History>(await fetch(`/api/operations/vehicles/${id}/history?${params}`,{signal:c.signal}));
      if(c.signal.aborted)return;
      setHistory(data);setFromDay(kstDay(data.from_ms));setToDay(kstDay(data.to_ms-1));
      const next=data.trips.find(t=>t.trip_key===keep)??data.trips.at(-1);
      clearDetail();setTripKey(next?.trip_key??"");
      if(next){setRangeStart(kst(next.start_ms));setRangeEnd(kst(next.end_ms));}
    }catch(e){fail(e,c);}finally{finish(c);}
  }
  async function lookup(){
    const text=query.trim();if(!text)return;
    const c=begin("차량을 찾고 있습니다.");
    try{
      const v=await readJSONResponse<PublicVehicle>(await fetch(`/api/operations/vehicles/lookup?q=${encodeURIComponent(text)}`,{signal:c.signal}));
      finish(c);if(!c.signal.aborted)await load(v.id);
    }catch(e){fail(e,c);finish(c);}
  }
  useEffect(()=>{if(request){setQuery(String(request.id));void load(request.id);top.current?.scrollIntoView({behavior:"smooth",block:"start"});}},[request?.token]);
  useEffect(()=>()=>active.current?.abort(),[]);

  async function obd(a?:number,b?:number,offset=0,whole=false,selected=trip){
    if(!selected||!history)return;
    const start=whole?selected.start_ms:a??fromLocal(rangeStart);
    const end=whole?selected.end_ms:b??fromLocal(rangeEnd);
    if(!Number.isFinite(start)||!Number.isFinite(end)||start>end||start<selected.start_ms||end>selected.end_ms){setError("조회 구간은 선택 운행의 출발~종료 안에서 시작 ≤ 종료로 지정하세요.");return;}
    const c=begin("선택 운행의 OBD를 다시 생성하고 있습니다.");
    setLoaded(null);
    try{
      const data=await readJSONResponse<OBD>(await fetch(`/api/operations/obd/${selected.vehicle_id}/${selected.trip_key}?start_ms=${start}&end_ms=${end}&offset=${offset}`,
        {method:"POST",headers:{"Content-Type":"application/json"},body:"{}",signal:c.signal}));
      if(c.signal.aborted)return;
      setLoaded({data,start,end,offset});setRangeStart(kst(start));setRangeEnd(kst(end));
      if(whole)setSummary(data);
      onActivity(`차량 ${selected.vehicle_id} 운행 조회 · Microtrip ${data.microtrip_count.toLocaleString()}건 · 재생성`);
    }catch(e){fail(e,c);}finally{finish(c);}
  }
  function choose(t?:HistoryTrip,auto=true){
    clearDetail();setTripKey(t?.trip_key??"");
    if(t){setRangeStart(kst(t.start_ms));setRangeEnd(kst(t.end_ms));if(auto)void obd(undefined,undefined,0,true,t);}
  }
  const range=dayRange(fromDay,toDay);
  const detail=summary??loaded?.data;
  const missingBefore=history&&history.last_completed_end_ms!=null&&(history.first_start_ms==null||history.last_completed_end_ms<history.first_start_ms);

  return <>
    <section ref={top} className="card"><div className="panel-heading"><div><h2>차량 운행 조회</h2>
      <p className="note">운행마다 초기 상태와 당시 설정만 기록하고, 선택한 운행을 같은 조건으로 다시 생성해 보여 줍니다. 메시지 원문은 저장하지 않습니다.</p></div>
      <InfoHint label="차량 운행 조회" text="명부 번호, VIN(시뮬레이터 합성 VIN) 또는 가명 VIN으로 찾습니다."/></div>
      <div className="schedule-actions">
        <label>명부 번호, VIN 또는 가명 VIN<input value={query} maxLength={128} placeholder="예: 1 또는 0DS…" onChange={e=>setQuery(e.target.value)} onKeyDown={e=>{if(e.key==="Enter")void lookup();}}/></label>
        <button className="primary" disabled={!!busy||!query.trim()} onClick={()=>void lookup()}>차량 조회</button>
      </div>
      {busy&&<p role="status" className="schedule-status">{busy}</p>}
      {error&&<p role="alert" className="error">{error}</p>}
      {history&&<>
        <p>차량 {history.vehicle.id} · VIN <code>{history.vehicle.vin??history.vehicle.vin_masked}</code> · 가명 VIN <code>{history.vehicle.pseudonym??"—"}</code> · {history.vehicle.status_label} · {patternLabels[history.vehicle.pattern]??history.vehicle.pattern}{history.vehicle.anomaly_eligible?" · 이상 이벤트 대상":""}</p>
        <fieldset disabled={!!busy} className="schedule-fieldset"><div className="schedule-actions">
          <label>시작 날짜 (KST)<input type="date" value={fromDay} onChange={e=>setFromDay(e.target.value)}/></label>
          <label>종료 날짜 (KST)<input type="date" value={toDay} min={fromDay} onChange={e=>setToDay(e.target.value)}/></label>
          <button disabled={!range} onClick={()=>range&&void load(history.vehicle.id,range,tripKey)}>기간 적용</button>
          <button onClick={()=>void load(history.vehicle.id)}>최근 운행 7일</button>
          <button onClick={()=>void load(history.vehicle.id,{from:history.from_ms,to:history.to_ms},tripKey)}>새로고침</button>
        </div>{!range&&<p className="note">기간은 시작 ≤ 종료이며 최대 31일입니다.</p>}</fieldset>
        <p>이 기간 운행 {trips.length.toLocaleString()}회{history.has_more?" 이상(처음 2,000회만 표시)":""} · 전체 기록 {history.total_trips.toLocaleString()}회{history.first_start_ms!=null?` · ${clock(history.first_start_ms)} → ${clock(history.last_end_ms!)}`:""}</p>
        <p className="note">운행 기록은 {new Date(history.history_since).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",hour12:false})} 이후 시작한 운행부터 남습니다. 운행 시각은 가상 시각입니다.{missingBefore?" 이 차량에는 기록 시작 전에 끝난 운행이 있으며, 재생성 근거가 없어 표시하지 않습니다.":""}</p>
        <TripTimeline key={`${history.vehicle.id}:${history.from_ms}:${history.to_ms}`} start={history.from_ms} end={history.to_ms} trips={trips} selectedKey={tripKey} vehicle={label} disabled={!!busy} onSelect={t=>choose(t)}/>
        {!trips.length&&<p className="schedule-empty">이 기간에 기록된 운행이 없습니다.</p>}
        {!!trips.length&&<label>조회 운행<select disabled={!!busy} value={tripKey} onChange={e=>choose(trips.find(t=>t.trip_key===e.target.value))}>{trips.map((t,i)=><option key={t.trip_key} value={t.trip_key}>운행 {i+1} · {clock(t.start_ms)} → {clock(t.end_ms)} · {historyStatusLabels[t.status]}</option>)}</select></label>}
        {trip&&<>
          <p>{roadLabels[trip.road_profile]??trip.road_profile} · 운행 {clock(trip.start_ms)} → {clock(trip.end_ms)} · <b>총주행시간 {length(trip.end_ms-trip.start_ms)}</b></p>
          <div className={`schedule-trip-status ${trip.status==="completed"?"":"synthetic"}`}><b>{historyStatusLabels[trip.status]} · {trip.transport==="mqtt"?"실제 MQTT":"송신 없는 생성"}</b>
            <p>{trip.status==="completed"?(trip.transport==="mqtt"?"종료 Trip까지 브로커 PUBACK을 받았습니다(후속 저장 성공은 아님).":"종료 Trip까지 생성했습니다. 외부로 보내지 않았습니다.")
              :trip.status==="sending"?`진행 중입니다. ${trip.confirmed_ts!=null?`최근 체크포인트의 마지막 확인 ${clock(trip.confirmed_ts)}`:"아직 확인된 메시지가 없습니다."}`
              :trip.status==="interrupted"?`운영이 비정상 종료돼 멈췄습니다. ${trip.confirmed_ts!=null?`${clock(trip.confirmed_ts)}까지 확인, `:""}다음 시작 때 그 다음 메시지부터 이어 보냅니다.`
              :`손실을 기록하고 제외했습니다. ${trip.confirmed_ts!=null?`${clock(trip.confirmed_ts)} 이후`:"전체"} 메시지는 보내지 않았습니다.`}
              {trip.recoveries>0&&` 복구 ${trip.recoveries}회 · ${trip.resume_after_ms!=null?clock(trip.resume_after_ms):"시작"} 이후 메시지는 같은 내용으로 중복 수신됐을 수 있습니다.`}</p></div>
          <EventFinder key={`${trip.vehicle_id}:${trip.trip_key}`} vehicle={label} path={`/api/operations/obd/${trip.vehicle_id}/${trip.trip_key}`} body={{}} disabled={!!busy} renderMessages={renderMessages}
            onSelect={ts=>{setEventTime(ts);void obd(Math.max(trip.start_ms,ts-30000),Math.min(trip.end_ms,ts+30000));}}/>
          <button className="primary" disabled={!!busy} onClick={()=>void obd(undefined,undefined,0,true)}>{summary?"OBD 상세 다시 조회":"OBD 상세 조회"}</button>
          <p className="note">운행 막대나 조회 운행을 선택하면 상세 데이터를 자동으로 다시 생성합니다.</p>
        </>}
      </>}
    </section>
    {detail&&trip&&<section className="card" aria-label="상시 운영 차량 데이터">
      <h2>차량 데이터</h2>
      <p>{label} · {clock(trip.start_ms)} 출발 · 모델 {detail.model} · Microtrip 주기 {detail.settings.interval_ms/1000}초</p>
      <ul className="note">{detail.warnings.map(w=><li key={w}>{w}</li>)}</ul>
      <p className="note">출발 위치 {detail.initial_state.lat.toFixed(6)}, {detail.initial_state.lon.toFixed(6)} · 초기 총주행거리 {(detail.initial_state.odometer_m/1000).toFixed(3)} km</p>
      {summary&&<>{summary.trip?<details><summary>종료 Trip 요약 · 총주행시간 {length(Number(summary.trip.pld.edt)-Number(summary.trip.pld.stt))} · {tripEvidence(trip)} · 표/JSON 보기</summary>{renderMessages([withIdentity(summary.trip,summary.vehicleId)],label)}</details>
        :<p className="note">종료 Trip이 없습니다.</p>}</>}
      {!summary&&<p className="note">선택한 구간의 결과입니다. 종료 Trip 요약은 ‘전체 운행 보기’로 확인합니다.</p>}
      <h3>Microtrip 구간 조회</h3>
      <fieldset disabled={!!busy} className="schedule-fieldset"><div className="fields">
        <label>조회 시작 (KST)<input type="datetime-local" step="0.001" value={rangeStart} onChange={e=>setRangeStart(e.target.value)}/></label>
        <label>조회 종료 (KST)<input type="datetime-local" step="0.001" value={rangeEnd} onChange={e=>setRangeEnd(e.target.value)}/></label>
      </div><div className="schedule-actions"><button onClick={()=>void obd()}>입력 구간 조회</button><button onClick={()=>void obd(undefined,undefined,0,true)}>전체 운행 보기</button></div></fieldset>
      {loaded&&<>
        <p className="schedule-range">조회 완료 · {clock(loaded.start)} → {clock(loaded.end)} · Microtrip {loaded.data.microtrip_count.toLocaleString()}건</p>
        <SpeedChart marker={eventTime} trace={loaded.data.trace} start={loaded.start} end={loaded.end} departure={trip.start_ms} disabled={!!busy} onRange={(a,b)=>void obd(a,b)}/>
        <p className="note">그래프를 좌우로 드래그하면 그래프·상태표·Microtrip 표/JSON이 함께 바뀝니다. 그래프는 최대 120점 요약입니다.</p>
        {!!loaded.data.trace.length&&<details><summary>주행 상태표 · 최대 120점</summary><div className="table-scroll"><table className="schedule-trace"><thead><tr><th>시각 (KST)</th><th>속도 (km/h)</th><th>운행 누적거리 (km)</th><th>상태</th><th>도로·상황</th></tr></thead>
          <tbody>{loaded.data.trace.map(p=><tr key={p.elapsed_seconds}><td>{clock(trip.start_ms+p.elapsed_seconds*1000)}</td><td>{p.speed_kmh.toFixed(1)}</td><td>{(p.distance_m/1000).toFixed(3)}</td><td>{p.state}</td><td>{p.context?.road} · {p.context?.reason}</td></tr>)}</tbody></table></div></details>}
        <div className="schedule-actions"><b>Microtrip · {loaded.data.microtrip_count?loaded.offset+1:0}~{loaded.offset+loaded.data.messages.length} / {loaded.data.microtrip_count.toLocaleString()}건</b>
          <button disabled={!!busy||loaded.offset===0} onClick={()=>void obd(loaded.start,loaded.end,Math.max(0,loaded.offset-100))}>이전 100건</button>
          <button disabled={!!busy||!loaded.data.has_more} onClick={()=>void obd(loaded.start,loaded.end,loaded.offset+100)}>다음 100건</button></div>
        <div className="interval-messages">{renderMessages(loaded.data.messages.map(message=>withIdentity(message,loaded.data.vehicleId)),label,loaded.offset)}</div>
      </>}
    </section>}
  </>;
}
