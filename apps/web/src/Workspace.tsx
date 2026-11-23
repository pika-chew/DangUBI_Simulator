import eventInspectionExample from "./event-inspection.example.json";
import { useCallback, useEffect, useRef, useState } from "react";
import { OperationsPanel } from "./OperationsPanel";
import { ScheduleExecution } from "./ScheduleExecution";
import { SchedulePanel } from "./SchedulePanel";
import { TransmissionPerformance } from "./TransmissionPerformance";
import { runView } from "./runView";
import { api, activeStates, Info, DrivingResult, MessageViewer, stateLabel, TransmissionBench, type Catalog, type Result, type SavedRun } from "./SimulationTools";
import type { PlanInput, PlanRecord, RunRecord, View, WorkspaceRecord } from "./workspaceTypes";

const titles: Record<View,string> = {settings:"조건 설정",data:"차량 데이터",transmission:"연결·전송",storage:"저장소"};
const paths: Record<string,string> = {
  plus:"M12 5v14M5 12h14", records:"M4 5h6l2 2h8v13H4z", data:"M4 19h16M5 15l4-6 4 4 6-9", link:"M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0V8m6 10v4", storage:"M4 6c0-4 16-4 16 0s-16 4-16 0v12c0 4 16 4 16 0V6M4 12c0 4 16 4 16 0", search:"M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14m5-2 6 6", theme:"M12 3a9 9 0 1 0 9 9c-5 3-12-4-9-9", refresh:"M20 7V3l-4 4M4 17v4l4-4M20 7a9 9 0 0 0-16 3m0 7a9 9 0 0 0 16-3", panel:"M3 4h18v16H3zM8 4v16", copy:"M8 8h12v12H8zM4 16V4h12", close:"m6 6 12 12M18 6 6 18",
};
function Icon({name}:{name:string}) { return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.records}/></svg>; }
function stamp(value:string) { return new Date(value).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}); }
function badge(r:WorkspaceRecord) {
  if(r.kind==="plan") return "일정 준비";
  return ({starting:"시작 중",running:"실행 중",pausing:"정지 중",paused:"일시정지",resuming:"재개 중",stopping:"중단 중",completed:"완료",stopped:"중단",interrupted:"종료 미확인",mqtt_failed:"전송 실패",failed:"실패",timed_out:"시간 초과",dry_run_completed:"생성 완료"} as Record<string,string>)[r.state] || "실행 기록";
}
function asRecord(run:SavedRun):RunRecord {
  return {key:"run:"+run.id,id:run.id,kind:"run",title:(run.request.transport==="mqtt" ? "MQTT 전송" : "로컬 생성")+" · "+run.request.config.vehicle_count+"대",created_at:run.created_at,state:run.result.job_state || run.result.state,vehicle_count:run.request.config.vehicle_count,planned_messages:run.result.planned_messages,transport:run.request.transport || "dry_run",request:run.request,result:run.result};
}

export function Workspace() {
  const [view,setView]=useState<View>("settings");
  // 시험 실행: 소량 생성·전송 확인 / 상시 운영: 차량 명부와 연속 운행·전송.
  const [mode,setMode]=useState<"test"|"operations">(()=>{try{return localStorage.getItem("dangubi-mode")==="operations"?"operations":"test";}catch{return "test";}});
  const [records,setRecords]=useState<WorkspaceRecord[]>([]);
  const [record,setRecord]=useState<WorkspaceRecord|null>(null);
  const [catalog,setCatalog]=useState<Catalog|null>(null);
  const [activeId,setActiveId]=useState<string|null>(null);
  const [health,setHealth]=useState({worker_ready:false,mqtt_ready:false});
  const [error,setError]=useState("");
  const [loading,setLoading]=useState(false);
  const [filter,setFilter]=useState("");
  const [kind,setKind]=useState("all");
  const [checked,setChecked]=useState<string[]>([]);
  const [review,setReview]=useState(false);
  const [draftToken,setDraftToken]=useState(0);
  const [draftInput,setDraftInput]=useState<PlanInput|undefined>();
  const [theme,setTheme]=useState(()=>localStorage.getItem("dangubi-theme") || "dark");
  const [sidebar,setSidebar]=useState(()=>window.innerWidth>=1000);
  const [sidebarWidth,setSidebarWidth]=useState(270);
  const [focus,setFocus]=useState(false);
  const [bottom,setBottom]=useState<"activity"|"settings"|null>(null);
  const [events,setEvents]=useState<{time:string;text:string}[]>([]);
  const [palette,setPalette]=useState(false),[commandQuery,setCommandQuery]=useState("");
  const paletteInput=useRef<HTMLInputElement>(null), paletteDialog=useRef<HTMLDivElement>(null), commandButton=useRef<HTMLButtonElement>(null);
  const loadID=useRef(0), lastStates=useRef<Record<string,string>>({});
  const log=useCallback((text:string)=>setEvents(old=>[{time:new Date().toLocaleTimeString("ko-KR",{timeZone:"Asia/Seoul",hour12:false}),text},...old].slice(0,100)),[]);
  const refresh=useCallback(async()=>{
    const data=await api<{records:WorkspaceRecord[]}>("/api/workspace/records");setRecords(data.records);
    setChecked(old=>old.filter(k=>data.records.some(r=>r.key===k)));setReview(false);
  },[]);
  const open=useCallback(async(key:string)=>{
    const id=++loadID.current;setLoading(true);setError("");
    try {const item=await api<WorkspaceRecord>("/api/workspace/records/"+encodeURIComponent(key));if(id!==loadID.current)return;setRecord(item);setView("data");window.location.hash=encodeURIComponent(key);log("작업 기록 열기 · "+item.title);}
    catch(e){if(id===loadID.current)setError(e instanceof Error ? e.message : "기록 조회 실패");}
    finally{if(id===loadID.current)setLoading(false);}
  },[log]);
  const onRun=useCallback((run:SavedRun,started=false)=>{
    const item=asRecord(run);
    setRecords(old=>[item,...old.filter(r=>r.key!==item.key)].sort((a,b)=>b.created_at.localeCompare(a.created_at)).slice(0,50));
    setRecord(old=>started || old?.key===item.key ? item : old);
    if(activeStates.has(item.state)) setActiveId(item.id); else setActiveId(old=>old===item.id ? null : old);
    if(lastStates.current[item.id]!==item.state){lastStates.current[item.id]=item.state;log(item.title+" · "+badge(item));}
    if(started){setView("data");window.location.hash=encodeURIComponent(item.key);}
  },[log]);
  useEffect(()=>{
    let alive=true;
    Promise.all([api<Catalog>("/api/schema"),api<{worker_ready:boolean;mqtt_ready:boolean;active_job_id:string|null}>("/api/health"),refresh()]).then(([c,h])=>{
      if(!alive)return;setCatalog(c);setHealth(h);setActiveId(h.active_job_id);
      const key=decodeURIComponent(window.location.hash.slice(1));if(key)void open(key);else if(h.active_job_id)void open("run:"+h.active_job_id);
    }).catch(e=>{if(alive)setError(e.message);});
    return()=>{alive=false;loadID.current++;};
  },[open,refresh]);
  useEffect(()=>{if(!activeId)return;let ended=false;let timer:ReturnType<typeof setTimeout>;
    async function poll(){try{const run=await api<SavedRun>("/api/jobs/"+activeId);if(ended)return;onRun(run);if(!activeStates.has(run.result.job_state || ""))return;}catch(e){if(!ended)setError(e instanceof Error?e.message:"실행 상태 확인 실패");}if(!ended)timer=setTimeout(poll,1000);}
    void poll();return()=>{ended=true;clearTimeout(timer);};
  },[activeId,onRun]);
  useEffect(()=>{localStorage.setItem("dangubi-theme",theme);},[theme]);
  useEffect(()=>{try{localStorage.setItem("dangubi-mode",mode);}catch{/* 저장 불가 환경은 무시 */}},[mode]);
  function newWork(input?:PlanInput){loadID.current++;setLoading(false);setRecord(null);setDraftInput(input);setDraftToken(n=>n+1);setView("settings");setError("");window.history.replaceState(null,"",window.location.pathname);log(input?"지정한 조건으로 새 작업 준비":"새 시뮬레이션 준비");}
  function toggleTheme(){setTheme(t=>t==="dark"?"light":"dark");}
  const commands=[{title:"새 시뮬레이션",action:()=>newWork()},{title:"조건 설정 열기",action:()=>setView("settings")},{title:"차량 데이터 열기",action:()=>setView("data")},{title:"연결·전송 열기",action:()=>setView("transmission")},{title:"저장소 관리 열기",action:()=>setView("storage")},{title:"작업 기록 새로고침",action:()=>void refresh().catch(e=>setError(e.message))},{title:"밝은 / 어두운 화면 전환",action:toggleTheme},{title:"집중 보기 전환",action:()=>setFocus(v=>!v)}].filter(c=>c.title.includes(commandQuery));
  useEffect(()=>{function key(e:KeyboardEvent){if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"){e.preventDefault();setPalette(p=>!p);setCommandQuery("");}if(e.key==="Escape"){setPalette(false);setFocus(false);}}window.addEventListener("keydown",key);return()=>window.removeEventListener("keydown",key);},[]);
  useEffect(()=>{if(palette)paletteInput.current?.focus();else commandButton.current?.focus({preventScroll:true});},[palette]);
  function recorded(item:PlanRecord){setRecord(item);setView("data");window.location.hash=encodeURIComponent(item.key);log("일정 생성·기록 완료 · "+item.title);void refresh().catch(e=>setError(e.message));}
  async function remove(){setLoading(true);setError("");try{await api("/api/workspace/delete",{keys:checked,confirm:true});if(record&&checked.includes(record.key))newWork();log(`작업 기록 ${checked.length}개 삭제`);setChecked([]);await refresh();}catch(e){setError(e instanceof Error?e.message:"삭제 실패");}finally{setLoading(false);}}
  const visible=records.filter(r=>(kind==="all"||r.kind===kind)&&(r.title+" "+r.id+" "+badge(r)).toLowerCase().includes(filter.toLowerCase()));
  const selectedPlan=record?.kind==="plan"?record:null;
  const selectedRun=record?.kind==="run"?record:null;
  const planView=selectedRun && view!=="storage" ? "none" : view;
  return <div className={`studio ${focus?"focus-mode":""} ${sidebar?"":"sidebar-collapsed"} ${mode==="operations"?"operations-mode":""}`} data-theme={theme} style={{"--sidebar-width":`${sidebarWidth}px`} as React.CSSProperties}>
    <header className="studio-titlebar"><div className="studio-brand"><span className="brand-mark">D</span><b>DANGUBI</b><span>차량 시뮬레이터</span></div><div className="mode-switch" role="tablist" aria-label="작업 공간">{([["test","시험 실행"],["operations","상시 운영"]] as const).map(([key,label])=><button key={key} role="tab" aria-selected={mode===key} onClick={()=>{setMode(key);log(label+" 화면");}}>{label}</button>)}</div><button className="command-trigger" ref={commandButton} onClick={()=>{setCommandQuery("");setPalette(true);}}><Icon name="search"/>빠른 명령<kbd>Ctrl K</kbd></button><div className="titlebar-status"><span className={`status-dot ${health.worker_ready?"ready":""}`}/>{health.worker_ready?"생성 엔진 준비":"엔진 확인 중"}</div></header>
    <div className="studio-body">
      <nav className="activity-rail" aria-label="작업 도구"><button title="새 시뮬레이션" aria-label="새 시뮬레이션" onClick={()=>newWork()}><Icon name="plus"/></button><button title="작업 탐색기 접기/펼치기" aria-label="작업 탐색기 접기/펼치기" aria-pressed={sidebar} onClick={()=>setSidebar(s=>!s)}><Icon name="records"/></button><button title="차량 데이터" aria-label="차량 데이터 열기" onClick={()=>setView("data")}><Icon name="data"/></button><button title="연결·전송" aria-label="연결·전송 열기" onClick={()=>setView("transmission")}><Icon name="link"/></button><button title="저장소" aria-label="저장소 열기" onClick={()=>setView("storage")}><Icon name="storage"/></button><button className="rail-bottom" title="화면 테마 전환" aria-label="화면 테마 전환" onClick={toggleTheme}><Icon name="theme"/></button></nav>
      {mode==="operations"?<main className="studio-main"><div className="workspace-heading"><div><span className="eyebrow">상시 운영</span><h1>상시 운영</h1><p>등록한 차량을 끝없이 이어지는 가상 시간으로 운행시키고 가명 VIN으로 전송합니다. 시험 실행과 별도로 동작합니다.</p></div></div><div className="workspace-content"><OperationsPanel onActivity={log}/></div></main>:<>
      <aside className="studio-sidebar" aria-label="작업 기록 탐색기"><div className="sidebar-heading"><h2>작업 기록</h2><button aria-label="작업 기록 새로고침" onClick={()=>void refresh().catch(e=>setError(e.message))}><Icon name="refresh"/></button></div><button className="new-work" onClick={()=>newWork()}><Icon name="plus"/>새 시뮬레이션</button><label className="record-search"><span className="sr-only">작업 기록 검색</span><Icon name="search"/><input placeholder="이름·상태·ID 검색" value={filter} onChange={e=>setFilter(e.target.value)}/></label><div className="record-filters" role="group" aria-label="기록 종류">{[["all","전체"],["plan","일정"],["run","실행"]].map(([key,label])=><button key={key} aria-pressed={kind===key} onClick={()=>setKind(key)}>{label}</button>)}</div><p className="sidebar-meta">최근50개 · {visible.length}개 표시</p>
      <div className="record-list">{visible.map(r=><div className={`record-row ${record?.key===r.key?"selected":""}`} key={r.key}><input aria-label={`기록 선택 ${r.title} ${r.id}`} type="checkbox" checked={checked.includes(r.key)} disabled={r.kind==="run"&&activeStates.has(r.state)} onChange={e=>{setReview(false);setChecked(old=>e.target.checked?[...old,r.key]:old.filter(k=>k!==r.key));}}/><button onClick={()=>void open(r.key)} aria-current={record?.key===r.key?"true":undefined}><span className="record-row-title">{r.title}</span><span className="record-row-meta"><span className={`record-badge ${activeStates.has(r.state)?"active":""}`}>{badge(r)}</span>{stamp(r.created_at)}</span><span className="record-row-meta">{r.vehicle_count}대 · {r.transport==="mqtt"?"MQTT":r.kind==="plan"?"로컬 조회":"로컬 실행"}</span></button></div>)}{!visible.length&&<p className="empty-state small">표시할 기록이 없습니다.</p>}</div>
      <div className="sidebar-delete"><button disabled={!checked.length||checked.length>30||loading} onClick={()=>setReview(true)}>선택 기록 삭제 · {checked.length}</button>{checked.length>30&&<p className="note">한 번에 최대30개를 선택하세요.</p>}{review&&<div className="delete-review"><b>선택한 {checked.length}개 기록을 영구 삭제할까요?</b><ul>{records.filter(r=>checked.includes(r.key)).map(r=><li key={r.key}>{r.title}</li>)}</ul><p>설정·요약을 복구할 수 없습니다. 중간 상태 캐시는 별도로 관리합니다.</p><button onClick={()=>void remove()} disabled={loading}>영구 삭제 확인</button><button onClick={()=>setReview(false)}>취소</button></div>}</div></aside>
      <div className="sidebar-resizer" role="separator" tabIndex={0} aria-label="탐색기 너비" aria-orientation="vertical" aria-valuemin={220} aria-valuemax={420} aria-valuenow={sidebarWidth} onKeyDown={e=>{if(e.key==="ArrowLeft"||e.key==="ArrowRight"){e.preventDefault();setSidebarWidth(w=>Math.max(220,Math.min(420,w+(e.key==="ArrowRight"?20:-20))));}}} onPointerDown={e=>e.currentTarget.setPointerCapture(e.pointerId)} onPointerMove={e=>{if(e.currentTarget.hasPointerCapture(e.pointerId))setSidebarWidth(Math.max(220,Math.min(420,e.clientX-48)));}} onPointerUp={e=>{if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId);}}/>
      <main className="studio-main"><div className="workspace-heading"><div><span className="eyebrow">시뮬레이션 / {record?record.kind==="plan"?"저장된 일정":"실행 기록":"새 작업"}</span><h1>{record?.title || draftInput?.title || "새 시뮬레이션"}</h1><p>{record?`${stamp(record.created_at)} · ${badge(record)} · ${record.vehicle_count}대`:"전체 조건을 설정하고 일정을 생성하세요."}</p></div><div className="workspace-heading-actions"><button title="이상 이벤트를 찾기 쉬운 시험용 고빈도 조건입니다. 모든 종류의 발생을 보장하지 않습니다." onClick={()=>newWork(eventInspectionExample as PlanInput)}>이벤트 확인 예시</button>{activeId&&<button className="active-job-button" onClick={()=>void open("run:"+activeId)}>● 실행 중 열기</button>}<button disabled={!selectedPlan?.request} onClick={()=>selectedPlan?.request&&newWork({...selectedPlan.request,title:selectedPlan.title+" · 복사"})}><Icon name="copy"/>설정 복사</button><button aria-label="집중 보기 전환" aria-pressed={focus} onClick={()=>setFocus(f=>!f)} title="집중 보기 · Esc로 돌아오기"><Icon name="panel"/></button></div></div>
      <div className="workspace-tabs" role="tablist" aria-label="시뮬레이션 작업">{(Object.entries(titles) as [View,string][]).map(([key,title])=><button key={key} role="tab" id={`tab-${key}`} aria-controls={`panel-${key}`} aria-selected={view===key} tabIndex={view===key?0:-1} onKeyDown={e=>{if(e.key==="ArrowRight"||e.key==="ArrowLeft"){e.preventDefault();const keys=Object.keys(titles) as View[];const next=keys[(keys.indexOf(key)+(e.key==="ArrowRight"?1:3))%4];setView(next);document.getElementById("tab-"+next)?.focus();}}} onClick={()=>setView(key)}>{title}</button>)}</div>
      <div className="workspace-content" role="tabpanel" id={`panel-${view}`} aria-labelledby={`tab-${view}`}>
      {error&&<div className="error" role="alert">{error}<button aria-label="오류 닫기" onClick={()=>setError("")}>×</button></div>}{loading&&<p className="context-note" role="status">작업 기록을 처리하고 있습니다.</p>}
      {catalog&&<div hidden={planView==="none" || view==="transmission"}><SchedulePanel view={planView} record={selectedPlan} draftToken={draftToken} draftInput={draftInput} onRecorded={recorded} onConfigure={()=>setView("settings")} onActivity={log} renderMessages={(messages,vehicle,offset=0)=><MessageViewer catalog={catalog} localOnly samples={messages.map((message,i)=>({message,vehicle_id:vehicle,sequence:offset+i+1,client_id:"",topic:""}))}/>} /></div>}
      {selectedRun&&catalog&&view==="data"&&<RunInspector record={selectedRun} catalog={catalog} onRun={onRun} onOpenPlan={key=>void open(key)}/>}
      {selectedRun&&view==="settings"&&<section className="card"><h2>실행 당시 설정</h2><p className="note">이 기록은 실행한 시점의 설정과 생성 모델로 조회합니다. 새 일정 설정과 자동 변환하거나 기존 기록을 변경하지 않습니다.</p><pre>{JSON.stringify(selectedRun.request,null,2)}</pre><button onClick={()=>newWork()}>새 일정 조건 설정</button></section>}
      <div hidden={view!=="transmission"}><ScheduleExecution onHealth={ready=>setHealth(h=>({...h,mqtt_ready:ready}))} onEdit={()=>selectedPlan?.request?.fleet?newWork({...selectedPlan.request,title:selectedPlan.title+" · 실행 조건 수정"}):newWork()} mqttReady={health.mqtt_ready} plan={selectedPlan} activeId={activeId} onRun={onRun}/><details className="card"><summary>개발용 · 별도 입력으로 접속 확인</summary><p className="context-note">선택 일정과 무관한 별도 접속 시험입니다. 일반 일정 실행에는 필요하지 않습니다.</p><TransmissionBench onRun={onRun} activeId={activeId}/></details></div>
      </div>
      <section className={`workspace-bottom ${bottom?"open":""}`}><div className="bottom-tabs"><button aria-pressed={bottom==="activity"} onClick={()=>setBottom(v=>v==="activity"?null:"activity")}>작업 활동</button><button aria-pressed={bottom==="settings"} onClick={()=>setBottom(v=>v==="settings"?null:"settings")}>선택 기록의 설정</button><button className="bottom-close" aria-label="하단 패널 접기" onClick={()=>setBottom(null)}>×</button></div>{bottom==="activity"&&<div className="activity-output" role="log" aria-label="작업 활동">{events.length?events.map((e,i)=><div key={i}><time>{e.time}</time><span>{e.text}</span></div>):<p>현재 화면에서 수행한 작업이 여기에 표시됩니다. 전송 원문 로그가 아닙니다.</p>}</div>}{bottom==="settings"&&<pre>{record?.request?JSON.stringify(record.request,null,2):"저장된 작업 기록을 선택하면 설정을 확인할 수 있습니다."}</pre>}</section>
      </main></>}
    </div><footer className="studio-statusbar"><span>로컬 작업공간</span><span>{health.worker_ready?"Worker 준비":"Worker 확인 필요"}</span><button onClick={()=>setView("transmission")}>{health.mqtt_ready?"MQTT 프로필 준비":"MQTT 설정 필요"}</button><span className="statusbar-end">KST · {record?badge(record):"새 조건"}</span></footer>
    {palette&&<div className="command-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)setPalette(false);}}><div ref={paletteDialog} role="dialog" aria-modal="true" aria-label="빠른 명령" className="command-palette" onKeyDown={e=>{if(e.key==="Tab"){const nodes=paletteDialog.current?.querySelectorAll<HTMLElement>("input,button");if(!nodes?.length)return;const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}if(e.key==="Enter"&&document.activeElement===paletteInput.current&&commands[0]){commands[0].action();setPalette(false);}}}><label className="command-search"><Icon name="search"/><input ref={paletteInput} aria-label="명령 검색" placeholder="명령을 검색하세요…" value={commandQuery} onChange={e=>setCommandQuery(e.target.value)}/><button aria-label="명령 창 닫기" onClick={()=>setPalette(false)}>Esc</button></label><div className="command-list">{commands.map(c=><button key={c.title} onClick={()=>{c.action();setPalette(false);}}>{c.title}<span>↵</span></button>)}{!commands.length&&<p className="empty-state">일치하는 명령이 없습니다.</p>}</div></div></div>}
  </div>;
}

function RunInspector({record,catalog,onRun,onOpenPlan}:{record:RunRecord;catalog:Catalog;onRun:(run:SavedRun,started?:boolean)=>void;onOpenPlan:(key:string)=>void}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  const result=record.result,request=record.request;
  if(!result||!request)return <p className="empty-state">실행 결과를 불러오세요.</p>;
  async function control(action:string){setBusy(true);setError("");try{onRun(await api<SavedRun>(`/api/jobs/${record.id}/${action}`,{}));}catch(e){setError(e instanceof Error?e.message:"실행 제어 실패");}finally{setBusy(false);}}
  const live=activeStates.has(result.job_state || "");
  return <div className="run-inspector"><section className="card"><div className="panel-heading"><div><h2>실행 결과</h2><p className="note">{stateLabel(result)} · {record.transport==="mqtt"?"MQTT 실행":"로컬 생성 실행"}</p></div>{live&&<div className="schedule-actions"><button disabled={busy || !["running","paused"].includes(result.job_state || "")} onClick={()=>void control(result.job_state==="paused"?"resume":"pause")}>{result.job_state==="paused"?"실행 재개":"일시정지"}</button><button disabled={busy} onClick={()=>void control("stop")}>실행 중단</button></div>}</div>{error&&<p role="alert" className="error">{error}</p>}<RunMetrics result={result} live={live}/><p className="note">PUBACK는 브로커의 수신 확인입니다. 차량 데이터 조회는 저장된 설정으로 재생성하며 당시 전송 원문이나 후속 저장 완료를 뜻하지 않습니다.</p>{result.errors?.map((e,i)=><p key={i} className="error">{e}</p>)}</section>
  {request.source_plan_key&&<section className="card"><h2>실행 대상 차량 데이터</h2><p className="note">전체 차량 일정을 자동 실행합니다. 원본 일정에서 차량별 그래프·구간 데이터를 조회할 수 있습니다. 실행 샘플은 최대10건입니다.</p><button onClick={()=>onOpenPlan(request.source_plan_key!)}>원본 일정의 차량 데이터 조회</button></section>}
  {runView(request)==="single_journey" ? <section className="card"><DrivingResult key={record.id} result={result} catalog={catalog}/></section> : <section className="card"><h2>생성 메시지 샘플 · 최대10건</h2><p className="note">현재 실행에서 생성한 표본입니다. 차량별 운행 그래프와 구간별 JSON은 위의 원본 일정 조회에서 확인하세요.</p><MessageViewer samples={result.samples || []} catalog={catalog}/></section>}
  <section className="card"><h2>연결 배정과 성능</h2><p className="note">Client {result.allocated_clients}개 · 연결 성공 {result.connection_count ?? "기록 없음"}회 · 해제 {result.disconnect_count ?? "기록 없음"}회 · {request.config.mapping_mode==="shared"?"연결 풀":"차량별 전체 연결"} · {result.reconnect_count || 0}회 재연결</p><details><summary>차량 → Client 배정표 · 최대100대</summary><div className="table-scroll"><table><thead><tr><th>차량</th><th>VIN</th><th>Client</th></tr></thead><tbody>{result.assignments?.map(a=><tr key={a.vehicle_id}><td>{a.vehicle_id}</td><td>{a.vin??"—"}</td><td>{a.client_id}</td></tr>)}</tbody></table></div></details><TransmissionPerformance result={result}/></section></div>;
}

function RunMetrics({result:r,live}:{result:Result;live:boolean}) {
  const remaining=r.remaining_messages ?? Math.max(0,r.planned_messages-r.processed_messages);
  const metrics=(entries:[string,string|number][]) => <div className="metric-grid">{entries.map(([name,value])=><div key={name}><span>{name} <Info name={name}/></span><b>{typeof value==="number"?value.toLocaleString():value}</b></div>)}</div>;
  return <>
    {metrics([["생성 확인",r.processed_messages],["MQTT 발행",r.mqtt_published],["PUBACK",r.puback_count],["응답 미확인",r.unconfirmed_messages || 0],["배정 Client",r.allocated_clients],["최대 주기 지연",`${r.schedule_delay_ms || 0} ms`]])}
    <p className="note">전체 {r.planned_messages.toLocaleString()}건 중 {r.processed_messages.toLocaleString()}건 생성 · 경과 {((r.elapsed_ms || 0)/1000).toFixed(1)}초 · 일시정지 {((r.paused_ms || 0)/1000).toFixed(1)}초 · 정지 제외 {((r.active_elapsed_ms || 0)/1000).toFixed(1)}초 · 운행 진행 {((r.simulation_time_ms || 0)/1000).toFixed(1)}초 · 전체 제한 {r.wall_limit_seconds ?? "—"}초</p>
    {live&&<progress aria-label="생성 진행률" max={Math.max(1,r.planned_messages)} value={r.processed_messages}/>}
    <details><summary>상세 처리 결과와 연결 상태</summary>
      {metrics([["미생성",remaining],["발행 미시도",r.unsent_messages || 0],["Microtrip",r.microtrip_count ?? "기록 없음"],["Trip",r.trip_count ?? "기록 없음"],["접속 확인 Client",r.connected_clients],[live?"현재 연결":"최종 보고 연결",r.active_clients || 0],["발행 시도",r.publish_attempts || 0],["재연결 시도",r.reconnect_count || 0],["생성 JSON 크기",`${r.payload_bytes.toLocaleString()} B`]])}
      <p className="note">일시정지 중에도 전체 제한시간은 흐릅니다. 연결 정리 {r.connection_cleanup_confirmed===true?"확인":r.connection_cleanup_confirmed===false?"미확인":"기록 없음"} · 강제 종료 {r.forced_termination?"발생":"없음"}</p>
      {r.transmission&&<p className="note">{r.transmission.mode==="parallel"?"Client별 동시 전송":"순차 전송"} · 대기 {r.transmission.queued_messages}/{r.transmission.queue_capacity} · 응답 대기 {r.transmission.in_flight} · 최대 대기 {r.transmission.peak_queued} · 최대 응답 대기 {r.transmission.peak_in_flight} · 생성 대기 {r.transmission.backpressure_count}회 · 대기 취소 {r.transmission.discarded_queued}건</p>}
      {r.transmission?.queue_byte_capacity!==undefined&&<p className="note">Client당 진행 상한 {r.transmission.per_client_in_flight}건 · 대기·진행·연결 명령 합계 {r.transmission.reserved_commands}/{r.transmission.queue_capacity}건 · 예약 크기 {((r.transmission.reserved_bytes??0)/1024).toFixed(1)}/{(r.transmission.queue_byte_capacity/1024).toFixed(0)} KiB · 최대 예약 {((r.transmission.peak_reserved_bytes??0)/1024).toFixed(1)} KiB</p>}
      {r.buffer_budget&&<p className="note">생성·송신 통합 예약 {r.buffer_budget.reserved_messages}/{r.buffer_budget.max_messages}건 · {(r.buffer_budget.reserved_bytes/1048576).toFixed(2)}/{(r.buffer_budget.max_bytes/1048576).toFixed(0)} MiB · 최대 예약 {(r.buffer_budget.peak_reserved_bytes/1048576).toFixed(2)} MiB · 전체 실행 자원 정리 {r.execution_cleanup_confirmed?"확인":"대기 또는 미확인"}</p>}
    </details>
  </>;
}
