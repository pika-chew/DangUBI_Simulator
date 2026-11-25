/** 운행 집중 시간대 입력·검사(서버 traffic_focus.TrafficFocus와 같은 범위). */
export type FocusWindow={start:string;end:string;target_percent:number;shape:"peak"|"flat";weekdays:number[]};
export type TrafficFocus={windows:FocusWindow[]};
export type FocusWindowReport={index:number;start:string;end:string;shape:string;target_percent:number;baseline_percent:number;
  planned_percent:number;maximum_percent:number;active_vehicle_percent:number;within_tolerance:boolean;reasons:string[]};
export type FocusReport={date:string;vehicles:number;windows:FocusWindowReport[];bins?:{baseline:number[];adjusted:number[]}};

export const weekdayNames=["월","화","수","목","금","토","일"];
export const shapeLabels:Record<string,string>={peak:"봉우리형",flat:"평탄형"};
export const TOLERANCE_PERCENT=5;

export function defaultFocusWindow(existing:FocusWindow[]=[]):FocusWindow {
  const presets:[string,string][]=[["08:20","08:50"],["18:00","18:40"],["12:00","12:30"]];
  const free=presets.find(([s])=>!existing.some(w=>w.start===s))??presets[0];
  return {start:free[0],end:free[1],target_percent:40,shape:"peak",weekdays:[1,2,3,4,5]};
}

export const minuteOf=(text:string)=>{const [h,m]=text.split(":").map(Number);return h*60+m;};

export function focusIssues(focus:TrafficFocus|null|undefined):string[] {
  if(!focus)return [];
  const issues:string[]=[];
  if(focus.windows.length<1||focus.windows.length>3)issues.push("집중 시간대는 1~3개입니다.");
  focus.windows.forEach((w,i)=>{
    const label=`시간대 ${i+1}`;
    if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(w.start)||!/^(([01]\d|2[0-3]):[0-5]\d|24:00)$/.test(w.end)){issues.push(`${label}: 시각을 HH:MM으로 입력하세요.`);return;}
    const a=minuteOf(w.start),b=minuteOf(w.end);
    if(a<240)issues.push(`${label}: 04:00 이후에 시작해야 합니다.`);
    if(b-a<10||b-a>240)issues.push(`${label}: 시작 < 종료이며 10분~4시간입니다.`);
    if(!Number.isInteger(w.target_percent)||w.target_percent<1||w.target_percent>100)issues.push(`${label}: 목표는 1~100%입니다.`);
    if(!w.weekdays.length)issues.push(`${label}: 요일을 하나 이상 고르세요.`);
  });
  const spans=focus.windows.map(w=>[minuteOf(w.start),minuteOf(w.end)]).sort((x,y)=>x[0]-y[0]);
  if(spans.some((s,i)=>i>0&&s[0]<spans[i-1][1]))issues.push("집중 시간대끼리 겹칠 수 없습니다.");
  return issues;
}

/** 10분 칸 번호 → "HH:MM". */
export const binLabel=(index:number)=>`${String(Math.floor(index/6)).padStart(2,"0")}:${String(index%6*10).padStart(2,"0")}`;
