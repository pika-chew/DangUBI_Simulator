import {InfoHint} from "./InfoHint";
import {useState,useEffect} from "react";
import {api,type SavedRun} from "./SimulationTools";
import type {PlanRecord} from "./workspaceTypes";
import {scheduleReadiness,type ExecutionHealth} from "./scheduleReadiness";

export function ScheduleExecution({plan,activeId,onRun,mqttReady,onEdit,onHealth}:{plan:PlanRecord|null;activeId:string|null;mqttReady:boolean;onRun:(run:SavedRun,started?:boolean)=>void;onEdit:()=>void;onHealth:(ready:boolean)=>void}) {
  const [realtime,setRealtime]=useState(true),[mapping,setMapping]=useState("shared"),[clients,setClients]=useState(0),[force,setForce]=useState(false);
  const [transport,setTransport]=useState<"dry_run"|"mqtt">("dry_run"),[lifetime,setLifetime]=useState("always"),[confirmed,setConfirmed]=useState(false);
  const [workers,setWorkers]=useState(1);
  useEffect(()=>{setClients(0);setConfirmed(false);setForce(false);},[plan?.key]);
  const [health,setHealth]=useState<ExecutionHealth|null>(null),[check,setCheck]=useState(0);
  useEffect(()=>{let mounted=true;setHealth(null);void api<ExecutionHealth>("/api/health").then(h=>{if(mounted){setHealth(h);onHealth(h.mqtt_ready);}}).catch(()=>{});return()=>{mounted=false;};},[mqttReady,check]);
  const server=health?.mqtt_limits;
  const ready=health?.mqtt_ready ?? mqttReady;
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  async function start(){if(!plan)return;setBusy(true);setError("");try{onRun(await api<SavedRun>("/api/workspace/execute",{plan_key:plan.key,generation_workers:workers,realtime,mapping_mode:mapping,client_count:clients||undefined,force_end_trip:force,transport,confirm_mqtt:confirmed,connection_lifetime:lifetime}),true);}catch(e){setError(e instanceof Error?e.message:"실행 시작 실패");}finally{setBusy(false);}}
  const operations=plan?.request?.fleet?.execution_profile==="operations";
  const clientMax=plan?.vehicle_count??1;
  const mqttMax=server?Math.min(clientMax,server.max_clients,operations?25000:20):null;
  const selectedClients=mapping==="dedicated"?clientMax:clients||Math.min(operations?500:1,clientMax);
  const preflight=plan?scheduleReadiness(plan,transport,selectedClients,health):null;
  return <section className="card"><h2>현재 일정 전체 실행</h2><p className="note">{plan?`${plan.title} · ${plan.vehicle_count}대`:"왼쪽에서 저장된 일정을 먼저 선택하세요."} · 모든 차량의 운행·주차·종료를 자동 진행합니다. 차량별 조회 버튼은 실행에 필요하지 않습니다.</p>
    <ol className="note"><li>저장된 일정을 선택하고 아래 기간·예정 메시지·상한을 확인합니다.</li><li>실제 전송은 전송 방식에서 실제 MQTT 송신을 선택합니다. 전체 일정에 맞는 서버 접속 설정도 필요합니다.</li><li>차단 사유를 해결한 뒤 송신 확인을 선택하고 실행합니다. 결과는 작업 기록 → 연결 배정과 성능에서 확인합니다.</li></ol>
    {preflight&&<div className="context-note" aria-live="polite"><b>현재 일정 실행 전 확인</b><p>{operations?"운영":"소량 시험"} 일정 · 측정 기간 {preflight.duration===null?"확인 불가":`${(preflight.duration/3600000).toLocaleString(undefined,{maximumFractionDigits:3})}시간`} · 예정 {plan!.planned_messages.toLocaleString()}건 · 선택 Client {selectedClients.toLocaleString()}개</p>
      {preflight.limits&&<p>일정 실행 상한: {(preflight.limits.max_duration_ms/3600000).toLocaleString()}시간 이하 · {preflight.limits.max_messages.toLocaleString()}건 이하</p>}
      {transport==="mqtt"&&preflight.mqtt&&<p>현재 MQTT 상한: Client {preflight.mqtt.clients.toLocaleString()}개 · {preflight.mqtt.messages.toLocaleString()}건 · 일정은 {preflight.mqtt.seconds.toLocaleString()}초 미만</p>}
      {!ready&&<p>실제 MQTT 송신을 선택하려면 서버 접속 프로필부터 준비해야 합니다. DANGUBI_MQTT_PROFILE의 비공개 파일에 endpoint·CA·인증서·개인 키 경로·토픽을 설정하고 활성화한 뒤 서버 설정 다시 확인을 누르세요. 운영 일정은 configs/mqtt.operations.example.json을 참고하세요.</p>}
      {operations&&server?.execution_profile!=="operations"&&ready&&<p>생성 확인은 가능하지만 실제 일정 송신에는 운영 MQTT 서버 프로필이 필요합니다. 일정의 실행 프로필만 변경해도 서버 상한은 자동으로 바뀌지 않습니다.</p>}
      {!health?<p>서버 상한을 확인하지 못했습니다. 서버 설정 다시 확인을 누르세요.</p>:preflight.issues.length?<ul>{preflight.issues.map((item,i)=><li key={i}><b>{item.reason}</b><br/>{item.action}</li>)}</ul>:<p>확인된 기본 상한 안에 있습니다. 실행 시작 시 서버에서 최종 검사합니다{force?". 시험용 Trip 추가분도 최종 검사합니다":""}.</p>}
      <div className="schedule-actions"><button type="button" disabled={!!activeId||busy} onClick={onEdit}>{plan?.request?.fleet?"설정 복사해 수정":"새 전체 조건 작성"}</button><button type="button" onClick={()=>{setConfirmed(false);setCheck(n=>n+1);}}>서버 설정 다시 확인</button></div>
      <p>저장된 일정은 자동 변경되지 않습니다. 설정을 수정했다면 새로 생성·기록한 일정을 선택하세요. 현재 프로필 파일의 내용은 다시 확인할 때 읽습니다. DANGUBI_MQTT_PROFILE 경로를 바꾸는 경우 실행이 없는 때 서버를 재시작하세요.</p>
    </div>}
    {server&&<p className="note">서버 MQTT: {server.execution_profile==="operations"?"운영":"소량 시험"} · Client 최대{server.max_clients.toLocaleString()}개 · 접속 최대{server.connect_per_second.toLocaleString()}회/초 · 발행 최대{server.publish_per_second.toLocaleString()}건/초 · 동시 접속 처리 최대{server.max_connecting}개</p>}<fieldset disabled={!plan||busy||!!activeId} className="schedule-fieldset"><div className="fields">
      <label>전송 방식 <InfoHint label="전송 방식" text="생성만 확인하거나 실제 브로커로 보냅니다. 실제 송신은 서버 연결 설정이 필요합니다." /><select aria-label="일정 전송 방식" value={transport} onChange={e=>{const v=e.target.value as "dry_run"|"mqtt";setTransport(v);setConfirmed(false);if(v==="mqtt"){setRealtime(true);setForce(false);}}}><option value="dry_run">송신 없는 생성 확인</option><option value="mqtt" disabled={!ready}>실제 MQTT 송신{!ready?" · 서버 설정 필요":""}</option></select></label>
      <label>일정 연결 유지 <InfoHint label="연결 유지" text="기간 내내 연결하거나, 배정 차량이 운행할 때만 연결합니다. 운행 중만 연결하면 주차 중 접속 시간이 줄어듭니다." /><select value={lifetime} onChange={e=>setLifetime(e.target.value)}><option value="always">측정 기간 내내 유지</option><option value="while_driving">배정 차량의 운행 중 유지</option></select></label>
      <label>일정 실행 시간 <InfoHint label="실행 시간" text="실제 시간은 메시지 주기를 기다리고, 가상시간은 기다리지 않고 생성합니다. MQTT는 실제 시간만 지원합니다." range={operations?"최대30일 · MQTT 서버 상한 별도":"최대1시간·10,000메시지 · MQTT 서버 상한 별도"} /><select disabled={transport==="mqtt"} value={realtime?"real":"virtual"} onChange={e=>setRealtime(e.target.value==="real")}><option value="real">실제 시간 · 측정 시작부터 주기에 맞춰 실행</option><option value="virtual">가상시간 · 기다리지 않고 생성 확인</option></select></label>
      <label>일정 연결 배정 <InfoHint label="연결 배정" text="연결 풀은 여러 차량이 연결을 공유합니다. 차량별 연결은 한 차량에 한 연결을 배정합니다." /><select value={mapping} onChange={e=>setMapping(e.target.value)}><option value="shared">연결 풀</option><option value="dedicated">차량별 전체 연결</option></select></label>
      <label>일정 Client 수 <InfoHint label="Client 수" text="연결 풀의 연결 개수입니다. 늘리면 연결별 부담은 줄고 접속·메모리 부담은 늘어납니다." range={"풀1~"+clientMax.toLocaleString()+"개 · 차량별 연결은 "+clientMax.toLocaleString()+"개 고정"+(transport==="mqtt"?(mqttMax===null?" · 서버 송신 상한 미확인":" · 현재 MQTT 최대"+mqttMax.toLocaleString()+"개"):"")} /><input type="number" min="1" max={plan?.vehicle_count || 1} disabled={mapping==="dedicated"} value={mapping==="dedicated"?plan?.vehicle_count || 1:clients||Math.min(plan?.request?.fleet?.execution_profile==="operations"?500:1,plan?.vehicle_count||1)} onChange={e=>setClients(Number(e.target.value))}/></label>
      <label>생성 Worker 수 <InfoHint label="생성 Worker 수" text="차량 계산을 나눠 처리합니다. 늘리면 CPU·메모리를 더 쓰며 속도 향상은 환경에 따라 다릅니다." range="1·2·4개 중 선택 · 최대4개" /><select aria-label="생성 Worker 수" value={workers} onChange={e=>setWorkers(Number(e.target.value))}><option value={1}>1개 · 기본</option><option value={2}>2개</option><option value={4}>4개</option></select><span className="note">실행 중에는 변경할 수 없습니다.</span></label>
    </div><label className="schedule-check"><input type="checkbox" disabled={transport==="mqtt"} checked={force} onChange={e=>setForce(e.target.checked)}/>측정 끝에 운행 중인 차량의 시험용 Trip 생성</label>
    <p className="note">프로필: {plan?.request?.fleet?.execution_profile==="operations"?"운영 · 최대25,000대·30일, 연결 풀 기본500개":"소량 시험 · 최대100대·1시간·10,000메시지"}. 실제 MQTT는 서버 프로필의 연결·발행·접속 속도 제한도 적용합니다. 소량 시험의 실제 송신은 최대20연결·100건·30초를 추가 적용합니다.</p><p className="context-note">{transport==="mqtt"?"실제 시간에 OBD 본문만 QoS 1로 발행합니다. Client마다 한 건의 PUBACK을 기다리며 다른 Client는 병행 전송합니다. 전체 전송 속도와 대기열에는 공통 상한을 적용하고, 가득 차면 다음 생성을 기다립니다. 시험용 Trip과 가상시간 전송은 지원하지 않습니다. 서버 MQTT 연결·건수·시간 상한을 적용합니다.":"외부 전송 없는 실행입니다. Client 배정만 고정하며 실제 연결은 만들지 않습니다. 실행 상한은 선택한 프로필을 따릅니다."} 실제 시간은 측정 시작을 실행 시점에 대응시켜 재생합니다.</p>
    <p className="note">운행 중 유지는 같은 Client에 배정된 차량 중 한 대라도 운행 중이면 연결을 유지합니다. 모두 주차하면 마지막 응답 확인 후 해제합니다. 정차·신호 대기는 운행에 포함합니다. 연결 수와 배정표는 실행 중 바꾸지 않습니다.</p>
    {transport==="mqtt"&&<label className="schedule-check"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>설정된 시험 브로커로 실제 MQTT 송신을 확인했습니다.</label>}
    <button className="primary" disabled={!health||!!preflight?.issues.length||(transport==="mqtt"&&!confirmed)} onClick={()=>void start()}>{busy?"시작 중…":transport==="mqtt"?"전체 일정 MQTT 실행 시작":"전체 일정 생성 실행 시작"}</button></fieldset>{error&&<p className="error" role="alert">{error}</p>}
  </section>;
}
