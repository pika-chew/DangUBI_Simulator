import {InfoHint} from "./InfoHint";
import {useState,useEffect} from "react";
import {api,type SavedRun} from "./SimulationTools";
import type {PlanRecord} from "./workspaceTypes";
import {profileGuide,profileExecutionDefaults} from "./fleetSettings";
import {scheduleReadiness,type ExecutionHealth} from "./scheduleReadiness";

export function ScheduleExecution({plan,activeId,onRun,mqttReady,onEdit,onHealth}:{plan:PlanRecord|null;activeId:string|null;mqttReady:boolean;onRun:(run:SavedRun,started?:boolean)=>void;onEdit:()=>void;onHealth:(ready:boolean)=>void}) {
  const [realtime,setRealtime]=useState(true),[clients,setClients]=useState(0),[force,setForce]=useState(false);
  const [transport,setTransport]=useState<"dry_run"|"mqtt">("dry_run"),[lifetime,setLifetime]=useState("always"),[confirmed,setConfirmed]=useState(false);
  const [workers,setWorkers]=useState(1);
  useEffect(()=>{const d=profileExecutionDefaults(plan?.request?.fleet?.execution_profile==="operations"?"operations":"small_test");setClients(d.clients);setConfirmed(false);setForce(d.force);setRealtime(d.realtime);setTransport(d.transport);setLifetime(d.lifetime);setWorkers(d.workers);},[plan?.key]);
  const [health,setHealth]=useState<ExecutionHealth|null>(null),[check,setCheck]=useState(0);
  useEffect(()=>{let mounted=true;setHealth(null);void api<ExecutionHealth>("/api/health").then(h=>{if(mounted){setHealth(h);onHealth(h.mqtt_ready);}}).catch(()=>{});return()=>{mounted=false;};},[mqttReady,check]);
  const server=health?.mqtt_limits;
  const ready=health?.mqtt_ready ?? mqttReady;
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  async function start(){if(!plan)return;setBusy(true);setError("");try{onRun(await api<SavedRun>("/api/workspace/execute",{plan_key:plan.key,generation_workers:workers,realtime,mapping_mode:"shared",client_count:clients||undefined,force_end_trip:force,transport,confirm_mqtt:confirmed,connection_lifetime:lifetime}),true);}catch(e){setError(e instanceof Error?e.message:"실행 시작 실패");}finally{setBusy(false);}}
  const operations=plan?.request?.fleet?.execution_profile==="operations";
  const clientMax=plan?.vehicle_count??1;
  const mqttMax=server?Math.min(clientMax,server.max_clients,operations?25000:1000):null;
  const selectedClients=clients||Math.min(operations?500:1,clientMax);
  const preflight=plan?scheduleReadiness(plan,transport,selectedClients,health,realtime):null;
  const guide=profileGuide[operations?"operations":"small_test"];
  return <section className="card"><div className="panel-heading"><h2>일정 실행</h2><InfoHint label="프로필 실행 범위" text={guide.info} range={guide.range}/></div>
    <p className="note">{plan?`${plan.title} · ${operations?"운영":"시험 실행"} · ${plan.vehicle_count}대 · 예정 ${plan.planned_messages.toLocaleString()}건`:"저장된 일정을 선택하세요."}</p>
    {preflight&&<div aria-live="polite">
      <p className="context-note"><b>실행 전 확인</b> · {preflight.duration===null?"기간 확인 불가":`${(preflight.duration/3600000).toLocaleString(undefined,{maximumFractionDigits:3})}시간`} · {!health?"서버 설정 확인 필요":preflight.issues.length?"조건 수정 필요":"기본 상한 확인 완료"}</p>
      {preflight.issues.length>0&&<><ul className="note">{preflight.issues.map((item,i)=><li key={i}>{item.reason}</li>)}</ul><details><summary>해결 방법</summary><ul>{[...new Set(preflight.issues.map(i=>i.action))].map(action=><li key={action}>{action}</li>)}</ul></details></>}
      <div className="schedule-actions"><button type="button" disabled={!!activeId||busy} onClick={onEdit}>{plan?.request?.fleet?"설정 복사해 수정":"새 전체 조건 작성"}</button><button type="button" onClick={()=>{setConfirmed(false);setCheck(n=>n+1);}}>서버 설정 다시 확인</button></div>
    </div>}
    <fieldset disabled={!plan||busy||!!activeId} className="schedule-fieldset execution-fields"><div className="fields">
      <label><span className="label-line">전송 방식</span><select aria-label="일정 전송 방식" value={transport} onChange={e=>{const value=e.target.value as "dry_run"|"mqtt";setTransport(value);setConfirmed(false);if(value==="mqtt")setForce(false);}}><option value="dry_run">송신 없는 생성 확인</option><option value="mqtt" disabled={!ready}>실제 MQTT 송신</option></select></label>
      <label><span className="label-line">전송 속도 <InfoHint label="전송 속도" text="기본 전송은 일정의 시간 간격에 맞춥니다. 빠른 전송은 시간 간격의 대기만 건너뜁니다." /></span><select value={realtime?"real":"fast"} onChange={e=>{setRealtime(e.target.value==="real");setConfirmed(false);}}><option value="real">기본 전송</option><option value="fast">빠른 전송</option></select></label>
      <label><span className="label-line">Client 수 <InfoHint label="Client 수" text={`연결 풀 기본 ${Math.min(guide.defaultClients,clientMax)}개입니다.`} range={`1~${clientMax.toLocaleString()}개${transport==="mqtt"?` · 현재 MQTT 최대 ${mqttMax??"미확인"}개`:""}`}/></span><input type="number" min="1" max={transport==="mqtt"&&mqttMax!==null?mqttMax:clientMax} value={selectedClients} onChange={e=>setClients(Number(e.target.value))}/></label>
      <label><span className="label-line">연결 유지 <InfoHint label="연결 유지" text="운행 중 유지는 배정 차량이 모두 주차하면 연결을 해제합니다. 신호 대기는 운행에 포함합니다."/></span><select value={lifetime} onChange={e=>setLifetime(e.target.value)}><option value="always">기간 내내 · 기본</option><option value="while_driving">배정 차량 운행 중</option></select></label>
      <label><span className="label-line">생성 Worker <InfoHint label="생성 Worker" text={operations?"계산을 나누는 프로세스 수입니다. 늘리면 CPU·메모리를 더 사용합니다.":"계산을 나누는 프로세스 수입니다."} range="1·2·4개 · 기본1개"/></span><select aria-label="생성 Worker 수" value={workers} onChange={e=>setWorkers(Number(e.target.value))}><option value={1}>1개 · 기본</option><option value={2}>2개</option><option value={4}>4개</option></select></label>
    </div>
    <label className="schedule-check"><input type="checkbox" disabled={transport==="mqtt"} checked={force} onChange={e=>setForce(e.target.checked)}/>측정 종료 시 시험용 Trip <InfoHint label="시험용 Trip" text="측정 끝에 운행 중인 차량의 종료 Trip을 시험용으로 만듭니다. 실제 MQTT에서는 사용하지 않습니다."/></label>
    {!ready&&<p className="note">실제 MQTT: 서버 접속 설정 필요. 아래 접속 설정에서 준비 방법을 확인하세요.</p>}
    {transport==="mqtt"&&<label className="schedule-check"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>설정된 브로커로 실제 전송하는 데 동의합니다.</label>}
    <button className="primary" disabled={!health||!!preflight?.issues.length||(transport==="mqtt"&&!confirmed)} onClick={()=>void start()}>{busy?"시작 중…":transport==="mqtt"?"일정 MQTT 전송 시작":"일정 생성 확인 시작"}</button></fieldset>
    <details className="schedule-advanced"><summary>접속 설정·상한 자세히</summary>
      <p>{guide.range}</p>
      {server&&<p>서버: {server.execution_profile==="operations"?"운영":"소량"} · Client {server.max_clients.toLocaleString()}개 · 메시지 {server.max_messages.toLocaleString()}건 · 실행 {server.max_duration_seconds.toLocaleString()}초 · 접속 {server.connect_per_second.toLocaleString()}회/초 · 발행 {server.publish_per_second.toLocaleString()}건/초</p>}
      <p>운영 전송은 configs/mqtt.operations.example.json을 비공개 경로에 복사해 endpoint·인증서/키 경로·토픽·시험 상한을 설정합니다. 서버의 DANGUBI_MQTT_PROFILE로 파일을 지정하고 활성화·수신 규격 확인 후 서버 설정을 다시 확인하세요. 경로 변경으로 재시작할 때는 실행 중인 작업이 없어야 합니다.</p>
      <p>일정과 서버의 프로필은 별개입니다. 저장된 일정은 설정 복사 후 다시 기록합니다. 실제 전송은 OBD 본문을 QoS 1로 발행하며 PUBACK은 브로커 수신 확인입니다. 결과는 작업 기록의 연결 배정과 성능에서 확인합니다.</p>
    </details>
    {error&&<p className="error" role="alert">{error}</p>}
  </section>;
}
