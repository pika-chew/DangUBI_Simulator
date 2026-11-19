import {useEffect,useRef,useState,type ReactNode} from "react";
import type {Message} from "./SimulationTools";
import {readJSONResponse} from "./http";

const names=["급출발","급좌회전","급우회전","급유턴","급감속","급가속","급정지","","과속"];
const clock=(ms:number)=>new Date(ms).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",hour12:false});
type Evidence={kind:string;start_ms:number;end_ms:number;start_speed_kmh:number;end_speed_kmh:number;acceleration_mps2:number;angle_deg:number;lateral_mps2:number;road:string;speed_limit_kmh:number};
type Row={sequence:number;message:Message;evidence:Evidence[]};
type Search={rows:Row[];next_offset:number;total:number;scanned:number;nonzero:number;zero:number;missing:number;complete:boolean};
type SearchResponse={status:string;event_search?:Search;restoration?:{checkpoint_epoch:number;completed_preceding_trips:number;total_preceding_trips:number}};
export function EventFinder({path,body,disabled,onSelect,renderMessages,vehicle}:{path:string;body:object;disabled:boolean;onSelect:(ts:number)=>void;renderMessages:(messages:Message[],vehicle:string,offset?:number)=>ReactNode;vehicle:string}) {
  const [filter,setFilter]=useState(0);
  const [supported,setSupported]=useState(false);
  const [phase,setPhase]=useState("idle");
  const [note,setNote]=useState("");
  const [rows,setRows]=useState<Row[]>([]);
  const [picked,setPicked]=useState<Row|null>(null);
  const [progress,setProgress]=useState({offset:0,total:0,nonzero:0,zero:0,missing:0,matched:0});
  const active=useRef<AbortController|null>(null);
  const running=phase==="running";
  useEffect(()=>{const c=new AbortController();void fetch("/api/health",{signal:c.signal}).then(r=>r.ok?r.json():null).then(v=>setSupported(v?.features?.microtrip_event_search===true)).catch(()=>{});return ()=>{c.abort();active.current?.abort();};},[]);
  function reset(value:number) {active.current?.abort();setFilter(value);setPhase("idle");setRows([]);setPicked(null);setNote("");setProgress({offset:0,total:0,nonzero:0,zero:0,missing:0,matched:0});}
  async function search(next=false) {
    active.current?.abort();const c=new AbortController();active.current=c;
    setPhase("running");setNote("");setRows([]);setPicked(null);
    let counts=next?{...progress}:{offset:0,total:0,nonzero:0,zero:0,missing:0,matched:0};
    if(!next)setProgress(counts);
    try {
      const cache=await readJSONResponse<{epoch:number}>(await fetch("/api/checkpoints",{signal:c.signal}));
      let epoch=cache.epoch,previous=-1,preparations=0;
      while(!c.signal.aborted) {
        const result=await readJSONResponse<SearchResponse>(await fetch(path+"?offset="+counts.offset+"&limit=100",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...body,checkpoint_epoch:epoch,search_events:true,event_mask:filter}),signal:c.signal}));
        if(c.signal.aborted)return;
        if(result.status==="preparing"&&result.restoration) {
          const r=result.restoration;
          if(r.completed_preceding_trips<=previous||++preparations>100)throw new Error("이전 운행 상태 준비가 진행되지 않습니다. 다시 검색하세요.");
          previous=r.completed_preceding_trips;epoch=r.checkpoint_epoch;
          setNote("이전 운행 상태 준비 "+previous+" / "+r.total_preceding_trips+"회");continue;
        }
        const page=result.event_search;
        if(!page)throw new Error("이벤트 검색을 지원하는 서버·Worker로 갱신한 뒤 사용하세요.");
        if(!page.complete&&page.next_offset<=counts.offset)throw new Error("검색이 진행되지 않아 중단했습니다.");
        counts={offset:page.next_offset,total:page.total,nonzero:counts.nonzero+page.nonzero,zero:counts.zero+page.zero,missing:counts.missing+page.missing,matched:counts.matched+page.rows.length};
        setProgress({...counts});setRows(page.rows);setNote("");
        // 응답당 최대100건만 표시하고 다음 페이지로 이어간다.
        if(page.rows.length||page.complete) {setPhase(page.complete?"complete":"partial");return;}
      }
    } catch(e) {if(!c.signal.aborted){setPhase("error");setNote(e instanceof Error?e.message:"이벤트 검색 실패");}}
  }
  return <section className="event-finder" aria-label="선택 운행 이벤트 찾기">
    <h3>선택 운행 이벤트 찾기</h3>
    <p className="note">현재 Microtrip 페이지와 관계없이 선택 운행의 전체 관측 범위를 검색합니다. 메시지를 추가 저장하지 않으며 실제 전송 성공 여부와는 구분합니다.</p>
    {!supported&&<p className="note">검색 기능은 서버·Worker 갱신 후 사용할 수 있습니다.</p>}
    <div className="schedule-actions"><label>이벤트 종류 <select value={filter} onChange={e=>reset(Number(e.target.value))}><option value={0}>전체 이벤트</option>{names.map((name,bit)=>name&&<option key={bit} value={1<<bit}>{name}</option>)}</select></label>
      <button disabled={disabled||running||!supported} onClick={()=>void search()}>이벤트 찾기</button>
      {running&&<button onClick={()=>{active.current?.abort();setPhase("cancelled");setNote("검색을 취소했습니다. 완료된 검색 구간만 집계합니다.");}}>검색 취소</button>}
      {phase==="partial"&&<button disabled={disabled} onClick={()=>void search(true)}>다음 구간 검색</button>}
    </div>
    {phase!=="idle"&&<><p role="status">{({running:"검색 중",complete:"검색 완료",partial:"일부 검색 완료 · 남은 구간 있음",cancelled:"검색 취소",error:"검색 실패"} as Record<string,string>)[phase]} · 확인 {progress.offset.toLocaleString()} / {progress.total.toLocaleString()}건</p>
      <progress aria-label="Microtrip 이벤트 검색 진행률" max={Math.max(1,progress.total)} value={progress.offset}/>
      <p>누적 확인 · 선택 종류 포함 {progress.matched.toLocaleString()}건 · 전체 종류 이벤트 포함 메시지 {progress.nonzero.toLocaleString()}건 · em=0 {progress.zero.toLocaleString()}건 · em 필드 없음 {progress.missing.toLocaleString()}건</p></>}
    {note&&<p role="status">{note}</p>}
    {phase==="complete"&&!rows.length&&<p className="schedule-empty">{progress.missing?"em 필드가 없습니다. 새 일정 또는 설정 복사에서 이벤트 생성을 켜세요.":"마지막 검색 구간에 선택한 이벤트가 없습니다."}</p>}
    {!!rows.length&&<div className="table-scroll"><table><thead><tr><th>시각 (KST)</th><th>이벤트</th><th>em</th><th>확인</th></tr></thead><tbody>{rows.map(row=><tr key={row.message.ts} className={picked===row?"is-selected":""}><td>{clock(row.message.ts)}</td><td>{names.filter((_,bit)=>(Number(row.message.pld.em)&(1<<bit))!==0).join(", ")}</td><td>{Number(row.message.pld.em)}</td><td><button disabled={disabled||running} onClick={()=>{setPicked(row);onSelect(row.message.ts);}}>앞뒤 30초 보기</button></td></tr>)}</tbody></table></div>}
    <p className="note">표는 최대100건씩 표시합니다. 이벤트 포함 메시지 수는 독립 사건 수가 아닙니다. 지속 이벤트는 여러 메시지에 나타날 수 있습니다.</p>
    {picked&&<><h4>선택 이벤트 · {clock(picked.message.ts)}</h4><p className="note">개발 모형의 판정 근거입니다. 가감속은 부호로 구분하며 완료 기동은 완료 시각을 포함하는 메시지에 기록됩니다.</p>
      <div className="table-scroll"><table><thead><tr><th>기동</th><th>도로</th><th>시작 → 종료</th><th>속도 (km/h)</th><th>제한속도 (km/h)</th><th>가감속 (m/s²)</th><th>방향 변화 (°)</th><th>횡가속도 (m/s²)</th></tr></thead><tbody>{picked.evidence.map((e,i)=><tr key={i}><td>{e.kind}</td><td>{e.road}</td><td>{clock(e.start_ms)} → {clock(e.end_ms)}</td><td>{e.start_speed_kmh.toFixed(1)} → {e.end_speed_kmh.toFixed(1)}</td><td>{e.speed_limit_kmh.toFixed(1)}</td><td>{e.acceleration_mps2.toFixed(2)}</td><td>{e.angle_deg.toFixed(1)}</td><td>{e.lateral_mps2.toFixed(2)}</td></tr>)}</tbody></table></div>
      {renderMessages([picked.message],vehicle,picked.sequence-1)}</>}
  </section>;
}
