import test from "node:test";
import assert from "node:assert/strict";
import {transmissionReport} from "../apps/web/src/transmissionReport.ts";

const result=(patch={})=>({run_id:"run-1",state:"mqtt_completed",job_state:"completed",transport:"mqtt",
  planned_messages:100,processed_messages:100,puback_count:100,assignment_count:10,allocated_clients:2,
  elapsed_ms:10000,active_elapsed_ms:8000,paused_ms:2000,unconfirmed_messages:0,
  connection_cleanup_confirmed:true,execution_cleanup_confirmed:true,...patch});

test("average uses whole execution time including pause, not ACK latency or active time",()=>{
  const r=transmissionReport(result(),"2026-11-20T00:00:00Z");
  assert.equal(r.average_puback_per_second,10);
  assert.equal(r.timing.paused_ms,2000);
  assert.equal(r.completed_and_confirmed,true);
});

test("zero or missing elapsed time is unmeasured, not infinity or a guessed rate",()=>{
  for(const elapsed_ms of [undefined,0,NaN,-1]){
    assert.equal(transmissionReport(result({elapsed_ms})).average_puback_per_second,null);
  }
  assert.equal(transmissionReport(result({puback_count:0})).average_puback_per_second,0);
});

test("partial, failed, unconfirmed and uncleaned executions never claim complete",()=>{
  for(const patch of [{puback_count:99},{unconfirmed_messages:1},{unconfirmed_messages:undefined},{execution_cleanup_confirmed:undefined},
    {connection_cleanup_confirmed:false},{errors:["failure"]},{job_state:"running"},{job_state:"failed"},
    {state:"mqtt_failed",job_state:"failed"}]){
    assert.equal(transmissionReport(result(patch)).completed_and_confirmed,false);
  }
});

test("dry run has no MQTT throughput and old records preserve missing measurements",()=>{
  const r=transmissionReport(result({transport:"dry_run",state:"dry_run_completed",puback_count:0}));
  assert.equal(r.average_puback_per_second,null);
  assert.equal(r.completed_and_confirmed,false);
  assert.equal(r.timing.initial_connect_ms,null);
  assert.equal(r.timing.phases_ms,null);
});

test("report exports aggregates without raw payloads, assignments or connection configuration",()=>{
  const source=result({samples:[{message:{pld:{private:"secret"}}}],assignments:[{client_id:"private"}],mqtt:{key:"secret"},
    execution_timing:{preparation_ms:200,replay_ms:9000,cleanup_ms:50}});
  const r=transmissionReport(source);
  const json=JSON.stringify(r);
  assert.equal(json.includes("secret"),false);
  assert.equal(json.includes("private"),false);
  assert.equal(r.timing.phases_ms.cleanup_ms,50);
  assert.equal(source.elapsed_ms,10000);
});
