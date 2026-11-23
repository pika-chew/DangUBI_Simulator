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
  assert.ok(stopExplanation({...base,last_stop:"graceful",stop_reason:"기간 상한 7일 도달"}).includes("건너뜀"));
  const abnormal=stopExplanation({...base,last_stop:"abnormal",stop_reason:"오류로 중단",error:"MQTT 응답 없음"});
  assert.ok(abnormal.includes("MQTT 응답 없음")&&abnormal.includes("중복"));
});
