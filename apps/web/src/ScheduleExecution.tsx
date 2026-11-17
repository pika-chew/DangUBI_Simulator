import {useState} from "react";
import {api,type SavedRun} from "./SimulationTools";
import type {PlanRecord} from "./workspaceTypes";

export function ScheduleExecution({plan,activeId,onRun}:{plan:PlanRecord|null;activeId:string|null;onRun:(run:SavedRun,started?:boolean)=>void}) {
  const [realtime,setRealtime]=useState(true),[mapping,setMapping]=useState("shared"),[clients,setClients]=useState(1),[force,setForce]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  async function start(){if(!plan)return;setBusy(true);setError("");try{onRun(await api<SavedRun>("/api/workspace/execute",{plan_key:plan.key,realtime,mapping_mode:mapping,client_count:clients,force_end_trip:force}),true);}catch(e){setError(e instanceof Error?e.message:"실행 시작 실패");}finally{setBusy(false);}}
  return <section className="card"><h2>현재 일정 전체 실행</h2><p className="note">{plan?`${plan.title} · ${plan.vehicle_count}대`:"왼쪽에서 저장된 일정을 먼저 선택하세요."} · 모든 차량의 운행·주차·종료를 자동 진행합니다. 차량별 조회 버튼은 실행에 필요하지 않습니다.</p>
    <fieldset disabled={!plan||busy||!!activeId} className="schedule-fieldset"><div className="fields">
      <label>일정 실행 시간<select value={realtime?"real":"virtual"} onChange={e=>setRealtime(e.target.value==="real")}><option value="real">실제 시간 · 측정 시작부터 주기에 맞춰 실행</option><option value="virtual">가상시간 · 기다리지 않고 생성 확인</option></select></label>
      <label>일정 연결 배정<select value={mapping} onChange={e=>setMapping(e.target.value)}><option value="shared">연결 풀</option><option value="dedicated">차량별 전체 연결</option></select></label>
      <label>일정 Client 수<input type="number" min="1" max={plan?.vehicle_count || 1} disabled={mapping==="dedicated"} value={mapping==="dedicated"?plan?.vehicle_count || 1:clients} onChange={e=>setClients(Number(e.target.value))}/></label>
    </div><label className="schedule-check"><input type="checkbox" checked={force} onChange={e=>setForce(e.target.checked)}/>측정 끝에 운행 중인 차량의 시험용 Trip 생성</label>
    <p className="context-note">외부 전송 없는 실행입니다. Client 배정은 실행 전에 고정하지만 실제 MQTT 연결은 만들지 않습니다. 시험용 Trip도 전송하지 않습니다. 초기 실행 상한은 측정1시간·10,000메시지이며, 실제 시간은 과거 일정을 실행 시점부터 재생합니다.</p>
    <button className="primary" onClick={()=>void start()}>{busy?"시작 중…":"전체 일정 생성 실행 시작"}</button></fieldset>{error&&<p className="error" role="alert">{error}</p>}
  </section>;
}
