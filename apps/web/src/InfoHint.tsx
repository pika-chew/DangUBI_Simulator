import {useId} from "react";

export function InfoHint({text,range,label="설정"}:{text:string;range?:string;label?:string}) {
  const id=useId();
  return <span className="hint">
    <button type="button" className="info-button" aria-label={label+" info"} aria-describedby={id} onClick={e=>e.preventDefault()}>i</button>
    <span id={id} className="hint-content" role="tooltip">{text}{range&&<span className="hint-range">입력 범위: {range}</span>}</span>
  </span>;
}
