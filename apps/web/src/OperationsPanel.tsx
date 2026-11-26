import {useCallback,useEffect,useState,type ReactNode} from "react";
import {InfoHint} from "./InfoHint";
import {api,type Message} from "./SimulationTools";
import {OperationVehicleView} from "./OperationVehicleView";
import {FocusEditor,FocusReportView} from "./FocusEditor";
import type {FocusReport} from "./trafficFocus";
import {defaultOperationSettings,defaultRegister,eventLabels,formatKST,kstDay,operationState,patternLabels,recoveryDiff,registerIssues,settingIssues,stopExplanation,
  type OperationSettings,type OperationStatus,type RegisterInput} from "./operationsView";

type Vehicle={id:number;vin?:string|null;vin_masked:string;pseudonym:string|null;status:string;status_label:string;pattern:string;anomaly_eligible:boolean;registered_at:string};
type VehiclePage={total:number;offset:number;limit:number;vehicles:Vehicle[]};
type OperationEvent={id:number;at:string;kind:string;detail:Record<string,unknown>};
const statusFilters:[string,string][]=[["","전체"],["pending_pseudonym","가명 대기"],["ready","대기"],["driving","운행"],["removing","정리 중"],["cleanup_hold","정리 보류"],["removed","제외"]];

/** 상시 운영: 차량 명부(합성 VIN·가명 VIN)와 연속 운행·전송을 관리한다. 합성 VIN은 전체를 표시한다. */
export function OperationsPanel({onActivity,renderMessages}:{onActivity:(text:string)=>void;renderMessages:(messages:Message[],vehicle:string,offset?:number)=>ReactNode}) {
  const [status,setStatus]=useState<OperationStatus|null>(null);
  const [settings,setSettings]=useState<OperationSettings>(defaultOperationSettings);
  const [settingsLoaded,setSettingsLoaded]=useState(false);
  const [register,setRegister]=useState<RegisterInput>(defaultRegister);
  const [filter,setFilter]=useState(""),[offset,setOffset]=useState(0);
  const [page,setPage]=useState<VehiclePage|null>(null);
  const [selected,setSelected]=useState<number[]>([]);
  const [events,setEvents]=useState<OperationEvent[]>([]);
  const [confirmMqtt,setConfirmMqtt]=useState(false),[confirmRemove,setConfirmRemove]=useState(false),[confirmForce,setConfirmForce]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState(""),[notice,setNotice]=useState("");
  const [inspect,setInspect]=useState<{id:number;token:number}|null>(null);
  const [previewDay,setPreviewDay]=useState(()=>kstDay(Date.now())),[focusPreview,setFocusPreview]=useState<FocusReport|null>(null);

  const loadStatus=useCallback(async()=>{const s=await api<OperationStatus>("/api/operations/status");setStatus(s);return s;},[]);
  const loadVehicles=useCallback(async()=>{
    const query=new URLSearchParams({offset:String(offset),limit:"100"});if(filter)query.set("status",filter);
    setPage(await api<VehiclePage>("/api/operations/vehicles?"+query));
  },[filter,offset]);
  const loadEvents=useCallback(async()=>setEvents((await api<{events:OperationEvent[]}>("/api/operations/events?limit=30")).events),[]);
  const reload=useCallback(async()=>{try{await Promise.all([loadStatus(),loadVehicles(),loadEvents()]);}catch(e){setError(e instanceof Error?e.message:"상시 운영 조회 실패");}},[loadStatus,loadVehicles,loadEvents]);

  useEffect(()=>{void reload();},[reload]);
  useEffect(()=>{if(!settingsLoaded&&status){if(status.settings)setSettings({...defaultOperationSettings,...status.settings});setSettingsLoaded(true);}},[status,settingsLoaded]);
  useEffect(()=>{
    if(!status?.running)return;
    const timer=setInterval(()=>{void loadStatus().then(s=>{if(!s.running)void reload();}).catch(()=>{});},2000);
    return()=>clearInterval(timer);
  },[status?.running,loadStatus,reload]);

  async function act(label:string,call:()=>Promise<unknown>,done?:string){
    setBusy(true);setError("");setNotice("");
    try{await call();if(done){setNotice(done);onActivity(done);}await reload();}
    catch(e){setError(e instanceof Error?e.message:label+" 실패");}
    finally{setBusy(false);}
  }
  const registry=status?.registry;
  const remaining=registry?registry.max_active-registry.active:0;
  const regIssues=registerIssues(register,remaining);
  const setIssues=settingIssues(settings);
  const recovery=status&&!status.running?status.recovery??null:null;
  const recoveryIssues=recovery?recoveryDiff(settings,recovery.settings):[];
  const running=!!status?.running;
  const change=status?.pending_settings??null;
  const waiting=change?.state==="waiting"||change?.state==="starting";
  const changes=running&&status?.settings?recoveryDiff(settings,{...defaultOperationSettings,...status.settings}):[];
  const focusNow=status?.live?.focus??null;
  const live=status?.live?.result;
  const pending=registry?.status.pending_pseudonym?.count??0;
  const holds=page?.vehicles.filter(v=>selected.includes(v.id)&&v.status==="cleanup_hold").map(v=>v.id)??[];
  const num=(value:number,set:(n:number)=>void,min:number,max:number,step=1)=><input type="number" min={min} max={max} step={step} value={value} onChange={e=>set(Number(e.target.value))}/>;
  const pct=(value:number,set:(n:number)=>void)=><input type="number" min={0} max={100} value={Math.round(value*100)} onChange={e=>set(Number(e.target.value)/100)}/>;

  return <div className="operations">
    {error&&<div className="error" role="alert">{error}<button aria-label="오류 닫기" onClick={()=>setError("")}>×</button></div>}
    {notice&&<p className="context-note" role="status">{notice}</p>}

    <section className="card"><div className="panel-heading"><div><h2>운영 상태</h2><p className="note">{operationState(status)}{status?.live?.recovering?" · 확인 지점부터 복구 중":""}</p></div>
      <div className="schedule-actions">
        {running
          ?<button className="primary" disabled={busy||waiting||!changes.length||setIssues.length>0||!!status?.live?.stopping||(settings.transport==="mqtt"&&!confirmMqtt)} onClick={()=>void act("설정 적용",()=>api("/api/operations/settings/apply",{settings,confirm_mqtt:confirmMqtt}),"설정 변경 요청 · 진행 중 운행을 마치면 새 설정으로 자동 시작합니다")}>설정 적용(자동 재시작)</button>
          :<button className="primary" disabled={busy||!status||waiting||setIssues.length>0||recoveryIssues.length>0||(recovery!==null&&!recovery.settings)||(settings.transport==="mqtt"&&!confirmMqtt)} onClick={()=>void act("운영 시작",()=>api("/api/operations/start",{settings,confirm_mqtt:confirmMqtt}),"상시 운영 시작")}>운영 시작</button>}
        <button disabled={busy||!running||status?.state==="stopping"||status?.state==="pausing"} onClick={()=>void act("운영 시각 제어",()=>api(status?.state==="paused"?"/api/operations/resume":"/api/operations/pause",{}))}>{status?.state==="paused"?"운영 재개":"일시중지"}</button>
        <button disabled={busy||!running||!!status?.live?.stopping} onClick={()=>void act("정상 중단",()=>api("/api/operations/stop",{force:false}),"정상 중단 요청 · 진행 중 운행을 마치고 멈춥니다")}>정상 중단</button>
        {!confirmForce?<button disabled={busy||!running} onClick={()=>setConfirmForce(true)}>강제 중단</button>
          :<span className="confirm-inline">진행 중 운행을 끊고 멈춥니다. 다음 시작 때 확인 지점부터 이어 보냅니다.<button disabled={busy} onClick={()=>{setConfirmForce(false);void act("강제 중단",()=>api("/api/operations/stop",{force:true}),"강제 중단 · 다음 시작 때 복구");}}>강제 중단 확인</button><button onClick={()=>setConfirmForce(false)}>취소</button></span>}
      </div></div>
      {change&&<div className="context-note" role="status"><b>{change.state==="blocked"?"설정 변경 · 자동 재시작 안 함":"설정 변경 대기 · 자동 재시작"}</b>
        <p>바뀐 항목: {change.changed.join(", ")}. {change.state==="blocked"?change.reason:change.state==="starting"?"새 설정으로 시작하고 있습니다.":`진행 중 운행${status?.live?.driving_now?` ${status.live.driving_now.toLocaleString()}대`:""}을 마치면 새 설정으로 자동 시작합니다. 멈춘 가상 시각에서 이어 갑니다.`}</p>
        {change.state!=="starting"&&<button disabled={busy} onClick={()=>void act("설정 변경 취소",()=>api("/api/operations/settings/cancel",{}),change.state==="blocked"?"설정 변경 대기를 정리했습니다":"자동 재시작 취소 · 정상 중단 후 멈춘 상태로 둡니다")}>{change.state==="blocked"?"대기 정리":"자동 재시작 취소"}</button>}</div>}
      {running&&!change&&changes.length>0&&<p className="context-note">입력한 설정이 실행 중 설정과 다릅니다: {changes.join(", ")}. ‘설정 적용(자동 재시작)’을 누르면 진행 중 운행을 마친 뒤 새 설정으로 자동 시작합니다.</p>}
      {stopExplanation(status)&&<p className="context-note">{stopExplanation(status)}</p>}
      {recovery&&<div className="context-note" role="status"><b>복구 대기 운행 {recovery.trips.toLocaleString()}건</b>
        <p>비정상 종료(강제 중단·오류·서버 종료) 때 보내던 운행입니다. 운영을 시작하면 확인된 메시지 다음부터 먼저 이어 보내며, 그 운행을 만든 당시 설정으로만 시작할 수 있습니다.</p>
        {!recovery.settings?<p>당시 설정 기록이 없어 이어 보낼 수 없습니다.</p>
          :recoveryIssues.length?<p>현재 입력이 당시 설정과 다릅니다: {recoveryIssues.join(", ")}. <button disabled={busy} onClick={()=>{setSettings({...defaultOperationSettings,...recovery.settings!});setConfirmMqtt(false);}}>당시 설정 불러오기</button></p>
          :<p>현재 입력이 당시 설정과 같습니다. 복구가 끝난 뒤 다음 시작부터 설정을 바꿀 수 있습니다.</p>}</div>}
      {status?.window_end_ms!=null&&<p className="note">가상 운영 종료 예정 · {formatKST(status.window_end_ms)} KST</p>}
      <div className="metric-grid">
        <div><span>운영 차량</span><b>{(status?.live?.vehicles??registry?.status.driving?.count??0).toLocaleString()}</b></div>
        <div><span>가상 시각 <InfoHint label="가상 시각" text="메시지 ts의 기준입니다. 처음 시작 때 그날 첫 출발 시각에서 시작해 1배속으로 흐르므로 실제 시각보다 늦을 수 있습니다. 일시중지 동안 멈추고 재개하면 이어서 흐릅니다."/></span><b>{formatKST(status?.live?.virtual_now_ms??status?.virtual_checkpoint_ms)}</b></div>
        <div><span>생성</span><b>{(live?.processed_messages??0).toLocaleString()}</b></div>
        <div><span>확인 <InfoHint label="확인" text="MQTT는 PUBACK(브로커 수신 확인), 송신 없는 생성은 생성 완료 기준입니다. 후속 저장 성공이 아닙니다."/></span><b>{(live?.confirmed_messages??0).toLocaleString()}</b></div>
        <div><span>Trip / Microtrip</span><b>{(live?.trip_count??0).toLocaleString()} / {(live?.microtrip_count??0).toLocaleString()}</b></div>
        <div><span>복구 운행</span><b>{(live?.recovered_trips??0).toLocaleString()}</b></div>
        {running&&<div><span>지금 운행 중</span><b>{(status?.live?.driving_now??0).toLocaleString()}대</b></div>}
      </div>
      {focusNow&&<p className="context-note" role="status">집중 시간대 {focusNow.start}~{focusNow.end} · 목표 {focusNow.target_percent}% · 계획(구간 평균) {focusNow.window_planned_percent}% · 계획(현재 10분) {focusNow.planned_percent.toFixed(1)}% · <b>실제 운행 {focusNow.actual_percent}%</b> <InfoHint label="집중 시간대 비교" text="계획은 날짜별로 고정된 조정 일정의 동시 운행 비율, 실제 운행은 지금 운행 중인 차량 비율입니다. 생성·확인(PUBACK)은 위 지표를 봅니다."/></p>}
      {status?.live?.mqtt&&<p className="note">MQTT · 접속 {status.live.mqtt.connected_clients??0} · 현재 연결 {status.live.mqtt.active_clients??0} · 발행 {status.live.mqtt.publish_attempts??0} · PUBACK {status.live.mqtt.puback_count??0} · 미확인 {status.live.mqtt.unconfirmed_messages??0}</p>}
      {live&&live.samples.length>0&&<details><summary>최근 전송 본문 표본 · 최대10건</summary><p className="note">본문 맨 앞의 vehicleId는 송신에 사용한 VIN입니다. 토픽: {live.samples[0].topic}</p><pre className="sample-json">{live.samples.map(s=>s.message).join("\n")}</pre></details>}
    </section>

    <section className="card"><div className="panel-heading"><h2>운영 설정</h2><InfoHint label="운영 설정" text="실행 중에는 고정됩니다. 실행 중에 바꾸고 ‘설정 적용(자동 재시작)’을 누르면 진행 중 운행을 마친 뒤 새 설정으로 자동 시작합니다."/></div>
      <fieldset disabled={busy||waiting} className="schedule-fieldset"><div className="fields">
        <label><span className="label-line">전송 방식 <InfoHint label="전송 방식" text="송신 없는 생성은 외부로 보내지 않습니다. 실제 MQTT는 서버 운영 프로필과 vault 가명 VIN이 필요합니다(개발용 가명은 로컬 브로커만)."/></span><select value={settings.transport} onChange={e=>{setSettings({...settings,transport:e.target.value as OperationSettings["transport"]});setConfirmMqtt(false);}}><option value="dry_run">송신 없는 생성</option><option value="mqtt">실제 MQTT 송신</option></select></label>
        <label><span className="label-line">Microtrip 주기 (초)</span>{num(settings.interval_ms/1000,n=>setSettings({...settings,interval_ms:Math.round(n*1000)}),1,60)}</label>
        <label><span className="label-line">Client 수 <InfoHint label="Client 수" text="연결 풀 크기입니다. 차량은 차량 수가 가장 적은 Client에 배정합니다." range="1~25,000 · 서버 상한 별도"/></span>{num(settings.client_count,n=>setSettings({...settings,client_count:n}),1,25000)}</label>
        <label><span className="label-line">가상 운영 기간 (일) <InfoHint label="가상 운영 기간" text="일시중지 시간은 제외합니다. 종료 전에 마칠 수 있는 운행을 시작합니다." range="1~30일 · 기본 7일"/></span>{num(settings.duration_cap_days,n=>setSettings({...settings,duration_cap_days:n}),1,30)}</label>
        <label><span className="label-line">운영 차량 수 <InfoHint label="운영 차량 수" text="저장된 차량 명부에서 등록 순서로 선택합니다. 비워 두면 준비된 차량을 최대 20,000대까지 사용합니다." range="1~20,000대"/></span><input type="number" min={1} max={20000} placeholder="최대 20,000대" value={settings.vehicle_count??""} onChange={e=>setSettings({...settings,vehicle_count:e.target.value===""?null:Number(e.target.value)})}/></label>
        <label><span className="label-line">하루 미운행 확률 (%)</span>{pct(settings.day_off_probability,n=>setSettings({...settings,day_off_probability:n}))}</label>
        <label><span className="label-line">주말 운행 참여 확률 (%)</span>{pct(settings.weekend_activity_probability,n=>setSettings({...settings,weekend_activity_probability:n}))}</label>
      </div>
      <details className="schedule-advanced"><summary>고급 운영 설정</summary><div className="fields">
        <label><span className="label-line">날짜별 편차 (±%)</span>{num(settings.daily_variation_percent,n=>setSettings({...settings,daily_variation_percent:n}),0,50)}</label>
        <label><span className="label-line">출발 편차 (±분)</span>{num(settings.departure_jitter_minutes,n=>setSettings({...settings,departure_jitter_minutes:n}),0,120)}</label>
        <label><span className="label-line">이상 이벤트 후보 빈도 (회/운행 1시간)</span>{num(settings.anomaly_events_per_hour,n=>setSettings({...settings,anomaly_events_per_hour:n}),0,60,.1)}</label>
        <label><span className="label-line">이상 이벤트 강도</span><select value={settings.anomaly_intensity} onChange={e=>setSettings({...settings,anomaly_intensity:e.target.value as OperationSettings["anomaly_intensity"]})}><option value="low">낮음</option><option value="medium">보통</option><option value="high">높음</option></select></label>
        <label><span className="label-line">생성 Worker</span><select value={settings.generation_workers} onChange={e=>setSettings({...settings,generation_workers:Number(e.target.value)})}><option value={1}>1개</option><option value={2}>2개</option><option value={4}>4개</option></select></label>
      </div></details>
      <details className="schedule-advanced" open={!!settings.traffic_focus}><summary>운행 집중 시간대</summary>
        <FocusEditor value={settings.traffic_focus} onChange={next=>{setSettings({...settings,traffic_focus:next});setFocusPreview(null);}}/>
        {settings.traffic_focus&&<div className="schedule-actions">
          <label>미리보기 날짜 (KST)<input type="date" value={previewDay} onChange={e=>{setPreviewDay(e.target.value);setFocusPreview(null);}}/></label>
          <button type="button" disabled={busy||setIssues.length>0} onClick={()=>void act("집중 미리보기",async()=>setFocusPreview(await api<FocusReport>("/api/operations/focus/preview",{settings,date:previewDay})))}>집중 미리보기</button></div>}
        {focusPreview&&<><p className="note">{focusPreview.date} · 현재 명부에서 운영에 넣을 {focusPreview.vehicles.toLocaleString()}대 기준 예상입니다. 실제 운영은 그 날짜를 처음 쓸 때의 운영 차량으로 정하고 고정합니다.</p><FocusReportView report={focusPreview}/></>}
      </details>
      {setIssues.length>0&&<ul className="note">{setIssues.map(i=><li key={i}>{i}</li>)}</ul>}
      {settings.transport==="mqtt"&&<label className="schedule-check"><input type="checkbox" checked={confirmMqtt} onChange={e=>setConfirmMqtt(e.target.checked)}/>설정된 브로커로 실제 전송하는 데 동의합니다.</label>}
      </fieldset>
      <p className="note">토픽 (접두어)/ops-main/(송신 VIN)/obd · Client sim-ops-main-cNNN · 본문 {"{vehicleId, ty, ts, pld}"}. 처음 시작하면 가상 시각이 그날(KST)의 가장 이른 출발 시각부터 흐르고, 시작 때 들어간 차량은 그날 운행을 모두 보냅니다. 운영 중에 추가한 차량은 다음 가상 날짜부터 운행합니다.</p>
    </section>

    <section className="card"><div className="panel-heading"><div><h2>차량 명부</h2><p className="note">{registry?`${registry.active.toLocaleString()} / ${registry.max_active.toLocaleString()}대 · 이상 이벤트 대상 ${registry.anomaly_eligible.toLocaleString()}대 · VIN 일련번호 잔여 ${registry.serial_remaining.toLocaleString()}`:"조회 중"}</p></div></div>
      {registry?.send_identity==="synthetic_raw"?<p className="context-note">Vault 도입 전 임시 송신 모드입니다. 토픽과 본문 vehicleId에 가명 대신 시뮬레이터 합성 VIN을 보냅니다(서버 설정 DANGUBI_OPERATION_VIN_MODE).</p>
        :registry&&!registry.provider_allows_real_transport&&<p className="context-note">현재 가명 VIN은 개발용 발급기로 만든 값입니다. 외부 브로커(AWS) 전송에는 vault 연동이 필요하며, 로컬 브로커 검증에만 쓸 수 있습니다.</p>}
      {registry&&<div className="metric-grid">
        {Object.entries(registry.patterns).map(([k,v])=><div key={k}><span>{patternLabels[k]??k}</span><b>{v.count.toLocaleString()}대 · {v.percent}%</b></div>)}
        {statusFilters.slice(1).map(([k,label])=><div key={k}><span>{label}</span><b>{(registry.status[k]?.count??0).toLocaleString()}</b></div>)}
      </div>}
      <p className="note">삭제로 어긋난 비율은 그대로 표시합니다. 차량을 추가하면 기존 차량은 바꾸지 않고 새 차량을 목표 비율에 모자란 패턴부터 배정합니다.</p>
      <details className="schedule-advanced" open><summary>차량 등록</summary>
        <fieldset disabled={busy} className="schedule-fieldset"><div className="fields">
          <label><span className="label-line">등록 대수</span>{num(register.count,n=>setRegister({...register,count:n}),1,25000)}</label>
          {(Object.keys(register.pattern_mix) as (keyof RegisterInput["pattern_mix"])[]).map(k=><label key={k}><span className="label-line">{patternLabels[k]} 목표 (%)</span>{num(register.pattern_mix[k],n=>setRegister({...register,pattern_mix:{...register.pattern_mix,[k]:n}}),0,100)}</label>)}
          {(Object.entries({urban:"도심",highway:"고속도로",congested:"정체",mixed:"혼합"}) as [keyof RegisterInput["road_mix"],string][]).map(([k,t])=><label key={k}><span className="label-line">{t} 도로 비중 (%)</span>{num(register.road_mix[k],n=>setRegister({...register,road_mix:{...register.road_mix,[k]:n}}),0,100)}</label>)}
          <label><span className="label-line">이상 이벤트 대상 (%)</span>{num(register.anomaly_vehicle_percent,n=>setRegister({...register,anomaly_vehicle_percent:n}),0,100,.1)}</label>
          <label><span className="label-line">최소 운행 시간 (분/회)</span>{num(register.trip_min_minutes,n=>setRegister({...register,trip_min_minutes:n}),2,240)}</label>
          <label><span className="label-line">최대 운행 시간 (분/회)</span>{num(register.trip_max_minutes,n=>setRegister({...register,trip_max_minutes:n}),2,240)}</label>
        </div>
        {regIssues.length>0&&<ul className="note">{regIssues.map(i=><li key={i}>{i}</li>)}</ul>}
        <div className="schedule-actions"><button className="primary" disabled={regIssues.length>0} onClick={()=>void act("차량 등록",()=>api("/api/operations/vehicles",register),`차량 ${register.count.toLocaleString()}대 등록 · 가상 VIN 생성·가명 발급`)}>VIN 생성·등록</button>
          <button disabled={!pending} onClick={()=>void act("가명 재발급",()=>api("/api/operations/pseudonyms/retry",{}),"가명 발급 재시도")}>가명 대기 {pending.toLocaleString()}대 재발급</button></div>
        </fieldset>
      </details>
    </section>

    <section className="card"><div className="panel-heading"><h2>차량 목록</h2><InfoHint label="차량 목록" text="시뮬레이터 합성 VIN은 17자리 전체를 표시합니다. 외부로 나가는 식별자는 서버의 VIN 송신 모드를 따릅니다: 기본은 가명 VIN, Vault 도입 전 임시 모드(synthetic_raw)는 합성 VIN."/></div>
      <div className="record-filters" role="group" aria-label="차량 상태">{statusFilters.map(([k,label])=><button key={k} aria-pressed={filter===k} onClick={()=>{setFilter(k);setOffset(0);setSelected([]);}}>{label}</button>)}</div>
      <div className="table-scroll"><table><thead><tr><th><span className="sr-only">선택</span></th><th>번호</th><th>VIN</th><th>가명 VIN</th><th>상태</th><th>생활패턴</th><th>이상 이벤트</th><th>등록</th><th><span className="sr-only">운행 조회</span></th></tr></thead>
        <tbody>{page?.vehicles.map(v=><tr key={v.id}><td><input type="checkbox" aria-label={`차량 ${v.id} 선택`} disabled={v.status==="removed"} checked={selected.includes(v.id)} onChange={e=>setSelected(old=>e.target.checked?[...old,v.id]:old.filter(x=>x!==v.id))}/></td><td>{v.id}</td><td><code>{v.vin??v.vin_masked}</code></td><td><code>{v.pseudonym??"—"}</code></td><td>{v.status_label}</td><td>{patternLabels[v.pattern]??v.pattern}</td><td>{v.anomaly_eligible?"대상":"—"}</td><td>{formatKST(Date.parse(v.registered_at))}</td><td><button onClick={()=>setInspect({id:v.id,token:Date.now()})}>운행 조회</button></td></tr>)}</tbody></table>
        {page&&!page.vehicles.length&&<p className="empty-state small">표시할 차량이 없습니다.</p>}</div>
      {page&&<div className="schedule-actions"><button disabled={offset===0} onClick={()=>setOffset(o=>Math.max(0,o-100))}>이전</button><span className="note">{page.total?`${offset+1}~${Math.min(page.total,offset+100)} / ${page.total.toLocaleString()}대`:"0대"}</span><button disabled={offset+100>=page.total} onClick={()=>setOffset(o=>o+100)}>다음</button></div>}
      <div className="schedule-actions">
        {!confirmRemove?<button disabled={!selected.length||busy} onClick={()=>setConfirmRemove(true)}>선택 {selected.length}대 제외</button>
          :<span className="confirm-inline">운행 중인 차량은 운행을 마치고 전송 확인 후 제외합니다. VIN 일련번호는 재사용하지 않습니다.<button onClick={()=>{setConfirmRemove(false);void act("차량 제외",()=>api("/api/operations/vehicles/remove",{ids:selected,confirm:true}),`차량 ${selected.length}대 제외 요청`).then(()=>setSelected([]));}}>제외 확인</button><button onClick={()=>setConfirmRemove(false)}>취소</button></span>}
        {holds.length>0&&<><button disabled={busy} onClick={()=>void act("보류 재시도",()=>api("/api/operations/vehicles/hold",{ids:holds,action:"retry"}),`정리 보류 ${holds.length}대 재시도 · 다음 운영 시작 때 마무리`)}>정리 보류 재시도</button>
          <button disabled={busy} onClick={()=>void act("손실 기록 후 제외",()=>api("/api/operations/vehicles/hold",{ids:holds,action:"drop"}),`정리 보류 ${holds.length}대 손실 기록 후 제외`)}>손실 기록 후 제외</button></>}
      </div>
    </section>

    <OperationVehicleView request={inspect} renderMessages={renderMessages} onActivity={onActivity}/>

    <section className="card"><div className="panel-heading"><h2>운영 기록</h2><button onClick={()=>void reload()}>새로고침</button></div>
      {events.length?<ul className="operation-events">{events.map(e=><li key={e.id}><time>{formatKST(Date.parse(e.at))}</time><b>{eventLabels[e.kind]??e.kind}</b><span>{describe(e)}</span></li>)}</ul>:<p className="empty-state small">아직 운영 기록이 없습니다.</p>}
    </section>
  </div>;
}

function describe(e:OperationEvent):string {
  const d=e.detail;
  if(e.kind==="start")return `${d.transport==="mqtt"?"MQTT":"송신 없는 생성"}${d.recovering?" · 확인 지점부터 복구":""}`;
  if(e.kind==="stopping"||e.kind==="graceful_stop")return String(d.reason??"");
  if(e.kind==="abnormal_stop")return `${d.error??"강제 중단"}${d.cleanup_hold?` · 정리 보류 ${d.cleanup_hold}대`:""}`;
  if(e.kind==="recover_trip"||e.kind==="removed"||e.kind==="cleanup_loss")return `차량 ${d.vehicle_id}`;
  return "";
}
