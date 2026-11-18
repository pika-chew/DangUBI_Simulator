import { useState } from "react";

type TimelineTrip = { vehicle_id:string; trip_key:string; start_ms:number; end_ms:number; observed_start_ms:number; observed_end_ms:number };
const clock=(ms:number)=>new Date(ms).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const length=(ms:number)=>{const s=Math.round(ms/1000);return `${Math.floor(s/3600)}시간 ${Math.floor(s%3600/60)}분 ${s%60}초`;};
export function TripTimeline<T extends TimelineTrip>({start,end,trips,selectedKey,vehicle,disabled,onSelect}:{start:number;end:number;trips:T[];selectedKey:string;vehicle:string;disabled:boolean;onSelect:(trip:T)=>void}) {
 const [hover,setHover]=useState<{at:number;trip?:T}|null>(null);
 const [focused,setFocused]=useState<{at:number;trip?:T}|null>(null);
 const active=hover??focused;
 const span=Math.max(1,end-start);
 const day=86400000,kst=9*3600000;
 const days:number[]=[];
 for(let at=Math.floor((start+kst)/day)*day-kst+day;at<end;at+=day) days.push(at);
 const percent=(at:number)=>(at-start)/span*100;
 const hint=(t:T,i:number)=>`운행 ${i+1} · 출발 ${clock(t.start_ms)} → 종료 ${clock(t.end_ms)} · 총주행시간 ${length(t.end_ms-t.start_ms)}`;
 return <div className="trip-timeline-view" key={`${vehicle}:${start}:${end}`}>
  <div className="schedule-timeline-label"><span>■ 운행 · 회색은 주차 · 막대를 선택하면 상세 조회</span><span>{clock(start)} → {clock(end)}</span></div>
  <div className="trip-timeline-scroll"><div className="trip-timeline-scale">
   <div className="timeline-days" aria-hidden="true">{days.map((at,i)=><span key={at} style={{left:`${percent(at)}%`}}>{days.length<=7 || i%7===0 ? new Date(at).toLocaleDateString("ko-KR",{timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit"}):""}</span>)}</div>
   <div className="schedule-timeline" aria-label={`${vehicle} 운행·주차 시간선`}
    onPointerMove={e=>{const r=e.currentTarget.getBoundingClientRect();const at=start+Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))*span;const key=(e.target as HTMLElement).closest("button")?.dataset.tripKey;setHover({at,trip:trips.find(t=>key ? t.trip_key===key : at>=t.observed_start_ms && at<t.observed_end_ms)});}}
    onPointerLeave={()=>setHover(null)}>
    {days.map(at=><span key={at} className="timeline-day-line" style={{left:`${percent(at)}%`}} />)}
    {trips.map((t,i)=><button key={t.trip_key} data-trip-key={t.trip_key} disabled={disabled} className={t.trip_key===selectedKey?"selected":""} style={{left:`${percent(t.observed_start_ms)}%`,width:`${Math.max(.15,(t.observed_end_ms-t.observed_start_ms)/span*100)}%`}} aria-label={hint(t,i)} title={hint(t,i)} onClick={()=>onSelect(t)} onFocus={()=>setFocused({at:t.observed_start_ms,trip:t})} onBlur={()=>setFocused(null)} />)}
    {active && <span className="timeline-cursor" style={{left:`${percent(active.at)}%`}} />}
   </div>
   <div className="timeline-ticks" aria-label="측정 기간 시각 눈금">{[0,.25,.5,.75,1].map(f=><span key={f} style={{left:`${f*100}%`,transform:f===0?"none":f===1?"translateX(-100%)":"translateX(-50%)"}}>{clock(start+span*f)}</span>)}</div>
  </div></div>
  <div className="timeline-readout" role="status" aria-live="off">{active ? <><b>{clock(Math.round(active.at))}</b> · {active.trip ? hint(active.trip,trips.indexOf(active.trip)) : "주차"}</> : "시간선 위에 마우스를 올리면 해당 시각과 운행 정보를 확인할 수 있습니다."}</div>
 </div>;
}
