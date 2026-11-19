import test from "node:test";
import assert from "node:assert/strict";
import {readJSONResponse} from "../apps/web/src/http.ts";
import {runView} from "../apps/web/src/runView.ts";
import {currentFleet} from "../apps/web/src/fleetSettings.ts";

test("HTTP plain text errors do not expose a JSON parser error",async()=>{
  await assert.rejects(readJSONResponse(new Response("Internal Server Error",{status:500})),/HTTP 500/);
  await assert.rejects(readJSONResponse(new Response("<html>proxy error</html>",{status:502})),/HTTP 502/);
  await assert.rejects(readJSONResponse(new Response("not-json",{status:200})),/JSON 형식/);
});

test("JSON detail errors and valid JSON remain usable",async()=>{
  await assert.rejects(readJSONResponse(Response.json({detail:"원본 일정에서 조회하세요."},{status:409})),/원본 일정/);
  await assert.rejects(readJSONResponse(Response.json({detail:["입력 확인",{msg:"범위 확인"}]},{status:422})),/입력 확인\n범위 확인/);
  assert.deepEqual(await readJSONResponse(Response.json({records:[]})),{records:[]});
});

test("schedule Worker settings never select the single journey trace",()=>{
  assert.equal(runView({source_plan_key:"plan:one",generation:{workers:4,queue_messages:2000}}),"schedule");
  assert.equal(runView({schedule_plan:{fleet:{}},generation:{duration_seconds:60}}),"schedule");
  assert.equal(runView({generation:{workers:4}}),"samples");
  assert.equal(runView({generation:{duration_seconds:60}}),"single_journey");
  assert.equal(runView({generation:null}),"samples");
});

test("copied older settings use current time allocation without changing original",()=>{
  const original={fleet_version:"fleet-plan-1",daily_driving_minutes:120,vehicle_variation_percent:20,vehicle_count:3};
  const current=currentFleet(original);
  assert.equal(current.fleet_version,"fleet-plan-4");
  assert.equal(current.trip_min_minutes,3);assert.equal(current.trip_max_minutes,120);
  assert.equal(current.daily_driving_minutes,undefined);assert.equal(current.vehicle_variation_percent,undefined);
  assert.equal(original.daily_driving_minutes,120);assert.equal(original.fleet_version,"fleet-plan-1");
  assert.equal(currentFleet({...original,trip_max_minutes:90}).trip_max_minutes,90);
});
