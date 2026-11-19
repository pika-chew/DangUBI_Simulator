import { useEffect, useState } from "react";

export function EMControl({value,onChange}:{value?:boolean;onChange:(value:boolean)=>void}) {
  const [ready,setReady]=useState(false);
  useEffect(()=>{
    const controller=new AbortController();
    fetch("/api/health",{signal:controller.signal}).then(r=>r.ok?r.json():null)
      .then(data=>setReady(data?.features?.microtrip_em===true)).catch(()=>{});
    return ()=>controller.abort();
  },[]);
  return <label className="check"><span>Microtrip 이벤트(em) 생성</span>
    <input type="checkbox" checked={value??false} disabled={!ready} onChange={e=>onChange(e.target.checked)} />
    <span className="note">{ready?"켜면 주기 내 이상 이벤트 비트마스크를 포함합니다. 끄면 em 필드를 생략하며 주행 패턴은 유지합니다.":"서버·Worker 업데이트 후 사용할 수 있습니다. 실행 중인 작업은 유지됩니다."}</span>
  </label>;
}
