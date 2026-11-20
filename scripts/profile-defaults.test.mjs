import test from "node:test";
import assert from "node:assert/strict";
import {defaultFleet,profileExecutionDefaults,profileGuide} from "../apps/web/src/fleetSettings.ts";
import {scheduleReadiness} from "../apps/web/src/scheduleReadiness.ts";

const now=Date.parse("2026-11-20T10:00:00+09:00");
test("operating generation defaults preserve the previous complete set of values",()=>{
  const actual=defaultFleet("operations",now);
  assert.deepEqual(actual,{execution_profile:"operations",fleet_version:"fleet-plan-4",vehicle_count:20,
    measurement:{start:"2026-11-20T00:00:00+09:00",end:"2026-11-23T00:00:00+09:00",timezone:"Asia/Seoul"},
    interval_ms:2000,seed:42,trip_min_minutes:3,trip_max_minutes:120,daily_variation_percent:25,
    departure_jitter_minutes:30,day_off_probability:0.2,weekend_activity_probability:0.5,
    anomaly_vehicle_percent:5,anomaly_events_per_hour:2,anomaly_intensity:"medium",
    pattern_mix:{commute:60,daytime:20,flexible:20},road_mix:{urban:35,highway:20,congested:5,mixed:40}});
});
test("small defaults fit a one-hour dry run even if all vehicles continuously produce microtrips",()=>{
  const f=defaultFleet("small_test",now);
  const duration=Date.parse(f.measurement.end)-Date.parse(f.measurement.start);
  const worstMicrotrips=f.vehicle_count*(duration/f.interval_ms+1);
  // 최소 주차30분과 운행 최소3분이므로1시간에 종료Trip은 차량당 최대2개다.
  const upper=worstMicrotrips+2*f.vehicle_count;
  assert.equal(duration,3600000);assert.ok(upper<10000);assert.equal(f.vehicle_count,3);
  assert.equal(f.trip_max_minutes,15);assert.equal(f.day_off_probability,0);assert.equal(f.weekend_activity_probability,1);
  const health={mqtt_ready:false,mqtt_limits:null,execution_profiles:{small_test:{max_duration_ms:3600000,max_messages:10000}}};
  assert.equal(scheduleReadiness({request:{fleet:f},vehicle_count:3,planned_messages:upper},"dry_run",1,health).issues.length,0);
});
test("switching profiles creates isolated defaults and does not mutate the previous values",()=>{
  const small=defaultFleet("small_test",now);small.pattern_mix.commute=12;
  const operations=defaultFleet("operations",Date.parse(small.measurement.start));
  assert.equal(operations.pattern_mix.commute,60);assert.equal(small.pattern_mix.commute,12);
  assert.equal(defaultFleet("small_test",now).pattern_mix.commute,60);
  assert.doesNotThrow(()=>defaultFleet("operations",NaN));
});
test("execution defaults differ by profile without enabling external transmission",()=>{
  assert.equal(profileExecutionDefaults("small_test").realtime,false);
  assert.equal(profileExecutionDefaults("operations").realtime,true);
  for(const p of ["small_test","operations"]){
    assert.equal(profileExecutionDefaults(p).transport,"dry_run");
    assert.equal(profileExecutionDefaults(p).mapping,"shared");assert.equal(profileExecutionDefaults(p).workers,1);
  }
  assert.equal(profileGuide.small_test.defaultClients,1);assert.equal(profileGuide.operations.defaultClients,500);
});
