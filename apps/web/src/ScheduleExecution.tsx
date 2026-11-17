import {useState} from "react";
import {api,type SavedRun} from "./SimulationTools";
import type {PlanRecord} from "./workspaceTypes";

export function ScheduleExecution({plan,activeId,onRun,mqttReady}:{plan:PlanRecord|null;activeId:string|null;mqttReady:boolean;onRun:(run:SavedRun,started?:boolean)=>void}) {
  const [realtime,setRealtime]=useState(true),[mapping,setMapping]=useState("shared"),[clients,setClients]=useState(1),[force,setForce]=useState(false);
  const [transport,setTransport]=useState<"dry_run"|"mqtt">("dry_run"),[lifetime,setLifetime]=useState("always"),[confirmed,setConfirmed]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  async function start(){if(!plan)return;setBusy(true);setError("");try{onRun(await api<SavedRun>("/api/workspace/execute",{plan_key:plan.key,realtime,mapping_mode:mapping,client_count:clients,force_end_trip:force,transport,confirm_mqtt:confirmed,connection_lifetime:lifetime}),true);}catch(e){setError(e instanceof Error?e.message:"실행 시작 실패");}finally{setBusy(false);}}
  return <section className="card"><h2>현재 일정 전체 실행</h2><p className="note">{plan?`${plan.title} · ${plan.vehicle_count}대`:"왼쪽에서 저장된 일정을 먼저 선택하세요."} · 모든 차량의 운행·주차·종료를 자동 진행합니다. 차량별 조회 버튼은 실행에 필요하지 않습니다.</p>
    <fieldset disabled={!plan||busy||!!activeId} className="schedule-fieldset"><div className="fields">
      <label>전송 방식<select aria-label="일정 전송 방식" value={transport} onChange={e=>{const v=e.target.value as "dry_run"|"mqtt";setTransport(v);setConfirmed(false);if(v==="mqtt"){setRealtime(true);setForce(false);}}}><option value="dry_run">송신 없는 생성 확인</option><option value="mqtt" disabled={!mqttReady}>실제 MQTT 송신{!mqttReady?" · 서버 설정 필요":""}</option></select></label>
      <label>일정 연결 유지<select value={lifetime} onChange={e=>setLifetime(e.target.value)}><option value="always">측정 기간 내내 유지</option><option value="while_driving">배정 차량의 운행 중 유지</option></select></label>
      <label>일정 실행 시간<select disabled={transport==="mqtt"} value={realtime?"real":"virtual"} onChange={e=>setRealtime(e.target.value==="real")}><option value="real">실제 시간 · 측정 시작부터 주기에 맞춰 실행</option><option value="virtual">가상시간 · 기다리지 않고 생성 확인</option></select></label>
      <label>일정 연결 배정<select value={mapping} onChange={e=>setMapping(e.target.value)}><option value="shared">연결 풀</option><option value="dedicated">차량별 전체 연결</option></select></label>
      <label>일정 Client 수<input type="number" min="1" max={plan?.vehicle_count || 1} disabled={mapping==="dedicated"} value={mapping==="dedicated"?plan?.vehicle_count || 1:clients} onChange={e=>setClients(Number(e.target.value))}/></label>
    </div><label className="schedule-check"><input type="checkbox" disabled={transport==="mqtt"} checked={force} onChange={e=>setForce(e.target.checked)}/>측정 끝에 운행 중인 차량의 시험용 Trip 생성</label>
    <p className="context-note">{transport==="mqtt"?"실제 시간에 OBD 본문만 QoS 1로 발행합니다. 한 건씩 PUBACK를 기다리며, 느리면 다음 생성도 기다립니다. 시험용 Trip과 가상시간 전송은 지원하지 않습니다. 서버 MQTT 연결·건수·시간 상한을 적용합니다.":"외부 전송 없는 실행입니다. Client 배정만 고정하며 실제 연결은 만들지 않습니다. 초기 상한은 측정1시간·10,000메시지입니다."} 실제 시간은 측정 시작을 실행 시점에 대응시켜 재생합니다.</p>
    <p className="note">운행 중 유지는 같은 Client에 배정된 차량 중 한 대라도 운행 중이면 연결을 유지합니다. 모두 주차하면 마지막 응답 확인 후 해제합니다. 정차·신호 대기는 운행에 포함합니다. 연결 수와 배정표는 실행 중 바꾸지 않습니다.</p>
    {transport==="mqtt"&&<label className="schedule-check"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>설정된 시험 브로커로 실제 MQTT 송신을 확인했습니다.</label>}
    <button className="primary" disabled={transport==="mqtt"&&!confirmed} onClick={()=>void start()}>{busy?"시작 중…":transport==="mqtt"?"전체 일정 MQTT 실행 시작":"전체 일정 생성 실행 시작"}</button></fieldset>{error&&<p className="error" role="alert">{error}</p>}
  </section>;
}
