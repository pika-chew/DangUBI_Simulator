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
export type Message = { ty: number; ts: number; pld: Record<string, unknown> };
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
type Assignment = { vehicle_id: string; client_id: string };
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
  return (
    labels[result.job_state || ""] ||
    (result.state === "mqtt_completed"
      ? "MQTT 송신 완료"
      : result.state === "mqtt_failed"
        ? "MQTT 송신 실패·부분 완료"
        : "송신 없는 생성 확인 완료")
  );
}
export function TransmissionBench({onRun,activeId}: {onRun: (run: SavedRun, started?: boolean) => void; activeId:string|null}) {
  const [realtime, setRealtime] = useState(true);
  const [mode, setMode] = useState<"manual" | "automatic">("manual");
  const [generation, setGeneration] = useState<Generation>({
    profile: "mixed",
    model_version: "combustion-events-6",
    anomaly_intensity: "medium",
    engine_start: "cold",
    weather: "dry",
    anomaly_vehicle_percent: 5,
    anomaly_events_per_hour: 2,
    seed: 42,
    start_time_ms: 1794783600000,
    duration_seconds: 60,
    start_lat: 37.5,
    start_lon: 127,
    initial_odometer_m: 500000,
  });
  const [transport, setTransport] = useState<"dry_run" | "mqtt">("dry_run");
  const [mqttReady, setMqttReady] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [catalog, setCatalog] = useState<Catalog | null>(null),
    [ty, setTy] = useState("4"),
    [raw, setRaw] = useState("");
  const [config, setConfig] = useState<Config>({
    vehicle_count: 3,
    mapping_mode: "shared",
    client_count: 2,
    messages_per_vehicle: 2,
    interval_ms: 2000,
    connection_lifetime: "always",
  });
  const [plan, setPlan] = useState<Result | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    Promise.all([api<Catalog>("/api/schema"),api<{mqtt_ready:boolean}>("/api/health")]).then(([c,h])=>{setCatalog(c);setRaw(JSON.stringify(c.profiles["4"].example,null,2));setMqttReady(h.mqtt_ready);}).catch(e=>setError(e.message));
  },[]);
  const locked=busy || !!activeId;
  let message: Message | null = null;
  try {
    message = JSON.parse(raw);
  } catch {
    /* API 확인 버튼에서 오류를 표시한다. */
  }
  const profile = catalog?.profiles[ty];
  function field(key: string, value: unknown) {
    const next =
      message && typeof message.pld === "object" && message.pld
        ? structuredClone(message)
        : structuredClone(profile!.example);
    if (value === undefined) delete next.pld[key];
    else next.pld[key] = value;
    setRaw(JSON.stringify(next, null, 2));
  }
  function changeConfig(key: keyof Config, value: string) {
    setConfig((old) => {
      const next = {
        ...old,
        [key]: ["mapping_mode", "connection_lifetime", "publish_mode"].includes(
          key,
        )
          ? value
          : Number(value),
      } as Config;
      if (next.mapping_mode === "dedicated")
        next.client_count = next.vehicle_count;
      next.max_in_flight =
        next.publish_mode === "parallel"
          ? Math.max(
              1,
              Math.min(next.max_in_flight || 1, next.client_count, 100),
            )
          : 1;
      return next;
    });
  }
  async function execute(preview: boolean) {
    setBusy(true);
    setError("");
    try {
      const payload = {
        config,
        ...(mode === "automatic"
          ? { generation }
          : { message: JSON.parse(raw) }),
        transport,
        confirm_mqtt: confirmed,
        realtime: transport === "dry_run" && realtime,
      };
      if (preview) {
        const data = await api<{ plan: Result }>("/api/preview", payload);
        setPlan(data.plan);
      } else {
        const saved = await api<SavedRun>("/api/jobs", payload);
        onRun(saved,true);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "입력 JSON을 확인하세요.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="transmission-bench">
        <div className="banner">
          <b>
            {transport === "dry_run"
              ? "송신 없는 확인 모드"
              : "실제 MQTT 송신 모드"}
          </b>
          <span>
            {transport === "dry_run"
              ? realtime
                ? "접속·발행 없이 설정한 주기에 맞춰 데이터를 생성합니다. 실행 상태를 조회하거나 중단할 수 있습니다."
                : "접속·발행 없이 가상시간으로 즉시 데이터를 생성합니다."
              : "서버에 설정된 TLS 접속 정보로 실제 발행합니다. QoS 1 PUBACK는 브로커 수신 확인이며 후속 저장 완료를 의미하지 않습니다."}
          </span>
        </div>
        {error && (
          <div role="alert" className="error">
            {error}
          </div>
        )}
        {!catalog ? (
          <section className="card">입력 규격을 불러오는 중입니다.</section>
        ) : (
          <div className="layout">
            <div>
              <section className="card">
                <h2>
                  <em>01</em> 연결·생성 설정
                </h2>
                <p className="note">
                  실행 전에 설정하고 실행 기록에 고정합니다. 다른 조건은 새
                  실행으로 비교하세요.
                </p>
                <fieldset disabled={locked}>
                  <div className="fields">
                    <label htmlFor="setting-0">
                      <span className="label-line">
                        데이터 생성 방식 <Info name="데이터 생성 방식" />
                      </span>
                      <select
                        id="setting-0"
                        aria-label="데이터 생성 방식"
                        value={mode}
                        onChange={(e) =>
                          setMode(e.target.value as "manual" | "automatic")
                        }
                      >
                        <option value="manual">수동 입력</option>
                        <option value="automatic">자동 주행 생성</option>
                      </select>
                    </label>
                    <label htmlFor="setting-1">
                      <span className="label-line">
                        실행 시간 <Info name="실행 시간" />
                      </span>
                      <select
                        id="setting-1"
                        aria-label="실행 시간"
                        value={
                          transport === "mqtt"
                            ? "realtime"
                            : realtime
                              ? "realtime"
                              : "virtual"
                        }
                        disabled={transport === "mqtt"}
                        onChange={(e) =>
                          setRealtime(e.target.value === "realtime")
                        }
                      >
                        <option value="realtime">
                          실제 시간 · 주기에 맞춰 실행
                        </option>
                        <option value="virtual">
                          가상시간 · 즉시 생성 확인
                        </option>
                      </select>
                    </label>
                    <label htmlFor="setting-2">
                      <span className="label-line">
                        전송 방식 <Info name="전송 방식" />
                      </span>
                      <select
                        id="setting-2"
                        aria-label="전송 방식"
                        value={transport}
                        onChange={(e) => {
                          setTransport(e.target.value as "dry_run" | "mqtt");
                          setConfirmed(false);
                        }}
                      >
                        <option value="dry_run">송신 없는 확인</option>
                        <option value="mqtt" disabled={!mqttReady}>
                          실제 MQTT 송신{!mqttReady ? " · 서버 설정 필요" : ""}
                        </option>
                      </select>
                    </label>
                    {transport === "mqtt" && (
                      <label className="include">
                        <input
                          type="checkbox"
                          checked={confirmed}
                          onChange={(e) => setConfirmed(e.target.checked)}
                        />
                        실제 연결·발행을 확인했습니다
                      </label>
                    )}

                    <label htmlFor="setting-3">
                      <span className="label-line">
                        차량 수 <Info name="차량 수" />
                      </span>
                      <input
                        id="setting-3"
                        aria-label="차량 수"
                        type="number"
                        min="1"
                        max="10000"
                        value={config.vehicle_count}
                        onChange={(e) =>
                          changeConfig("vehicle_count", e.target.value)
                        }
                      />
                    </label>
                    <label htmlFor="setting-4">
                      <span className="label-line">
                        연결 방식 <Info name="연결 방식" />
                      </span>
                      <select
                        id="setting-4"
                        aria-label="연결 방식"
                        value={config.mapping_mode}
                        onChange={(e) =>
                          changeConfig("mapping_mode", e.target.value)
                        }
                      >
                        <option value="shared">연결 풀</option>
                        <option value="dedicated">차량별 전체 연결</option>
                      </select>
                    </label>
                    <label htmlFor="setting-5">
                      <span className="label-line">
                        Client 수 <Info name="Client 수" range={"풀1~"+config.vehicle_count.toLocaleString()+"개 · 실제 MQTT는 서버 상한과20개 중 작은 값"} />
                      </span>
                      <input
                        id="setting-5"
                        aria-label="Client 수"
                        type="number"
                        min="1"
                        max={config.vehicle_count}
                        disabled={config.mapping_mode === "dedicated"}
                        value={config.client_count}
                        onChange={(e) =>
                          changeConfig("client_count", e.target.value)
                        }
                      />
                    </label>
                    <label htmlFor="setting-6">
                      <span className="label-line">
                        연결 유지 정책 <Info name="연결 유지 정책" />
                      </span>
                      <select
                        id="setting-6"
                        aria-label="연결 유지 정책"
                        value={config.connection_lifetime}
                        onChange={(e) =>
                          changeConfig("connection_lifetime", e.target.value)
                        }
                      >
                        <option value="always">기간 내내 유지</option>
                        <option value="while_driving">운행 중 유지</option>
                      </select>
                    </label>
                    <label>
                      전송 처리 방식 <Info name="전송 처리 방식" />
                      <select
                        aria-label="전송 처리 방식"
                        value={config.publish_mode || "serial"}
                        onChange={(e) =>
                          changeConfig("publish_mode", e.target.value)
                        }
                      >
                        <option value="serial">순차 전송</option>
                        <option value="parallel">Client별 동시 전송</option>
                      </select>
                    </label>
                    {config.publish_mode === "parallel" && (
                      <>
                        <label>
                          동시 처리 Client 수{" "}
                          <Info name="동시 처리 Client 수" range={"1~"+Math.min(config.client_count,100)+"개 · 순차 전송은1개"} />
                          <input
                            aria-label="동시 처리 Client 수"
                            type="number"
                            min={1}
                            max={Math.min(config.client_count, 100)}
                            value={config.max_in_flight || 1}
                            onChange={(e) =>
                              changeConfig("max_in_flight", e.target.value)
                            }
                          />
                        </label>
                        <label>
                          대기 메시지 상한 <Info name="대기 메시지 상한" />
                          <input
                            aria-label="대기 메시지 상한"
                            type="number"
                            min={1}
                            max={1000}
                            value={config.queue_capacity || 100}
                            onChange={(e) =>
                              changeConfig("queue_capacity", e.target.value)
                            }
                          />
                        </label>
                      </>
                    )}
                    <label htmlFor="setting-7">
                      <span className="label-line">
                        차량당 생성 건수 <Info name="차량당 생성 건수" range={mode==="automatic"?"자동 계산 · 전체10,000메시지 이하":"1~"+Math.floor(10000/config.vehicle_count).toLocaleString()+"건/차량"} />
                      </span>
                      <input
                        id="setting-7"
                        aria-label="차량당 생성 건수"
                        type="number"
                        min="1"
                        disabled={mode === "automatic"}
                        value={
                          mode === "automatic"
                            ? Math.floor(
                                (generation.duration_seconds * 1000) /
                                  config.interval_ms,
                              ) + 2
                            : config.messages_per_vehicle
                        }
                        onChange={(e) =>
                          changeConfig("messages_per_vehicle", e.target.value)
                        }
                      />
                    </label>
                    <label htmlFor="setting-8">
                      <span className="label-line">
                        생성 주기 (밀리초) <Info name="생성 주기 (밀리초)" range={mode==="automatic"?"100ms 이상 · 운행 시간을 나누어 떨어지는10구간 이상":"1~86,400,000ms"} />
                      </span>
                      <input
                        id="setting-8"
                        aria-label="생성 주기 (밀리초)"
                        type="number"
                        min="1"
                        value={config.interval_ms}
                        onChange={(e) =>
                          changeConfig("interval_ms", e.target.value)
                        }
                      />
                    </label>
                  </div>
                </fieldset>
                <div className="note">
                  예정 생성량{" "}
                  <b>
                    {(
                      config.vehicle_count *
                      (mode === "automatic"
                        ? Math.floor(
                            (generation.duration_seconds * 1000) /
                              config.interval_ms,
                          ) + 2
                        : config.messages_per_vehicle)
                    ).toLocaleString()}
                    건
                  </b>{" "}
                  · 송신 없는 확인은 최대 10,000건. 실제 송신은 서버 상한을
                  적용하며 설정한 주기·속도로 제한합니다. 운행 중 유지는 첫
                  메시지에 접속하고 마지막 메시지 응답 후 해제합니다.
                </div>
              </section>
              <section className="card">
                {mode === "automatic" ? (
                  <>
                    <h2>
                      <em>02</em> 자동 주행 설정
                    </h2>
                    <p className="note">
                      기본 v6는 운행 시간과 상황에 따라 이상 주행을 적용하며,
                      도로·강우 규칙과 기어·RPM·부하·연료의 연관성을 더합니다.
                      가상 혼잡·운전자 성향에 따라 짧은 가감속·정속·시동 유지
                      정차를 반복합니다. 개발용 가솔린·6단 자동 모형이며 유턴은
                      기본 생성하지 않습니다. 실제 도로망·차량 간 상호작용·실차
                      보정은 포함하지 않습니다.
                    </p>
                    <fieldset disabled={locked}>
                      <label htmlFor="setting-9">
                        <span className="label-line">
                          운행 프로필 <Info name="운행 프로필" />
                        </span>
                        <select
                          id="setting-9"
                          aria-label="운행 프로필"
                          value={generation.profile}
                          onChange={(e) =>
                            setGeneration({
                              ...generation,
                              profile: e.target.value as Generation["profile"],
                            })
                          }
                        >
                          <option
                            value="mixed"
                            disabled={
                              generation.model_version === "combustion-demo-1"
                            }
                          >
                            도심·고속 혼합
                          </option>
                          <option value="urban">도심 위주</option>
                          <option value="highway">고속 도로 위주</option>
                          <option value="congested">정체 위주</option>
                        </select>
                      </label>
                      <div className="fields">
                        <label>
                          운행 모델
                          <select
                            value={
                              generation.model_version || "combustion-demo-1"
                            }
                            onChange={(e) =>
                              setGeneration({
                                ...generation,
                                model_version: e.target.value,
                                anomaly_intensity:
                                  e.target.value === "combustion-events-6"
                                    ? generation.anomaly_intensity || "medium"
                                    : undefined,
                                engine_start: [
                                  "combustion-powertrain-5",
                                  "combustion-events-6",
                                ].includes(e.target.value)
                                  ? generation.engine_start || "cold"
                                  : undefined,
                                weather: [
                                  "combustion-korea-4",
                                  "combustion-powertrain-5",
                                  "combustion-events-6",
                                ].includes(e.target.value)
                                  ? generation.weather || "dry"
                                  : undefined,
                                profile:
                                  e.target.value === "combustion-demo-1" &&
                                  generation.profile === "mixed"
                                    ? "urban"
                                    : generation.profile,
                                anomaly_vehicle_percent: 0,
                                anomaly_events_per_hour: 0,
                              })
                            }
                          >
                            <option value="combustion-events-6">
                              상황별 이상 주행 v6 · 근사
                            </option>
                          </select>
                        </label>
                        {[
                          "combustion-korea-4",
                          "combustion-powertrain-5",
                          "combustion-events-6",
                        ].includes(generation.model_version || "") && (
                          <label>
                            날씨 조건 <Info name="날씨 조건" />
                            <select
                              aria-label="날씨 조건"
                              value={generation.weather || "dry"}
                              onChange={(e) =>
                                setGeneration({
                                  ...generation,
                                  weather: e.target.value as "dry" | "rain",
                                })
                              }
                            >
                              <option value="dry">건조</option>
                              <option value="rain">강우 · 젖은 노면</option>
                            </select>
                          </label>
                        )}
                        {[
                          "combustion-powertrain-5",
                          "combustion-events-6",
                        ].includes(generation.model_version || "") && (
                          <label>
                            엔진 시작 상태 <Info name="엔진 시작 상태" />
                            <select
                              aria-label="엔진 시작 상태"
                              value={generation.engine_start || "cold"}
                              onChange={(e) =>
                                setGeneration({
                                  ...generation,
                                  engine_start: e.target.value as
                                    | "cold"
                                    | "warm",
                                })
                              }
                            >
                              <option value="cold">냉간 · 시동 직후</option>
                              <option value="warm">
                                온간 · 이미 데워진 상태
                              </option>
                            </select>
                          </label>
                        )}
                        <label>
                          이상 대상 차량 비율 (%) <InfoHint label="이상 대상 차량 비율" text="급기동이 발생할 수 있는 차량 비율입니다. 실제 발생은 보장하지 않으며 과속은 별도 판정합니다." range="0~100%" />
                          <input
                            type="number"
                            min={0}
                            max={100}
                            step="any"
                            disabled={
                              generation.model_version === "combustion-demo-1"
                            }
                            value={generation.anomaly_vehicle_percent || 0}
                            onChange={(e) =>
                              setGeneration({
                                ...generation,
                                anomaly_vehicle_percent: Number(e.target.value),
                              })
                            }
                          />
                        </label>
                        <label>
                          대상 차량 이벤트 빈도 (회/시간){" "}
                          <Info name="이상 주행 빈도" />
                          <input
                            aria-label="대상 차량 이벤트 빈도 (회/시간)"
                            type="number"
                            min={0}
                            max={60}
                            step="any"
                            disabled={
                              generation.model_version === "combustion-demo-1"
                            }
                            value={generation.anomaly_events_per_hour || 0}
                            onChange={(e) =>
                              setGeneration({
                                ...generation,
                                anomaly_events_per_hour: Number(e.target.value),
                              })
                            }
                          />
                        </label>
                        {generation.model_version === "combustion-events-6" && (
                          <label>
                            이상 주행 강도 <Info name="이상 주행 강도" />
                            <select
                              aria-label="이상 주행 강도"
                              value={generation.anomaly_intensity || "medium"}
                              onChange={(e) =>
                                setGeneration({
                                  ...generation,
                                  anomaly_intensity: e.target.value as
                                    | "low"
                                    | "medium"
                                    | "high",
                                })
                              }
                            >
                              <option value="low">낮음</option>
                              <option value="medium">보통</option>
                              <option value="high">높음</option>
                            </select>
                          </label>
                        )}
                        {(
                          [
                            ["duration_seconds", "운행시간 (초)", 10, 3600],
                            ["seed", "재현용 seed", -2147483648, 2147483647],
                            [
                              "start_time_ms",
                              "시작 시각 (UTC 밀리초)",
                              0,
                              4102444800000,
                            ],
                            [
                              "initial_odometer_m",
                              "초기 총 주행거리 (m)",
                              0,
                              1e9,
                            ],
                            ["start_lat", "시작 위도", -85, 85],
                            ["start_lon", "시작 경도", -180, 180],
                          ] as const
                        ).map(([key, label, min, max]) => (
                          <label key={key}>
                            {label} <InfoHint label={label} text={key==="seed"?"무작위 주행의 기준값입니다. 같은 버전·조건·seed로 재현합니다.":key==="duration_seconds"?"한 번의 운행 길이입니다. 늘리면 운행 시간과 메시지가 늘어납니다.":key==="initial_odometer_m"?"출발 전 차량의 총주행거리입니다. 여기에 새 운행 거리가 더해집니다.":"운행을 시작할 위치·시각입니다. 생성 데이터의 기준을 바꿉니다."} range={key==="start_time_ms"?"1970~2100년 · UTC 밀리초":min.toLocaleString("ko-KR")+"~"+max.toLocaleString("ko-KR")+(key==="duration_seconds"?"초":key==="initial_odometer_m"?"m":key==="seed"?" · 정수":"°")} />
                            <input
                              type="number"
                              step={
                                key === "start_lat" ||
                                key === "start_lon" ||
                                key === "initial_odometer_m"
                                  ? "any"
                                  : 1
                              }
                              min={min}
                              max={max}
                              value={generation[key]}
                              onChange={(e) =>
                                setGeneration({
                                  ...generation,
                                  [key]: Number(e.target.value),
                                })
                              }
                            />
                          </label>
                        ))}
                      </div>
                    </fieldset>
                    <p className="note">
                      이상 대상 수는 차량 수×비율을 반올림해 seed로 선정합니다.
                      v6 빈도는 대상 차량당 운행 1시간의 평균 발생 기회입니다.
                      상황이 맞지 않거나 직전 사건 종료 후 30초 이내이면
                      건너뛰며, 실제 발생 비율/횟수 보장은 아닙니다. 짧은
                      운행이나 저속에서는 사건이 없을 수 있습니다. 이벤트는 내부
                      관측 지표이며 미확정 em/xyz를 자동 전송하지 않습니다.
                    </p>
                    <p className="note">
                      생성 주기는 운행시간을 나누어 떨어지게 하고 최소 10구간을
                      확보해야 합니다. 시작·종료 Microtrip과 종료 Trip을 포함해
                      건수를 계산합니다. 같은 seed·시각·조건에서 같은 데이터를
                      생성합니다. 짧은 운행은 가속 한계에 따라 최고속도가
                      낮아집니다.
                    </p>
                    <p className="note">
                      개발 기준: 속도 km/h, 거리 m, 연료 누적 L, 탄소 g, 시간
                      필드 초. 일부 단위는 수신 계약 미확정입니다. v2 프로필은
                      도로 구성과 혼잡·정차 비중을 바꾸며, v1은 이전 고정 비율
                      모델입니다.
                    </p>
                  </>
                ) : (
                  <>
                    <h2>
                      <em>02</em> 수동 OBD 데이터
                    </h2>
                    <p className="note">
                      {catalog.status}. 단위·자료형이 불명확한 필드는 표시된
                      개발 기준으로만 확인합니다.
                    </p>
                    <fieldset disabled={locked}>
                      <label>
                        메시지 종류
                        <select
                          value={ty}
                          onChange={(e) => {
                            setTy(e.target.value);
                            setRaw(
                              JSON.stringify(
                                catalog.profiles[e.target.value].example,
                                null,
                                2,
                              ),
                            );
                          }}
                        >
                          <option value="4">OBD Microtrip · ty 4</option>
                          <option value="3">OBD Trip · ty 3</option>
                        </select>
                      </label>
                      <div className="fields data-fields">
                        {profile &&
                          Object.entries(
                            profile.schema.properties.pld.properties,
                          ).map(([key, schema]) => {
                            const required =
                                profile.schema.properties.pld.required.includes(
                                  key,
                                ),
                              enabled =
                                !!message?.pld &&
                                Object.hasOwn(message.pld, key);
                            return (
                              <div key={ty + key} className="data-field">
                                <div className="field-title">
                                  <label htmlFor={"field-" + key}>
                                    {schema.title} <code>{key}</code>
                                    {required && <small>필수</small>}
                                  </label>
                                  {!required && (
                                    <label className="include">
                                      <input
                                        type="checkbox"
                                        checked={enabled}
                                        onChange={(e) =>
                                          field(
                                            key,
                                            e.target.checked
                                              ? schema.default
                                              : undefined,
                                          )
                                        }
                                      />
                                      포함
                                    </label>
                                  )}
                                </div>
                                <input
                                  id={"field-" + key}
                                  type={
                                    schema.type === "string" ? "text" : "number"
                                  }
                                  step={schema.type === "integer" ? 1 : "any"}
                                  min={schema.minimum}
                                  max={schema.maximum}
                                  disabled={!enabled}
                                  value={
                                    enabled ? String(message!.pld[key]) : ""
                                  }
                                  onChange={(e) =>
                                    field(
                                      key,
                                      e.target.value === ""
                                        ? undefined
                                        : schema.type === "string"
                                          ? e.target.value
                                          : Number(e.target.value),
                                    )
                                  }
                                />
                              </div>
                            );
                          })}
                      </div>
                    </fieldset>
                  </>
                )}
              </section>
            </div>
            <div>
              <section className="card">
                <h2>
                  <em>03</em> JSON 미리보기
                </h2>
                <p className="note">
                  {mode === "automatic"
                    ? "입력·배정 확인을 누르면 첫 Microtrip을 표시합니다. 전체 결과의 속도 변화와 종료 Trip도 확인할 수 있습니다."
                    : "필드 입력과 JSON은 함께 갱신됩니다. 아래에서 직접 편집할 수 있습니다. 시각은 UTC 밀리초입니다."}
                </p>
                <textarea
                  aria-label="OBD JSON"
                  spellCheck={false}
                  disabled={locked || mode === "automatic"}
                  value={
                    mode === "automatic"
                      ? plan?.samples?.[0]
                        ? JSON.stringify(plan.samples[0].message, null, 2)
                        : "자동 주행 설정 후 입력·배정 확인을 눌러주세요."
                      : raw
                  }
                  onChange={(e) => setRaw(e.target.value)}
                />
                <div className="actions">
                  <button disabled={locked} onClick={() => execute(true)}>
                    입력·배정 확인
                  </button>
                  <button
                    className="primary"
                    disabled={locked || (transport === "mqtt" && !confirmed)}
                    onClick={() => execute(false)}
                  >
                    {busy
                      ? "처리 중…"
                      : transport === "mqtt"
                        ? "MQTT 실행 시작"
                        : "생성 실행 시작"}
                  </button>
                </div>
                <p className="note">
                  {mode === "automatic"
                    ? "운행 조건으로 Microtrip과 Trip을 함께 생성합니다. 실제 시간은 설정한 주기에 맞춰 실행하고, 가상시간은 즉시 생성합니다."
                    : "여러 차량에 입력값을 복제하고 차량별 운행 번호와 시각을 갱신합니다."}
                </p>
                {plan && (
                  <div className="plan" role="status">
                    입력 확인 완료 · 차량 {plan.assignment_count}대 → Client{" "}
                    {plan.allocated_clients}개 배정 예정 ·{" "}
                    {plan.planned_messages}건 생성 예정
                  </div>
                )}
              </section>
              {plan && <section className="card"><h3>시험 입력 확인</h3><p>차량 {plan.assignment_count}대 · Client {plan.allocated_clients}개 · 예정 {plan.planned_messages.toLocaleString()}건</p><p className="note">새 실행의 결과는 왼쪽 작업 기록과 차량 데이터에서 확인합니다.</p></section>}
            </div>
          </div>
        )}
      </div>
    </>
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
      const names=["급출발","급좌회전","급우회전","급유턴","급감속","급가속","급정지","","과속"];
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
