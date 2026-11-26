import {InfoHint} from "./InfoHint";
import { useEffect, useRef, useState } from "react";
import { SpeedChart } from "./TelemetryChart";
import { readJSONResponse } from "./http";

type FieldSchema = {
  type: string;
  title: string;
  default: unknown;
  minimum?: number;
  maximum?: number;
};
export type Message = { vehicleId?:string; ty: number; ts: number; pld: Record<string, unknown> };
type Profile = {
  title: string;
  example: Message;
  schema: {
    properties: {
      pld: { properties: Record<string, FieldSchema>; required: string[] };
    };
  };
};
export type Catalog = {
  profile_version: string;
  status: string;
  profiles: Record<string, Profile>;
};
export type Config = {
  publish_mode?: "serial" | "parallel";
  max_in_flight?: number;
  queue_capacity?: number;
  vehicle_count: number;
  mapping_mode: "shared" | "dedicated";
  client_count: number;
  messages_per_vehicle: number;
  interval_ms: number;
  connection_lifetime: "always" | "while_driving";
};
type Assignment = { vehicle_id: string; client_id: string; vin?: string };
export type Generation = {
  include_em?: boolean;
  profile: "urban" | "highway" | "congested" | "mixed";
  model_version?: string;
  weather?: "dry" | "rain";
  anomaly_intensity?: "low" | "medium" | "high";
  engine_start?: "cold" | "warm";
  anomaly_vehicle_percent?: number;
  anomaly_events_per_hour?: number;
  seed: number;
  start_time_ms: number;
  duration_seconds: number;
  start_lat: number;
  start_lon: number;
  initial_odometer_m: number;
};
export type TracePoint = {
  elapsed_seconds: number;
  state: string;
  speed_kmh: number;
  rpm: number;
  distance_m: number;
  context?: {
    road: string;
    reason: string;
    speed_limit_kmh: number;
    grade_percent: number;
    acceleration_mps2: number;
    lateral_mps2: number;
    heading_deg: number;
    event?: string;
    eligible: boolean;
    driver_style: string;
    weather: string;
    gear?: number;
    engine_state?: string;
    wheel_power_kw?: number;
    fuel_rate_lph?: number;
    coolant_c?: number;
  };
};
export type Sample = Assignment & {
  synthetic_trip?:boolean;
  sequence: number;
  topic: string;
  message: Message;
};
type TimingSummary = {
  count: number;
  mean_ms: number;
  max_ms: number;
  p95_upper_ms: number;
  p99_upper_ms?: number;
  within_1s_count?: number;
  within_2s_count?: number;
  over_5s_count?: number;
};
export type Result = {
  recovery_available?: boolean;
  recovery_confirmed_before?: number;
  resent_messages?: number;
  auto_recovery?: {attempts?:number;without_progress?:number;next_at?:number|null;last_at?:string;last_error?:string|null;gave_up?:boolean;history?:{at:string;state:string;cause:string;confirmed:number}[]};
  initial_connect_ms?: number;
  execution_timing?: {
    preparation_ms?:number;replay_ms?:number;measurement_wait_ms?:number;drain_ms?:number;cleanup_ms?:number;
  };
  transmission_goal?: {
    status: "passed" | "exceeded" | "incomplete" | "pending" | "unavailable" | "not_applicable";
    reason: string;
    sample_count?: number;
    within_1s_count?: number;
    within_2s_count?: number;
    over_5s_count?: number;
  };
  buffer_budget?: {
    max_messages:number;max_bytes:number;reserved_messages:number;reserved_bytes:number;
    peak_reserved_messages:number;peak_reserved_bytes:number;
    generation_reserved_messages:number;generation_reserved_bytes:number;
    transmission_reserved_commands:number;transmission_reserved_bytes:number;
  };
  execution_cleanup_confirmed?:boolean;
  transmission?: {
    per_client_in_flight?: number;
    queue_byte_capacity?: number;
    reserved_bytes?: number;
    peak_reserved_bytes?: number;
    reserved_commands?: number;
    peak_reserved_commands?: number;
    mode: string;
    max_in_flight: number;
    queue_capacity: number;
    queued_messages: number;
    in_flight: number;
    peak_queued: number;
    peak_in_flight: number;
    backpressure_count: number;
    discarded_queued: number;
  };
  performance?: {
    generation: TimingSummary;
    schedule: TimingSummary;
    connect: TimingSummary;
    ack: TimingSummary;
    ack_per_second: number;
  };
  run_id: string;
  transport?: string;
  errors?: string[];
  publish_attempts?: number;
  unconfirmed_messages?: number;
  reconnect_count?: number;
  connection_count?:number;
  disconnect_count?:number;
  active_clients?: number;
  state: string;
  allocated_clients: number;
  assignment_count: number;
  assignments: Assignment[];
  planned_messages: number;
  processed_messages: number;
  payload_bytes: number;
  connected_clients: number;
  mqtt_published: number;
  puback_count: number;
  samples: Sample[];
  trip_summaries?: Sample[];
  trace?: TracePoint[];
  scenario?: {
    eligible_vehicles: number;
    observed_vehicles: number;
    events: number;
  };
  model_version?: string;
  microtrip_count?: number;
  trip_count?: number;
  job_state?: string;
  elapsed_ms?: number;
  remaining_messages?: number;
  unsent_messages?: number;
  schedule_delay_ms?: number;
  connection_cleanup_confirmed?: boolean;
  forced_termination?: boolean;
  paused_ms?: number;
  active_elapsed_ms?: number;
  simulation_time_ms?: number;
  wall_limit_seconds?: number;
};
export type SavedRun = {
  id: string;
  created_at: string;
  request: {
    config: Config;
    message?: Message;
    generation?: Generation;
    realtime?: boolean;
    source_plan_key?:string;
    schedule_plan?:unknown;
    transport?: "dry_run" | "mqtt";
  };
  result: Result;
};
export async function api<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return readJSONResponse<T>(response);
}
export const activeStates = new Set([
  "starting",
  "running",
  "pausing",
  "paused",
  "resuming",
  "stopping",
]);
export function stateLabel(result: Result): string {
  const labels: Record<string, string> = {
    starting: "실행 준비 중",
    running: "실행 중",
    pausing: "일시중지 요청 중",
    paused: "일시중지됨",
    resuming: "재개 요청 중",
    stopping: "중단 처리 중",
    stopped: "중단 완료 · 부분 결과",
    failed: "실행 실패 · 결과 확인 필요",
    interrupted: "이전 실행 종료 미확인",
  };
  const auto = result.auto_recovery;
  if ((result.job_state === "failed" || result.job_state === "interrupted") && auto) {
    if (auto.gave_up) return "자동 이어가기 중지 · 확인 필요";
    if (auto.next_at) return `자동 이어가기 대기 · ${new Date(auto.next_at * 1000).toLocaleTimeString("ko-KR", {timeZone: "Asia/Seoul", hour12: false})} 예정`;
  }
  return (
    labels[result.job_state || ""] ||
    (result.state === "mqtt_completed"
      ? "MQTT 송신 완료"
      : result.state === "mqtt_failed"
        ? "MQTT 송신 실패·부분 완료"
        : "송신 없는 생성 확인 완료")
  );
}
export function DrivingResult({
  result,
  catalog,
}: {
  result: Result;
  catalog: Catalog;
}) {
  const [vehicle, setVehicle] = useState(1);
  const [shownVehicle, setShownVehicle] = useState(1);
  const [customTrace, setCustomTrace] = useState<TracePoint[] | null>(null);
  const [start, setStart] = useState(0),
    [end, setEnd] = useState("");
  const [queryBusy, setQueryBusy] = useState(false),
    [queryError, setQueryError] = useState("");
  const [intervalSamples, setIntervalSamples] = useState<Sample[]>([]);
  const [messageTotal, setMessageTotal] = useState(0);
  const [messageOffset, setMessageOffset] = useState(0);
  const [loadedRange, setLoadedRange] = useState({
    start: 0,
    end: "",
    vehicle: 1,
  });
  const [hasQuery, setHasQuery] = useState(false);
  const requestID = useRef(0);
  async function query(
    rangeStart = start,
    rangeEnd = end,
    selectedVehicle = vehicle,
    offset = 0,
  ) {
    const id = ++requestID.current;
    setQueryBusy(true);
    setQueryError("");
    try {
      const suffix =
        "?start_seconds=" +
        rangeStart +
        (rangeEnd !== "" ? "&end_seconds=" + rangeEnd : "") +
        "&message_offset=" +
        offset;
      const data = await api<{
        trace: TracePoint[];
        samples: Sample[];
        message_total: number;
        message_offset: number;
        range_end_seconds: number;
      }>("/api/runs/" + result.run_id + "/trace/" + selectedVehicle + suffix);
      if (id !== requestID.current) return;
      setCustomTrace(data.trace);
      setShownVehicle(selectedVehicle);
      setIntervalSamples(data.samples);
      setMessageTotal(data.message_total);
      setMessageOffset(data.message_offset);
      setLoadedRange({
        start: rangeStart,
        end: String(data.range_end_seconds),
        vehicle: selectedVehicle,
      });
      setHasQuery(true);
      setStart(rangeStart);
      setEnd(rangeEnd);
      setVehicle(selectedVehicle);
    } catch (e) {
      if (id === requestID.current)
        setQueryError(e instanceof Error ? e.message : "조회 실패");
    } finally {
      if (id === requestID.current) setQueryBusy(false);
    }
  }
  useEffect(() => {
    if (
      ![
        "starting",
        "running",
        "pausing",
        "paused",
        "resuming",
        "stopping",
      ].includes(result.job_state || "")
    )
      void query(0, "", 1);
    return () => {
      requestID.current++;
    };
  }, [result.run_id]);
  const trace = customTrace ?? result.trace ?? [];
  return (
    <>
      <h3>차량별 속도·상태 변화</h3>
      <div className="fields">
        <label>
          조회 차량 번호
          <input
            type="number"
            min={1}
            max={result.assignment_count}
            value={vehicle}
            onChange={(e) => setVehicle(Number(e.target.value))}
          />
        </label>
        <label>
          시작 구간 (초)
          <input
            type="number"
            min={0}
            value={start}
            onChange={(e) => setStart(Number(e.target.value))}
          />
        </label>
        <label>
          종료 구간 (초·빈 값은 생성 구간 끝)
          <input
            type="number"
            min={0}
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
      </div>
      <button disabled={queryBusy} onClick={() => void query()}>
        선택 차량 조회
      </button>
      <button
        disabled={queryBusy}
        onClick={() => void query(0, "", shownVehicle)}
      >
        전체 생성 구간 보기
      </button>
      <p className="note">
        그래프의 원하는 구간을 좌우로 드래그하면 그래프·상황표·Microtrip 조회가
        함께 바뀝니다. 시작·종료 입력으로도 조회할 수 있습니다.
      </p>
      {queryBusy && (
        <p className="note" role="status">
          선택 구간을 조회하고 있습니다.
        </p>
      )}
      {queryError && (
        <p role="alert" className="error">
          {queryError}
        </p>
      )}
      {!trace.length && (
        <p className="note">조회 범위에 생성 메시지가 없습니다.</p>
      )}
      <p className="note">
        v{String(shownVehicle).padStart(6, "0")} · 생성 모델{" "}
        {result.model_version} · 최대 120점 요약입니다. 선택 조회는 저장
        설정·seed·모델 버전으로 재생성하며 전송 수신 기록이 아닙니다. 짧은
        이벤트는 생략될 수 있어 구간을 좁혀 확인하세요.
      </p>
      <SpeedChart trace={trace} start={(hasQuery ? loadedRange.start : trace[0]?.elapsed_seconds || 0) * 1000} end={(hasQuery ? Number(loadedRange.end) : trace.at(-1)?.elapsed_seconds || 0) * 1000} departure={0} disabled={queryBusy} axis="elapsed" onRange={(a,b) => void query(a/1000,String(b/1000),shownVehicle)} />
      <div className="table-scroll">
        <table className="drive-trace">
          <thead>
            <tr>
              <th>경과 (초)</th>
              <th>상태</th>
              <th>속도 (km/h)</th>
              <th>운행 누적거리 (km)</th>
              <th>엔진 상태·기어</th>
              <th>도로·상황</th>
              <th>운전자·노면</th>
              <th>가속/횡가속 (m/s²)</th>
              <th>내부 이벤트</th>
            </tr>
          </thead>
          <tbody>
            {trace.map((p) => (
              <tr key={p.elapsed_seconds}>
                <td>{p.elapsed_seconds}</td>
                <td>{p.state}</td>
                <td>{p.speed_kmh.toFixed(1)}</td>
                <td>{(p.distance_m/1000).toFixed(3)}</td>
                <td>
                  {p.context?.engine_state
                    ? p.context.engine_state +
                      " · " +
                      (p.context.gear ? p.context.gear + "단" : "—")
                    : "이전 모델"}
                </td>
                <td>
                  {p.context
                    ? p.context.road +
                      " · " +
                      p.context.reason +
                      " · 경사 " +
                      p.context.grade_percent.toFixed(1) +
                      "%"
                    : "이전 모델"}
                </td>
                <td>
                  {p.context
                    ? p.context.driver_style + " · " + p.context.weather
                    : "—"}
                </td>
                <td>
                  {p.context
                    ? p.context.acceleration_mps2.toFixed(2) +
                      " / " +
                      p.context.lateral_mps2.toFixed(2)
                    : "—"}
                </td>
                <td>{p.context?.event || "없음"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3>선택 구간 Microtrip</h3>
      <p className="note">
        조회 차량·시작·종료 구간에 해당하는 원래 생성 주기의 Microtrip을
        페이지당 최대 100건씩 재생성합니다. 그래프의 120점과 별개이며 DB에 추가
        저장하지 않습니다. 당시 전송 원문·MQTT 수신 기록이 아닙니다. 종료 Trip은
        아래 전체 운행 요약을 유지합니다.
      </p>
      {hasQuery && (
        <p aria-live="polite">
          선택 차량 v{String(shownVehicle).padStart(6, "0")} · 총 {messageTotal}
          건 · 현재 {intervalSamples.length ? messageOffset + 1 : 0}~
          {messageOffset + intervalSamples.length}건
        </p>
      )}
      <div className="view-switch">
        <button
          disabled={queryBusy || messageOffset === 0}
          onClick={() =>
            void query(
              loadedRange.start,
              loadedRange.end,
              loadedRange.vehicle,
              Math.max(0, messageOffset - 100),
            )
          }
        >
          이전 100건
        </button>
        <button
          disabled={
            queryBusy || messageOffset + intervalSamples.length >= messageTotal
          }
          onClick={() =>
            void query(
              loadedRange.start,
              loadedRange.end,
              loadedRange.vehicle,
              messageOffset + 100,
            )
          }
        >
          다음 100건
        </button>
      </div>
      <div className="interval-messages">
        <MessageViewer samples={intervalSamples} catalog={catalog} />
      </div>
      <h3>종료 Trip 요약</h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>차량</th>
              <th>거리 (m)</th>
              <th>평균속도 (km/h)</th>
              <th>연료 (L·개발 기준)</th>
            </tr>
          </thead>
          <tbody>
            {result.trip_summaries?.map((s) => (
              <tr key={s.vehicle_id}>
                <td>{s.vehicle_id}</td>
                <td>{Number(s.message.pld.dis).toFixed(1)}</td>
                <td>{Number(s.message.pld.mesp).toFixed(1)}</td>
                <td>{Number(s.message.pld.fc).toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details>
        <summary>Trip JSON 보기 · 최대 100대</summary>
        <MessageViewer
          samples={result.trip_summaries || []}
          catalog={catalog}
        />
      </details>
    </>
  );
}

const explanations: Record<string,string> = {
  "데이터 생성 방식": "수동은 입력 JSON을 복제하고, 자동은 주행 조건으로 Microtrip과 Trip을 만듭니다.",
  "실행 시간": "실제 시간은 주기를 기다립니다. 가상시간은 기다리지 않고 생성하며 외부 송신은 하지 않습니다.",
  "전송 방식": "생성만 확인하거나 실제 MQTT로 보냅니다. 실제 송신은 서버 설정과 확인이 필요합니다.",
  "차량 수": "데이터를 만들 가상 차량 수입니다. 늘리면 생성량과 처리 부하가 커집니다.",
  "연결 방식": "연결 풀은 여러 차량이 연결을 공유합니다. 차량별 연결은 차량마다 한 연결을 배정합니다.",
  "Client 수": "연결 풀에서 사용할 연결 개수입니다. 늘리면 연결별 부담은 줄고 접속 부담은 늘어납니다.",
  "연결 유지 정책": "기간 내내 연결하거나 운행 중에만 연결합니다. 운행 중만 유지하면 주차 중 접속 시간이 줄어듭니다.",
  "차량당 생성 건수": "수동 모드의 차량별 메시지 수입니다. 자동 모드에서는 운행 시간과 주기로 계산합니다.",
  "생성 주기 (밀리초)": "메시지 생성 간격입니다. 짧을수록 생성량이 늘어납니다. 2,000ms는2초입니다.",
  "이상 주행 빈도": "급기동 발생 기회 수입니다. 높일수록 자주 시도하지만 도로 조건에 따라 실제 횟수는 달라집니다.",
  "전송 처리 방식": "순차는 한 건씩 응답을 기다립니다. 동시는 서로 다른 Client의 메시지를 함께 보냅니다.",
  "동시 처리 Client 수": "동시에 발행 응답을 기다릴 Client 수입니다. 늘리면 병행 처리량이 커질 수 있습니다.",
  "대기 메시지 상한": "발행 전 메시지를 보관할 최대 개수입니다. 가득 차면 다음 생성을 기다립니다.",
  "전송 성능": "생성·접속·발행 지연을 구분해 표시합니다. P95/P99 상한은 구간별 추정값입니다.",
  "이상 주행 강도": "급기동의 가감속·회전을 강하게 만듭니다. 발생 빈도는 별도로 설정합니다.",
  "엔진 시작 상태": "냉간은 식은 엔진, 온간은 데워진 엔진으로 시작합니다. 초기 RPM·연료·냉각수에 영향을 줍니다.",
  "날씨 조건": "가상 노면 상태입니다. 강우를 선택하면 주행 속도와 가감속을 완화합니다.",
  "운행 프로필": "주행할 도로 성격입니다. 바꾸면 속도·정차·회전 패턴이 달라집니다.",
  "생성 확인": "실제로 생성한 메시지 수입니다. 전송 성공 건수와는 다릅니다.",
  "미생성": "계획했지만 아직 만들지 않은 메시지 수입니다.",
  "발행 미시도": "아직 MQTT 발행을 시도하지 않은 메시지 수입니다. 송신 없는 실행에서는0입니다.",
  "최대 주기 지연": "메시지 생성이 예정 시각보다 가장 늦어진 시간입니다. 일시중지는 제외합니다.",
  "Microtrip": "운행 중 주기적으로 만드는 위치·속도·엔진 메시지입니다.",
  "Trip": "운행이 종료될 때 만드는 운행 전체 요약입니다.",
  "배정 Client": "실행 전에 배정한 Client ID 수입니다. 실제 연결 성공 수와 구분합니다.",
  "접속 확인 Client": "한 번 이상 접속에 성공한 Client 수입니다. 현재 연결 수는 아닙니다.",
  "현재 연결": "Worker가 마지막으로 보고한 열려 있는 연결 수입니다.",
  "최종 보고 연결": "Worker가 마지막으로 보고한 연결 수입니다. 정리 미확인 시 최종 상태로 확정할 수 없습니다.",
  "발행 시도": "MQTT 발행을 요청한 건수입니다. 응답을 못 받은 요청도 포함합니다.",
  "응답 미확인": "발행했지만 PUBACK을 확인하지 못한 건수입니다. 브로커에 도착했을 수 있습니다.",
  "재연결 시도": "기존 Client ID로 다시 접속을 시도한 횟수입니다. 성공 횟수와 구분합니다.",
  "MQTT 발행": "현재 QoS1에서 PUBACK까지 확인한 발행 건수입니다.",
  "PUBACK": "브로커의 수신 확인입니다. 이후 DB·S3 저장 완료를 보장하지 않습니다.",
  "생성 JSON 크기": "생성한 JSON 본문의 총 크기입니다. 통신 헤더와 AWS 과금 단위는 제외합니다."
};

export function Info({name,range}:{name:string;range?:string}) {
  const bounds:Record<string,string>={"차량 수":"1~10,000대 · 전체 합계10,000메시지 이하","대기 메시지 상한":"1~1,000건","이상 주행 빈도":"0~60회/운행 1시간"};
  return <InfoHint label={name} text={explanations[name]||name} range={range??bounds[name]} />;
}

export function MessageViewer({samples,catalog,localOnly=false}: {samples:Sample[];catalog:Catalog;localOnly?:boolean}) {
  const [table,setTable]=useState(true);
  const [picked,setPicked]=useState(0);
  const [inspect,setInspect]=useState(true);
  const current=samples[Math.min(picked,Math.max(0,samples.length-1))];
  function display(key:string,value:unknown) {
    if (value === undefined || value === null) return "—";
    if (key === "em" && typeof value === "number") {
      const names=["급출발","급좌회전","급우회전","급유턴","급감속","급가속","급정지","과속"];
      return `${value} · 0x${value.toString(16).toUpperCase().padStart(2,"0")} · ${names.filter((_,bit)=>(value & (1<<bit))!==0).join(", ") || "이벤트 없음"}`;
    }
    if (["ts","clt","stt","edt"].includes(key) && typeof value === "number") return new Date(value).toLocaleString("ko-KR",{timeZone:"Asia/Seoul"})+" · UTC ms "+value;
    if (typeof value === "number") return value.toLocaleString("ko-KR",{maximumFractionDigits:6});
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }
  const profile=current && catalog.profiles[String(current.message.ty)];
  const rows: [string,string,unknown][] = current ? [["ty","메시지 종류",profile?.title || current.message.ty],["ts","메시지 시각",current.message.ts],...Object.entries(current.message.pld).map(([key,value])=>[key,profile?.schema.properties.pld.properties[key]?.title || "규격 설명 없음",value] as [string,string,unknown])] : [];
  return <div className="message-viewer">
    <div className="view-switch" role="group" aria-label="메시지 보기 형식"><button aria-pressed={table} onClick={()=>setTable(true)}>표로 보기</button><button aria-pressed={!table} onClick={()=>setTable(false)}>JSON으로 보기</button><span className="note">현재 페이지 {samples.length}건 · 행을 선택해 전체 항목 확인</span>{table && samples.length===1 && !inspect && <button onClick={()=>{setPicked(0);setInspect(true);}}>상세표 다시 열기</button>}</div>
    {!samples.length ? <p className="empty-state">표시할 메시지가 없습니다.</p> : !table ? <pre className="json-view">{JSON.stringify(samples.map(s=>s.message),null,2)}</pre> : <div className={`message-split ${inspect ? "with-inspector" : ""}`}>
      <div className="table-scroll message-index"><table><thead><tr><th>선택</th><th>시각 (KST)<br/><code>ts</code></th><th>차량</th><th>종류<br/><code>ty</code></th><th>속도 (km/h)<br/><code>sp / mesp</code></th><th>RPM<br/><code>rpm</code></th><th>거리 (m)<br/><code>cdit / dis</code></th></tr></thead><tbody>{samples.map((s,i)=><tr key={s.vehicle_id+":"+s.sequence} className={picked===i && inspect ? "is-selected" : ""}><td><button aria-label={`메시지 ${s.sequence} 상세`} aria-pressed={picked===i && inspect} onClick={()=>{setPicked(i);setInspect(true);}}>{s.sequence}</button></td><td title={String(s.message.ts)}>{new Date(s.message.ts).toLocaleTimeString("ko-KR",{timeZone:"Asia/Seoul",hour12:false})}</td><td>{s.vehicle_id}</td><td>{s.synthetic_trip ? "시험용 Trip" : s.message.ty===3 ? "Trip" : "Microtrip"}</td><td title={s.message.ty===3 ? "전체 운행 평균 속도" : "현재 속도"}>{display("",s.message.ty===3 ? s.message.pld.mesp : s.message.pld.sp)}</td><td>{display("",s.message.pld.rpm)}</td><td>{display("",s.message.ty===3 ? s.message.pld.dis : s.message.pld.cdit)}</td></tr>)}</tbody></table></div>
      {inspect && current && <section className="message-detail"><div className="panel-heading"><h4>{current.vehicle_id} · {current.synthetic_trip ? "시험용 Trip" : profile?.title || "메시지"}</h4><button onClick={()=>setInspect(false)} aria-label="메시지 상세 닫기">×</button></div><div className="table-scroll"><table><thead><tr><th>항목</th><th>필드</th><th>값</th></tr></thead><tbody>{rows.map(([key,title,value])=><tr key={key}><td>{title}</td><td><code>{key}</code></td><td>{display(key,value)}</td></tr>)}</tbody></table></div>{!localOnly && <details><summary>로컬 배정·참고 토픽</summary><p className="note">Client {current.client_id} · {current.topic}<br/>재생성 조회의 토픽은 당시 전송 토픽을 보장하지 않습니다.</p></details>}</section>}
    </div>}
    <p className="note">표는 한국어 항목명과 소수 최대6자리로 표시합니다. JSON에는 OBD 본문만 표시하며 로컬 설명정보를 넣지 않습니다.</p>
  </div>;
}
