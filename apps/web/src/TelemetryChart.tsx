import { useRef, useState } from "react";
import type { TracePoint } from "./SimulationTools";

export function SpeedChart({ marker, trace, start, end, departure, disabled, onRange, axis = "clock" }: { marker?:number; trace: TracePoint[]; start: number; end: number; departure: number; disabled: boolean; onRange: (a: number, b: number) => void; axis?: "clock" | "elapsed" }) {
  const label = (ms: number, short=false) => axis === "elapsed" ? `${(ms / 1000).toFixed(1)}초` : new Date(ms).toLocaleString("ko-KR", {timeZone:"Asia/Seoul",...(short?{}:{month:"2-digit" as const,day:"2-digit" as const}),hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
  const anchor = useRef<number | null>(null);
  const [drag, setDrag] = useState<[number, number] | null>(null);
  const span = Math.max(1, end - start), max = Math.max(10, ...trace.map(p => p.speed_kmh));
  const at = (e: React.PointerEvent<SVGSVGElement>) => Math.max(60, Math.min(940, (e.clientX - e.currentTarget.getBoundingClientRect().left) / e.currentTarget.getBoundingClientRect().width * 1000));
  if (!trace.length) return <p className="schedule-empty">선택 구간에 표시할 그래프가 없습니다.</p>;
  return <svg className="drive-chart schedule-chart" viewBox="0 0 1000 240" role="img" aria-label="선택 운행 속도 그래프 · 드래그로 구간 선택"
    onPointerDown={e => { if (disabled || end <= start || e.button !== 0) return; const x = at(e); anchor.current = x; setDrag([x, x]); e.currentTarget.setPointerCapture(e.pointerId); }}
    onPointerMove={e => { if (anchor.current !== null) setDrag([anchor.current, at(e)]); }}
    onPointerCancel={() => { anchor.current = null; setDrag(null); }}
    onLostPointerCapture={() => { anchor.current = null; setDrag(null); }}
    onPointerUp={e => { const from = anchor.current, to = at(e); anchor.current = null; setDrag(null); if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId); if (from === null || Math.abs(to - from) < 4) return; onRange(Math.round(start + (Math.min(from, to) - 60) / 880 * span), Math.round(start + (Math.max(from, to) - 60) / 880 * span)); }}>
    <path d="M60 30 V190 H940 M60 110 H940 M60 40 H940" fill="none" stroke="#c6d4de" />
    <polyline points={trace.map(p => `${60 + (departure + p.elapsed_seconds * 1000 - start) / span * 880},${190 - p.speed_kmh / max * 150}`).join(" ")} fill="none" stroke="#087c70" strokeWidth="3" />
    {trace.length === 1 && <circle cx={60 + (departure + trace[0].elapsed_seconds * 1000 - start) / span * 880} cy={190 - trace[0].speed_kmh / max * 150} r="4" fill="#087c70" />}
    {marker!==undefined && marker>=start && marker<=end && <g><line x1={60+(marker-start)/span*880} x2={60+(marker-start)/span*880} y1="30" y2="190" stroke="#c15b16" strokeWidth="2" strokeDasharray="5 3"/><text x={Math.min(800,60+(marker-start)/span*880+5)} y="25">선택 이벤트</text></g>}
    {drag && <rect x={Math.min(...drag)} y="30" width={Math.abs(drag[1] - drag[0])} height="160" fill="#087c70" fillOpacity="0.18" pointerEvents="none" />}
    <text x="4" y="22">km/h</text><text x="10" y="46">{max.toFixed(0)}</text><text x="30" y="194">0</text>
    {[0,.25,.5,.75,1].map(fraction => <g key={fraction}>
      <path d={`M${60+880*fraction} 190 v6`} fill="none" stroke="#c6d4de" />
      <text x={60+880*fraction} y="223" textAnchor={fraction===0?"start":fraction===1?"end":"middle"}>{label(start+(end-start)*fraction, fraction>0 && fraction<1)}</text>
    </g>)}
  </svg>;
}
