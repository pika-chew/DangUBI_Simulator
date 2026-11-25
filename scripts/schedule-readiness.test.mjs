import test from "node:test";
import assert from "node:assert/strict";
import {scheduleReadiness} from "../apps/web/src/scheduleReadiness.ts";

const health={mqtt_ready:true,mqtt_limits:{execution_profile:"operations",max_clients:25000,max_messages:1e12,max_duration_seconds:2592600,publish_per_second:50},
  execution_profiles:{small_test:{max_duration_ms:604800000,max_messages:7e9,default_clients:1},operations:{max_duration_ms:2592000000,max_messages:1e12,default_clients:500}}};
const plan=(operations=false,seconds=3600,messages=100)=>({vehicle_count:10,planned_messages:messages,
  request:{fleet:{execution_profile:operations?"operations":"small_test",measurement:{start:"2026-11-20T00:00:00+09:00",end:new Date(Date.parse("2026-11-20T00:00:00+09:00")+seconds*1000).toISOString()}}}});

test("long small plans explain both exceeded limits and how to recreate an operating plan",()=>{
  const r=scheduleReadiness(plan(false,604801,7e9+1),"dry_run",1,health);
  assert.equal(r.issues.length,2);
  assert.ok(r.issues.every(i=>i.action.includes("설정 복사")));
});
test("trial inherits the configured MQTT caps and distinguishes fast and basic",()=>{
  const r=scheduleReadiness(plan(false,604800,101),"mqtt",1,health);
  assert.deepEqual(r.mqtt,{clients:25000,messages:1e12,seconds:2592600});
  assert.equal(r.issues.length,0);
  const short={...health,mqtt_limits:{...health.mqtt_limits,max_duration_seconds:30}};
  assert.equal(scheduleReadiness(plan(false,3600,101),"mqtt",1,short).issues.length,1);
  assert.equal(scheduleReadiness(plan(false,3600,101),"mqtt",1,short,false).issues.length,0);
});
test("legacy operating plans are kept viewable but routed to persistent operation",()=>{
  const p=plan(true,7200,12000);
  for(const transport of ["dry_run","mqtt"]){
    const r=scheduleReadiness(p,transport,5,health);
    assert.ok(r.issues.some(i=>i.reason.includes("이전 운영 프로필")&&i.action.includes("상시 운영")));
  }
  assert.ok(scheduleReadiness(plan(false,604801,7e9+1),"dry_run",1,health).issues.every(i=>i.action.includes("상시 운영")));
});
test("exact limits, invalid client counts and missing settings are distinguished",()=>{
  assert.equal(scheduleReadiness(plan(false,3600,10000),"dry_run",10,health).issues.length,0);
  for(const clients of [0,11,1.5])assert.ok(scheduleReadiness(plan(),"dry_run",clients,health).issues.length);
  assert.ok(scheduleReadiness(plan(),"mqtt",1,{...health,mqtt_ready:false,mqtt_limits:null}).issues.some(i=>i.reason.includes("준비되지")));
  assert.doesNotThrow(()=>scheduleReadiness(plan(),"mqtt",1,null));
});
test("manual schedules get a realistic alternative and source plans stay unchanged",()=>{
  const p=plan(false,604801,12000);p.request.schedule=p.request.fleet;delete p.request.fleet;
  const before=JSON.stringify(p),r=scheduleReadiness(p,"dry_run",1,health);
  assert.ok(r.issues.every(i=>i.action.includes("수동 일정")));
  assert.equal(JSON.stringify(p),before);
});
