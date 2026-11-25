import {useState} from "react";
import {InfoHint} from "./InfoHint";
import {binLabel,defaultFocusWindow,focusIssues,minuteOf,shapeLabels,TOLERANCE_PERCENT,weekdayNames,
  type FocusReport,type FocusWindow,type TrafficFocus} from "./trafficFocus";

/** 운행 집중 시간대 입력. 상시 운영·시험 실행 공용. */
export function FocusEditor({value,onChange,disabled}:{value:TrafficFocus|null|undefined;onChange:(next:TrafficFocus|null)=>void;disabled?:boolean}) {
  const windows=value?.windows??[];
  const issues=focusIssues(value);
  const patch=(i:number,next:Partial<FocusWindow>)=>onChange({windows:windows.map((w,n)=>n===i?{...w,...next}:w)});
  return <div className="focus-editor">
    <label className="schedule-check"><input type="checkbox" disabled={disabled} checked={!!value} onChange={e=>onChange(e.target.checked?{windows:[defaultFocusWindow()]}:null)}/>
      운행 집중 시간대 사용 <InfoHint label="운행 집중 시간대" text="정한 시간대에 같은 순간 운행하는 차량 비율(구간 평균)이 목표에 가까워지도록, 일부 차량의 운행 하나를 시간대 쪽으로 옮깁니다. 운행 길이·횟수·주차 30분·쉬는 날은 바꾸지 않고 출발·도착은 흩어집니다." range={`시간대 1~3개 · 04:00~24:00 · 10분~4시간 · 목표 1~100% · 허용 ±${TOLERANCE_PERCENT}%p`}/></label>
    {windows.map((w,i)=><fieldset key={i} className="focus-window" disabled={disabled}>
      <legend>시간대 {i+1}</legend>
      <div className="fields">
        <label>시작<input type="time" step={60} value={w.start} onChange={e=>patch(i,{start:e.target.value})}/></label>
        <label>종료<input type="time" step={60} value={w.end==="24:00"?"23:59":w.end} onChange={e=>patch(i,{end:e.target.value})}/></label>
        <label><span className="label-line">목표 동시 운행 (%) <InfoHint label="목표 동시 운행" text="운영 차량 중 이 시간대에 같은 순간 운행하는 차량의 평균 비율입니다." range="1~100%"/></span>
          <input type="number" min={1} max={100} value={w.target_percent} onChange={e=>patch(i,{target_percent:Number(e.target.value)})}/></label>
        <label><span className="label-line">형태 <InfoHint label="형태" text="봉우리형은 가운데가 가장 많고 앞뒤로 줄어듭니다. 평탄형은 구간 내내 비슷합니다."/></span>
          <select value={w.shape} onChange={e=>patch(i,{shape:e.target.value as FocusWindow["shape"]})}>{Object.entries(shapeLabels).map(([k,t])=><option key={k} value={k}>{t}</option>)}</select></label>
      </div>
      <div className="schedule-weekdays" role="group" aria-label={`시간대 ${i+1} 적용 요일`}>{weekdayNames.map((name,d)=><label key={name}><input type="checkbox" checked={w.weekdays.includes(d+1)}
        onChange={e=>patch(i,{weekdays:e.target.checked?[...w.weekdays,d+1].sort((a,b)=>a-b):w.weekdays.filter(x=>x!==d+1)})}/>{name}</label>)}</div>
      <button type="button" onClick={()=>onChange(windows.length>1?{windows:windows.filter((_,n)=>n!==i)}:null)}>시간대 {i+1} 삭제</button>
    </fieldset>)}
    {value&&<button type="button" disabled={disabled||windows.length>=3} onClick={()=>onChange({windows:[...windows,defaultFocusWindow(windows)]})}>시간대 추가</button>}
    {issues.length>0&&<ul className="note">{issues.map(x=><li key={x}>{x}</li>)}</ul>}
  </div>;
}

/** 집중 결과 보고: 시간대별 표 + 10분 단위 동시 운행 비교 그래프(기본 vs 집중 적용). */
export function FocusReportView({report}:{report:FocusReport}) {
  return <div className="focus-report">
    <div className="table-scroll"><table><thead><tr><th>시간대</th><th>목표</th><th>기본 일정</th><th>집중 적용(계획)</th><th>가능 최대</th><th>그날 운행 차량</th><th>판정</th></tr></thead>
      <tbody>{report.windows.map(w=><tr key={w.index}><td>{w.start}~{w.end} · {shapeLabels[w.shape]??w.shape}</td><td>{w.target_percent}%</td><td>{w.baseline_percent}%</td><td><b>{w.planned_percent}%</b></td>
        <td>{w.maximum_percent}%</td><td>{w.active_vehicle_percent}%</td><td>{w.within_tolerance?`달성(±${TOLERANCE_PERCENT}%p)`:"미달"}{w.reasons.length>0&&<ul className="note">{w.reasons.map(r=><li key={r}>{r}</li>)}</ul>}</td></tr>)}</tbody></table></div>
    {report.bins&&<FocusChart report={report}/>}
  </div>;
}

function FocusChart({report}:{report:FocusReport}) {
  const bins=report.bins!;
  const [hover,setHover]=useState<number|null>(null);
  const first=24,last=144; // 04:00~24:00
  const max=Math.max(10,...bins.adjusted.slice(first),...bins.baseline.slice(first),...report.windows.map(w=>w.target_percent));
  const top=Math.ceil(max/10)*10;
  const x=(i:number)=>60+(i-first)/(last-first)*880, y=(v:number)=>190-v/top*160;
  const line=(values:number[])=>values.slice(first).map((v,k)=>`${x(first+k+.5).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const ticks=[0,top/2,top];
  return <div className="focus-chart">
    <div className="focus-legend" aria-hidden="true"><span><i className="swatch baseline"/>기본 일정</span><span><i className="swatch adjusted"/>집중 적용(계획)</span><span><i className="swatch window"/>집중 시간대</span></div>
    <svg viewBox="0 0 1000 230" role="img" aria-label={`${report.date} 10분 단위 동시 운행 비율 · 기본 일정과 집중 적용 비교`}
      onPointerMove={e=>{const r=e.currentTarget.getBoundingClientRect();const px=(e.clientX-r.left)/r.width*1000;setHover(Math.max(first,Math.min(last-1,Math.floor(first+(px-60)/880*(last-first)))));}}
      onPointerLeave={()=>setHover(null)}>
      {report.windows.map(w=>{const a=minuteOf(w.start)/10,b=minuteOf(w.end)/10;return <g key={w.index}>
        <rect className="focus-band" x={x(a)} y={30} width={x(b)-x(a)} height={160}/>
        <line className="focus-target" x1={x(a)} x2={x(b)} y1={y(w.target_percent)} y2={y(w.target_percent)}/>
        <text className="focus-label" x={x(b)+6} y={y(w.target_percent)+4}>목표 {w.target_percent}%</text></g>;})}
      {ticks.map(t=><g key={t}><line className="focus-grid" x1={60} x2={940} y1={y(t)} y2={y(t)}/><text x={52} y={y(t)+5} textAnchor="end">{t}%</text></g>)}
      {[24,48,72,96,120,143].map(i=><text key={i} x={x(i)} y={212} textAnchor="middle">{binLabel(i===143?144:i).replace("24:00","24:00")}</text>)}
      <polyline className="focus-line baseline" points={line(bins.baseline)}/>
      <polyline className="focus-line adjusted" points={line(bins.adjusted)}/>
      {hover!==null&&<g pointerEvents="none"><line className="focus-cursor" x1={x(hover+.5)} x2={x(hover+.5)} y1={30} y2={190}/>
        <circle className="focus-dot baseline" cx={x(hover+.5)} cy={y(bins.baseline[hover])} r={4}/><circle className="focus-dot adjusted" cx={x(hover+.5)} cy={y(bins.adjusted[hover])} r={4}/></g>}
    </svg>
    <p className="timeline-readout" role="status" aria-live="off">{hover!==null?<><b>{binLabel(hover)}~{binLabel(hover+1)}</b> · 기본 {bins.baseline[hover].toFixed(1)}% · 집중 적용 {bins.adjusted[hover].toFixed(1)}%</>:"그래프 위에 마우스를 올리면 10분 칸의 동시 운행 비율을 확인할 수 있습니다."}</p>
    <details><summary>10분 단위 표 보기 (04:00~24:00)</summary><div className="table-scroll"><table><thead><tr><th>시각 (KST)</th><th>기본 일정 (%)</th><th>집중 적용 (%)</th></tr></thead>
      <tbody>{bins.adjusted.slice(first).map((v,k)=><tr key={k}><td>{binLabel(first+k)}~{binLabel(first+k+1)}</td><td>{bins.baseline[first+k].toFixed(1)}</td><td>{v.toFixed(1)}</td></tr>)}</tbody></table></div></details>
  </div>;
}

/** 시험 일정의 날짜별 집중 결과(날짜 선택). */
export function FocusDays({reports}:{reports:FocusReport[]}) {
  const [day,setDay]=useState(reports[0]?.date??"");
  const report=reports.find(r=>r.date===day)??reports[0];
  if(!report)return null;
  return <details className="schedule-advanced" open><summary>운행 집중 시간대 결과</summary>
    <label>날짜<select value={report.date} onChange={e=>setDay(e.target.value)}>{reports.map(r=><option key={r.date} value={r.date}>{r.date}{r.windows.length?"":" · 적용 요일 아님"}</option>)}</select></label>
    {report.windows.length?<FocusReportView report={report}/>:<p className="note">이 날짜는 적용 요일이 아니라 기본 일정을 그대로 씁니다.</p>}
    <p className="note">일정 전체 {report.vehicles.toLocaleString()}대 기준 계획값입니다. 조정한 일정이며 차량의 운행 길이·횟수·쉬는 날은 바꾸지 않습니다.</p>
  </details>;
}
