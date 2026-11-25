import test from "node:test";
import assert from "node:assert/strict";
import {defaultOperationSettings,defaultRegister,operationState,registerIssues,settingIssues,stopExplanation} from "../apps/web/src/operationsView.ts";

test("operation settings follow server ranges",()=>{
  assert.deepEqual(settingIssues(defaultOperationSettings),[]);
  assert.equal(defaultOperationSettings.duration_cap_days,7);
  for(const patch of [{interval_ms:500},{interval_ms:1500.5},{client_count:0},{duration_cap_days:31},{generation_workers:3},{day_off_probability:1.2}])
    assert.equal(settingIssues({...defaultOperationSettings,...patch}).length,1,JSON.stringify(patch));
});
test("registration checks mixes, trip range and remaining capacity",()=>{
  assert.deepEqual(registerIssues(defaultRegister,25000),[]);
  assert.ok(registerIssues({...defaultRegister,count:101},100)[0].includes("100대"));
  assert.equal(registerIssues({...defaultRegister,pattern_mix:{commute:50,daytime:20,flexible:20}},25000).length,1);
  assert.equal(registerIssues({...defaultRegister,road_mix:{urban:50,highway:20,congested:10,mixed:10}},25000).length,1);
  assert.equal(registerIssues({...defaultRegister,trip_min_minutes:30,trip_max_minutes:10},25000).length,1);
});
test("state and stop explanations distinguish graceful and abnormal stops",()=>{
  const base={state:"stopped",running:false,settings:null,last_stop:null,stop_reason:null,error:null,virtual_checkpoint_ms:null,session_started_at:null,live:{},registry:null};
  assert.equal(operationState(null),"확인 중");
  assert.equal(operationState({...base,running:true,live:{stopping:true}}),"정상 중단 중 · 진행 운행 마무리");
  assert.equal(stopExplanation(base),"");
  assert.ok(stopExplanation({...base,last_stop:"graceful",stop_reason:"기간 상한 7일 도달"}).includes("저장된 가상시각"));
  const abnormal=stopExplanation({...base,last_stop:"abnormal",stop_reason:"오류로 중단",error:"MQTT 응답 없음"});
  assert.ok(abnormal.includes("MQTT 응답 없음")&&abnormal.includes("중복"));
});
test("vehicle history range uses KST days and the 31-day server limit",async()=>{
  const {dayRange,kstDay}=await import("../apps/web/src/operationsView.ts");
  const range=dayRange("2026-11-24","2026-11-30");
  assert.equal(range.from,Date.parse("2026-11-23T15:00:00Z"));
  assert.equal(range.to-range.from,7*86400000);
  assert.equal(kstDay(range.to-1),"2026-11-30");
  assert.equal(kstDay(Date.parse("2026-11-24T15:30:00Z")),"2026-11-25");
  assert.equal(dayRange("2026-11-30","2026-11-24"),null);
  assert.ok(dayRange("2026-11-01","2026-12-01"));
  assert.equal(dayRange("2026-11-01","2026-12-02"),null);
  assert.equal(dayRange("","2026-11-24"),null);
});
test("recovery diff names fields that differ from the interrupted trip's settings",async()=>{
  const {recoveryDiff,defaultOperationSettings:d}=await import("../apps/web/src/operationsView.ts");
  assert.deepEqual(recoveryDiff(d,null),[]);
  assert.deepEqual(recoveryDiff(d,{...d}),[]);
  assert.deepEqual(recoveryDiff({...d,vehicle_count:1000},d),["운영 차량 수"]);
  assert.deepEqual(recoveryDiff({...d,interval_ms:1000,transport:"mqtt"},d),["전송 방식","Microtrip 주기"]);
});
test("traffic focus input checks mirror the server ranges",async()=>{
  const {focusIssues,defaultFocusWindow,binLabel}=await import("../apps/web/src/trafficFocus.ts");
  const w=defaultFocusWindow();
  assert.deepEqual(focusIssues({windows:[w]}),[]);
  assert.deepEqual(focusIssues(null),[]);
  assert.equal(focusIssues({windows:[{...w,start:"03:00",end:"03:30"}]}).length,1);
  assert.equal(focusIssues({windows:[{...w,end:"08:25"}]}).length,1);
  assert.equal(focusIssues({windows:[{...w,target_percent:0}]}).length,1);
  assert.equal(focusIssues({windows:[{...w,weekdays:[]}]}).length,1);
  assert.ok(focusIssues({windows:[w,{...w}]}).some(x=>x.includes("겹칠")));
  assert.equal(focusIssues({windows:[w,defaultFocusWindow([w]),defaultFocusWindow([w,defaultFocusWindow([w])]),{...w,start:"21:00",end:"21:30"}]}).length,1);
  assert.equal(defaultFocusWindow([w]).start,"18:00");
  assert.equal(binLabel(50),"08:20");
  const {recoveryDiff,defaultOperationSettings:d,settingIssues}=await import("../apps/web/src/operationsView.ts");
  const focused={...d,traffic_focus:{windows:[w]}};
  assert.deepEqual(recoveryDiff(focused,JSON.parse(JSON.stringify(focused))),[]);  // 객체는 내용으로 비교
  assert.deepEqual(recoveryDiff(focused,d),["운행 집중 시간대"]);
  assert.equal(settingIssues({...d,traffic_focus:{windows:[{...w,target_percent:150}]}}).length,1);
});
