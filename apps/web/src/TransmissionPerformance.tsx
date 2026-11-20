import type {Result} from "./SimulationTools";
import {InfoHint} from "./InfoHint";
import {transmissionReport} from "./transmissionReport";

const labels={passed:"목표 충족",exceeded:"목표 초과",incomplete:"판정 보류",pending:"실행 중",unavailable:"계측 정보 부족",not_applicable:"송신 없음"};

export function TransmissionPerformance({result}:{result:Result}) {
  const goal=result.transmission_goal;
  const report=transmissionReport(result);
  const seconds=(value:number|null|undefined)=>value!==null&&value!==undefined&&Number.isFinite(value)&&value>=0?`${(value/1000).toLocaleString(undefined,{maximumFractionDigits:3})}초`:"기록 없음";
  const download=()=>{
    const url=URL.createObjectURL(new Blob([JSON.stringify(transmissionReport(result),null,2)],{type:"application/json"}));
    const link=document.createElement("a");link.href=url;
    link.download=`transmission-${result.run_id.replace(/[^a-zA-Z0-9_-]/g,"_").slice(0,80)}.json`;
    document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  const percentage=(count:number|undefined)=>goal?.sample_count&&count!==undefined ? `${(count/goal.sample_count*100).toFixed(2)}% · ${count.toLocaleString()}건` : "계측값 없음";
  return <>
    {report.transport==="mqtt"&&<>
      <div className="panel-heading"><h3>전송 측정</h3><button type="button" onClick={download}>측정 결과 JSON 저장</button></div>
      <p className="note">{report.completed_and_confirmed?"전체 전송·정리 확인 완료":"전체 완료 확인 전 · 진행·부분 결과 또는 완료 확인 정보가 부족한 이전 기록입니다."} · 현재 화면에 반영된 집계를 저장합니다.</p>
      <div className="metric-grid">
        <div><span>실행 전체 시간 <InfoHint label="실행 전체 시간" text="실행 준비부터 현재 또는 종료까지의 실제 경과 시간입니다. 일시정지와 정리도 포함합니다."/></span><b>{seconds(report.timing.elapsed_ms)}</b></div>
        <div><span>평균 PUBACK 처리량 <InfoHint label="평균 PUBACK 처리량" text="PUBACK 건수를 실행 전체 시간으로 나눈 값입니다. 대기·일시정지를 포함하며 최대 처리량이 아닙니다."/></span><b>{report.average_puback_per_second===null?"기록 없음":`${report.average_puback_per_second.toLocaleString(undefined,{maximumFractionDigits:2})}건/초`}</b></div>
        <div><span>초기 전체 접속 <InfoHint label="초기 전체 접속" text="송신 Worker 시작부터 모든 Client의 최초 접속 확인까지입니다. 운행 연동 연결에서는 기록이 없을 수 있습니다."/></span><b>{seconds(report.timing.initial_connect_ms)}</b></div>
      </div>
      <details><summary>실행 단계별 시간</summary><div className="table-scroll"><table><thead><tr><th>단계</th><th>시간</th><th>측정 범위</th></tr></thead><tbody>
        {([
          ["준비",result.execution_timing?.preparation_ms,"생성·송신 세션 준비와 초기 연결 명령 등록"],
          ["생성·전송",result.execution_timing?.replay_ms,"일정을 따라 생성·검사·전송하며 예정 시각 대기 포함"],
          ["측정 종료 대기",result.execution_timing?.measurement_wait_ms,"마지막 운행 이후 측정 종료 시각까지 진행"],
          ["남은 송신 대기",result.execution_timing?.drain_ms,"남은 송신 명령의 완료를 기다리는 구간"],
          ["정리",result.execution_timing?.cleanup_ms,"연결·생성 Worker 등 실행 자원 정리"],
        ] as const).map(([name,value,scope])=><tr key={name}><td>{name}</td><td>{seconds(value)}</td><td>{scope}</td></tr>)}
      </tbody></table></div><p className="note">단계 시간은 실제 경과 시간이며 일시정지를 포함할 수 있습니다. 초기 전체 접속은 별도 기준으로 측정하므로 단계 시간에 더하지 않습니다. 실행 중·이전 기록의 없는 항목은 기록 없음으로 표시합니다.</p></details>
      <p className="context-note">발행부터 PUBACK까지의 응답 시간은 아래 표에서 확인합니다. 브로커 수신 확인까지이며 이후 DB·S3 저장 시간은 측정하지 않습니다.</p>
    </>}
    <h3>이번 실행의 발행 지연 목표</h3>
    <p className="note">{goal ? <><b>{labels[goal.status]}</b> · {goal.reason}</> : "이전 기록에는 목표 판정 정보가 없습니다."}</p>
    {goal?.sample_count!==undefined&&<div className="table-scroll"><table><thead><tr><th>측정 항목</th><th>결과</th><th>기준</th></tr></thead><tbody>
      <tr><td>1초 이내 발행</td><td>{percentage(goal.within_1s_count)}</td><td>95% 이상</td></tr>
      <tr><td>2초 이내 발행</td><td>{percentage(goal.within_2s_count)}</td><td>99% 이상</td></tr>
      <tr><td>5초 초과 발행</td><td>{goal.over_5s_count?.toLocaleString() ?? "계측값 없음"}건</td><td>개별 지연 경고</td></tr>
    </tbody></table></div>}
    <p className="context-note">예정 시각부터 MQTT 발행 호출까지의 전체 실행을 평가하며 초기 접속·재접속 대기도 포함합니다. 중단·실패·미전송·응답 미확인이 있으면 통과로 판정하지 않습니다. 다른 차량 규모나 장기 안정성을 보장하는 결과는 아닙니다.</p>
    {result.performance&&<><div className="table-scroll"><table><thead><tr><th>측정 항목</th><th>횟수</th><th>평균 ms</th><th>최대 ms</th><th>P95 상한 ms</th><th>P99 상한 ms</th></tr></thead><tbody>{([
      ['생성·검사',result.performance.generation],['예정 시각 대비 지연',result.performance.schedule],['접속 응답',result.performance.connect],['발행·PUBACK',result.performance.ack],
    ] as const).map(([name,p])=><tr key={name}><td>{name}</td><td>{p.count.toLocaleString()}</td><td>{p.count?p.mean_ms.toFixed(3):"계측 없음"}</td><td>{p.count?p.max_ms.toFixed(3):"계측 없음"}</td><td>{p.count?p.p95_upper_ms.toFixed(3):"계측 없음"}</td><td>{p.count?(p.p99_upper_ms?.toFixed(3) ?? "이전 기록 · 없음"):"계측 없음"}</td></tr>)}</tbody></table></div>
    <p className="note">P95·P99 상한은 고정 버킷의 추정값입니다. 위 목표 판정은 상한 대신 실제 1초·2초 이내 계측 건수를 사용합니다. 발행·PUBACK 시간에는 응답 미확인으로 끝난 시도의 대기도 포함됩니다. 후속 파이프라인 처리 시간은 포함하지 않습니다.</p></>}
  </>;
}
