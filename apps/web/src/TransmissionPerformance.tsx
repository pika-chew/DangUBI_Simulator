import type {Result} from "./SimulationTools";

const labels={passed:"목표 충족",exceeded:"목표 초과",incomplete:"판정 보류",pending:"실행 중",unavailable:"계측 정보 부족",not_applicable:"송신 없음"};

export function TransmissionPerformance({result}:{result:Result}) {
  const goal=result.transmission_goal;
  const percentage=(count:number|undefined)=>goal?.sample_count&&count!==undefined ? `${(count/goal.sample_count*100).toFixed(2)}% · ${count.toLocaleString()}건` : "계측값 없음";
  return <>
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
    ] as const).map(([name,p])=><tr key={name}><td>{name}</td><td>{p.count.toLocaleString()}</td><td>{p.mean_ms.toFixed(3)}</td><td>{p.max_ms.toFixed(3)}</td><td>{p.p95_upper_ms.toFixed(3)}</td><td>{p.p99_upper_ms?.toFixed(3) ?? "이전 기록 · 없음"}</td></tr>)}</tbody></table></div>
    <p className="note">P95·P99 상한은 고정 버킷의 추정값입니다. 위 목표 판정은 상한 대신 실제 1초·2초 이내 계측 건수를 사용합니다. PUBACK 대기는 별도 항목이며 후속 파이프라인 처리 시간은 포함하지 않습니다.</p></>}
  </>;
}
