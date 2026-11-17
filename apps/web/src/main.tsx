import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type FieldSchema = {
  type: string;
  title: string;
  default: unknown;
  minimum?: number;
  maximum?: number;
};
type Message = { ty: number; ts: number; pld: Record<string, unknown> };
type Profile = {
  title: string;
  example: Message;
  schema: {
    properties: {
      pld: { properties: Record<string, FieldSchema>; required: string[] };
    };
  };
};
type Catalog = {
  profile_version: string;
  status: string;
  profiles: Record<string, Profile>;
};
type Config = {
  vehicle_count: number;
  mapping_mode: "shared" | "dedicated";
  client_count: number;
  messages_per_vehicle: number;
  interval_ms: number;
  connection_lifetime: "always" | "while_driving";
};
type Assignment = { vehicle_id: string; client_id: string };
type Generation = {
  profile: "urban" | "highway" | "congested" | "mixed";
  model_version?: string;
  weather?: "dry" | "rain";
  anomaly_vehicle_percent?: number;
  anomaly_events_per_hour?: number;
  seed: number;
  start_time_ms: number;
  duration_seconds: number;
  start_lat: number;
  start_lon: number;
  initial_odometer_m: number;
};
type TracePoint = {
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
  };
};
type Sample = Assignment & {
  sequence: number;
  topic: string;
  message: Message;
};
type Result = {
  run_id: string;
  transport?: string;
  errors?: string[];
  publish_attempts?: number;
  unconfirmed_messages?: number;
  reconnect_count?: number;
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
type SavedRun = {
  id: string;
  created_at: string;
  request: {
    config: Config;
    message?: Message;
    generation?: Generation;
    realtime?: boolean;
    transport?: "dry_run" | "mqtt";
  };
  result: Result;
};
async function api<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      Array.isArray(data.detail)
        ? data.detail.join("\n")
        : data.detail || "요청을 처리하지 못했습니다.",
    );
  return data;
}
const activeStates = new Set([
  "starting",
  "running",
  "pausing",
  "paused",
  "resuming",
  "stopping",
]);
function stateLabel(result: Result): string {
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
function App() {
  const [selected, setSelected] = useState<string[]>([]);
  const [deleteReview, setDeleteReview] = useState(false);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [realtime, setRealtime] = useState(true);
  const [stopPending, setStopPending] = useState(false);
  const [pausePending, setPausePending] = useState(false);
  const [mode, setMode] = useState<"manual" | "automatic">("manual");
  const [generation, setGeneration] = useState<Generation>({
    profile: "mixed",
    model_version: "combustion-korea-4",
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
    [run, setRun] = useState<SavedRun | null>(null),
    [history, setHistory] = useState<SavedRun[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [health, setHealth] = useState("확인 중");
  useEffect(() => {
    Promise.all([
      api<Catalog>("/api/schema"),
      api<{
        worker_ready: boolean;
        mqtt_ready: boolean;
        active_job_id?: string;
      }>("/api/health"),
      api<SavedRun[]>("/api/runs"),
    ])
      .then(async ([c, h, r]) => {
        setCatalog(c);
        setRaw(JSON.stringify(c.profiles["4"].example, null, 2));
        setHistory(r);
        setMqttReady(h.mqtt_ready);
        setHealth(h.worker_ready ? "Worker 준비 완료" : "Worker 준비 필요");
        if (h.active_job_id) {
          setActiveJobId(h.active_job_id);
          const saved = await api<SavedRun>("/api/jobs/" + h.active_job_id);
          setRun(saved);
          setConfig(saved.request.config);
          setTransport(saved.request.transport || "dry_run");
          setRealtime(saved.request.realtime ?? false);
          if (saved.request.generation) {
            setMode("automatic");
            setGeneration(saved.request.generation);
          } else if (saved.request.message) {
            setMode("manual");
            setTy(String(saved.request.message.ty));
            setRaw(JSON.stringify(saved.request.message, null, 2));
          }
        }
      })
      .catch((e) => setError(String(e.message)));
  }, []);
  useEffect(() => {
    if (!activeJobId) return;
    let canceled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const saved = await api<SavedRun>("/api/jobs/" + activeJobId);
        if (canceled) return;
        setRun(saved);
        if (!activeStates.has(saved.result.job_state || "")) {
          setActiveJobId(null);
          setStopPending(false);
          setHistory(await api<SavedRun[]>("/api/runs"));
          return;
        }
      } catch (e) {
        if (!canceled)
          setError(
            e instanceof Error ? e.message : "실행 상태를 확인하지 못했습니다.",
          );
      }
      if (!canceled) timer = setTimeout(poll, 1000);
    }
    void poll();
    return () => {
      canceled = true;
      clearTimeout(timer);
    };
  }, [activeJobId]);
  const locked = busy || !!activeJobId;
  async function stop() {
    if (!activeJobId) return;
    setStopPending(true);
    try {
      setRun(await api<SavedRun>("/api/jobs/" + activeJobId + "/stop", {}));
      setError("");
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "중단 요청을 전달하지 못했습니다.",
      );
      setStopPending(false);
    }
  }
  async function removeSelected() {
    setBusy(true);
    setError("");
    try {
      await api("/api/runs/delete", { ids: selected, confirm: true });
      if (run && selected.includes(run.id)) {
        setRun(null);
        setPlan(null);
      }
      setSelected([]);
      setDeleteReview(false);
      setHistory(await api<SavedRun[]>("/api/runs"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "삭제를 완료하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }
  async function pauseResume() {
    if (!activeJobId) return;
    setPausePending(true);
    try {
      const action = run?.result.job_state === "paused" ? "resume" : "pause";
      setRun(
        await api<SavedRun>("/api/jobs/" + activeJobId + "/" + action, {}),
      );
      setError("");
    } catch (e) {
      setError(
        e instanceof Error
          ? e.message
          : "일시중지·재개 요청을 전달하지 못했습니다.",
      );
    } finally {
      setPausePending(false);
    }
  }
  useEffect(() => {
    setPlan(null);
  }, [raw, config, mode, generation]);
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
        [key]: ["mapping_mode", "connection_lifetime"].includes(key)
          ? value
          : Number(value),
      } as Config;
      if (next.mapping_mode === "dedicated")
        next.client_count = next.vehicle_count;
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
        setRun(saved);
        setActiveJobId(saved.id);
        setHistory(await api<SavedRun[]>("/api/runs"));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "입력 JSON을 확인하세요.");
    } finally {
      setBusy(false);
    }
  }
  async function load(id: string) {
    setBusy(true);
    try {
      setRun(await api<SavedRun>("/api/runs/" + id));
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <header>
        <div>
          <span className="brand">DANGUBI / SIMULATOR</span>
          <h1>차량 데이터 생성 확인</h1>
          <p>
            수동 입력과 자동 주행 데이터 생성, 연결 방식별 전송을 확인합니다.
          </p>
        </div>
        <span className="status">{health}</span>
      </header>
      <main>
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
        <nav aria-label="데이터 흐름">
          <span>{mode === "automatic" ? "운행 설정" : "수동 OBD 입력"}</span>
          <i>→</i>
          <span>JSON 미리보기</span>
          <i>→</i>
          <span>제어 API</span>
          <i>→</i>
          <span>Go Worker</span>
          <i>→</i>
          <span className="offline">
            {mqttReady ? "MQTT · 서버 설정 준비" : "IoT Core · 설정 필요"}
          </span>
        </nav>
        {error && (
          <div role="alert" className="error">
            {error}
          </div>
        )}
        {activeJobId && (
          <section className="card live-run" aria-live="polite">
            <div>
              <h2>
                {run?.id === activeJobId
                  ? stateLabel(run.result)
                  : "실행 상태 불러오는 중"}
              </h2>
              <p className="note">
                실행 ID <code>{activeJobId}</code> · 설정과 차량·Client 배정은
                고정됩니다. 화면을 새로 열어도 진행 중인 실행을 다시 조회합니다.
              </p>
              {run?.id === activeJobId && (
                <>
                  <progress
                    aria-label="생성 진행률"
                    max={Math.max(1, run.result.planned_messages)}
                    value={run.result.processed_messages}
                  />
                  <p>
                    {run.result.processed_messages.toLocaleString()} /{" "}
                    {run.result.planned_messages.toLocaleString()}건 생성 · 경과{" "}
                    {((run.result.elapsed_ms || 0) / 1000).toFixed(1)}초 ·
                    PUBACK {run.result.puback_count}건
                  </p>
                  <p className="note">
                    정지 {((run.result.paused_ms || 0) / 1000).toFixed(1)}초 ·
                    정지 제외{" "}
                    {((run.result.active_elapsed_ms || 0) / 1000).toFixed(1)}초
                    · 운행 진행{" "}
                    {((run.result.simulation_time_ms || 0) / 1000).toFixed(1)}초
                    · 전체 제한 {run.result.wall_limit_seconds ?? "—"}초. 정지
                    중에도 전체 제한시간은 흐릅니다.
                  </p>
                </>
              )}
            </div>
            <div className="run-controls">
              <button
                disabled={
                  pausePending ||
                  stopPending ||
                  !["running", "paused"].includes(run?.result.job_state || "")
                }
                onClick={pauseResume}
              >
                {run?.result.job_state === "paused" ? "실행 재개" : "일시중지"}
              </button>
              <button
                className="stop"
                disabled={stopPending || run?.result.job_state === "stopping"}
                onClick={stop}
              >
                {stopPending || run?.result.job_state === "stopping"
                  ? "중단 처리 중…"
                  : "실행 중단"}
              </button>
            </div>
          </section>
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
                        Client 수 <Info name="Client 수" />
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
                    <label htmlFor="setting-7">
                      <span className="label-line">
                        차량당 생성 건수 <Info name="차량당 생성 건수" />
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
                        생성 주기 (밀리초) <Info name="생성 주기 (밀리초)" />
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
                      기본 v4는 도심 기본 속도와 강우 조건을 반영하며, 가상
                      혼잡·운전자 성향에 따라 짧은 가감속·정속·시동 유지 정차를
                      반복합니다. 도로 커브·경사도 근사하며 유턴은 기본 생성하지
                      않습니다. 실제 도로망·차량 간 상호작용·실차 보정은
                      포함하지 않습니다.
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
                                weather:
                                  e.target.value === "combustion-korea-4"
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
                            <option value="combustion-korea-4">
                              국내 기본 규칙 v4 · 근사
                            </option>
                            <option value="combustion-route-3">
                              경로 기반 모델 v3 · 근사
                            </option>
                            <option value="combustion-scenario-2">
                              상황 기반 모델 v2 · 근사
                            </option>
                            <option value="combustion-demo-1">
                              이전 고정 비율 모델 v1
                            </option>
                          </select>
                        </label>
                        {generation.model_version === "combustion-korea-4" && (
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
                        <label>
                          이상 대상 차량 비율 (%)
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
                          대상 차량 이벤트 빈도 (회/시간)
                          <input
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
                            {label}
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
                      빈도는 대상 차량의 확률적 발생 설정이며 실제 발생
                      비율/횟수 보장은 아닙니다. 짧은 운행이나 저속에서는 사건이
                      없을 수 있습니다. 이벤트는 내부 관측 지표이며 미확정
                      em/xyz를 자동 전송하지 않습니다.
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
              <section className="card result" aria-live="polite">
                <h2>
                  <em>04</em> Worker 처리 결과
                </h2>
                {!run ? (
                  <p className="note">
                    생성 확인 후 결과가 표시됩니다. 실제 전송 성공과 구분해
                    집계합니다.
                  </p>
                ) : (
                  <>
                    <span className="tag">{stateLabel(run.result)}</span>
                    {!!run.result.errors?.length && (
                      <div className="error" role="alert">
                        {run.result.errors.join("\n")}
                      </div>
                    )}
                    <p className="note">
                      실행 ID <code>{run.id}</code>
                      <br />
                      확인 시각{" "}
                      {new Date(run.created_at).toLocaleString("ko-KR")}
                    </p>
                    {run.result.job_state && (
                      <p className="note">
                        경과 {((run.result.elapsed_ms || 0) / 1000).toFixed(1)}
                        초 · 연결 정리{" "}
                        {activeStates.has(run.result.job_state)
                          ? "실행 중"
                          : run.result.connection_cleanup_confirmed
                            ? "확인됨"
                            : "미확인"}
                        {run.result.forced_termination
                          ? " · Worker 강제 종료"
                          : ""}
                        . 중단된 운행은 종료 Trip을 새로 만들지 않습니다. 정지{" "}
                        {((run.result.paused_ms || 0) / 1000).toFixed(1)}초 ·
                        정지 제외{" "}
                        {((run.result.active_elapsed_ms || 0) / 1000).toFixed(
                          1,
                        )}
                        초 · 운행 진행{" "}
                        {((run.result.simulation_time_ms || 0) / 1000).toFixed(
                          1,
                        )}
                        초.
                      </p>
                    )}
                    <div className="metrics">
                      {[
                        ["생성 확인", run.result.processed_messages + "건"],
                        [
                          "미생성",
                          (run.result.remaining_messages ??
                            Math.max(
                              0,
                              run.result.planned_messages -
                                run.result.processed_messages,
                            )) + "건",
                        ],
                        [
                          "발행 미시도",
                          (run.result.unsent_messages ?? 0) + "건",
                        ],
                        [
                          "최대 주기 지연",
                          (run.result.schedule_delay_ms ?? 0) + "ms",
                        ],
                        [
                          "Microtrip",
                          run.result.microtrip_count === undefined
                            ? "기록 없음"
                            : run.result.microtrip_count + "건",
                        ],
                        [
                          "Trip",
                          run.result.trip_count === undefined
                            ? "기록 없음"
                            : run.result.trip_count + "건",
                        ],
                        ["배정 Client", run.result.allocated_clients + "개"],
                        [
                          "접속 확인 Client",
                          run.result.connected_clients + "개",
                        ],
                        [
                          activeStates.has(run.result.job_state || "")
                            ? "현재 연결"
                            : "최종 보고 연결",
                          (run.result.active_clients || 0) + "개",
                        ],
                        [
                          "발행 시도",
                          (run.result.publish_attempts || 0) + "건",
                        ],
                        [
                          "응답 미확인",
                          (run.result.unconfirmed_messages || 0) + "건",
                        ],
                        [
                          "재연결 시도",
                          (run.result.reconnect_count || 0) + "회",
                        ],
                        ["MQTT 발행", run.result.mqtt_published + "건"],
                        ["PUBACK", run.result.puback_count + "건"],
                        [
                          "생성 JSON 크기",
                          run.result.payload_bytes.toLocaleString() + "B",
                        ],
                      ].map(([title, value]) => (
                        <div key={title}>
                          <span>
                            {title} <Info name={title} />
                          </span>
                          <b>{value}</b>
                        </div>
                      ))}
                    </div>
                    {!!run.result.trace?.length && (
                      <DrivingResult
                        key={run.id}
                        result={run.result}
                        catalog={catalog}
                      />
                    )}
                    {run.result.scenario && (
                      <p className="plan">
                        이상 대상 {run.result.scenario.eligible_vehicles}대 ·
                        실제 발생 {run.result.scenario.observed_vehicles}대 ·
                        내부 이벤트 구간 {run.result.scenario.events}개 (생성된
                        구간 기준)
                      </p>
                    )}
                    <h3>고정된 차량 → Client 배정</h3>
                    <p className="note">
                      총 {run.result.assignment_count}대 중 최대 100대 표시 ·
                      실행{" "}
                      {run.request.config.mapping_mode === "shared"
                        ? "연결 풀"
                        : "차량별 전체 연결"}
                    </p>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>차량 ID</th>
                            <th>Client ID</th>
                          </tr>
                        </thead>
                        <tbody>
                          {run.result.assignments.map((a) => (
                            <tr key={a.vehicle_id}>
                              <td>{a.vehicle_id}</td>
                              <td>{a.client_id}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <details>
                      <summary>생성 메시지 보기 · 최대 10건</summary>
                      <MessageViewer
                        samples={run.result.samples}
                        catalog={catalog}
                      />
                    </details>
                    <details>
                      <summary>실행 당시 설정</summary>
                      <pre>{JSON.stringify(run.request, null, 2)}</pre>
                    </details>
                  </>
                )}
              </section>
              <section className="card">
                <h2>최근 실행</h2>
                <p className="note">
                  최대 30개 조회 · 로컬 실행 기록이며 최신 입력값과 별개입니다.
                  기본 저장: .local/runs.sqlite3. 전체 메시지 대신 샘플
                  10건·배정 100대·자동 Trip 요약 100대·첫 차량 변화 최대 120점을
                  보관합니다. GitHub에는 올리지 않습니다.
                </p>
                <div className="actions">
                  <button
                    disabled={
                      locked ||
                      !history.some(
                        (h) => !activeStates.has(h.result.job_state || ""),
                      )
                    }
                    onClick={() => {
                      setSelected(
                        history
                          .filter(
                            (h) => !activeStates.has(h.result.job_state || ""),
                          )
                          .map((h) => h.id),
                      );
                      setDeleteReview(false);
                    }}
                  >
                    표시된 종료 기록 선택
                  </button>
                  <button
                    className="stop"
                    disabled={locked || !selected.length}
                    onClick={() => setDeleteReview(true)}
                  >
                    선택 기록 삭제 ({selected.length})
                  </button>
                </div>
                {deleteReview && (
                  <div
                    role="dialog"
                    aria-label="실행 기록 삭제 확인"
                    className="delete-review"
                  >
                    <b>
                      선택한 {selected.length}개 로컬 실행 기록을 영구
                      삭제할까요?
                    </b>
                    <p>
                      설정·집계·샘플·Trip·그래프 기록을 함께 삭제합니다. 복원할
                      수 없습니다. 현재 목록은 최대 30개이며 전체 저장 기록을
                      뜻하지 않습니다.
                    </p>
                    <ul>
                      {selected.map((id) => (
                        <li key={id}>
                          <code>{id}</code>
                        </li>
                      ))}
                    </ul>
                    <div className="actions">
                      <button
                        disabled={busy}
                        onClick={() => setDeleteReview(false)}
                      >
                        취소
                      </button>
                      <button
                        className="stop"
                        disabled={locked}
                        onClick={removeSelected}
                      >
                        영구 삭제 확인
                      </button>
                    </div>
                  </div>
                )}
                {history.length === 0 ? (
                  <p>아직 실행 기록이 없습니다.</p>
                ) : (
                  history.map((h) => (
                    <div className="history-row" key={h.id}>
                      <input
                        type="checkbox"
                        aria-label={"실행 기록 선택 " + h.id}
                        disabled={
                          locked || activeStates.has(h.result.job_state || "")
                        }
                        checked={selected.includes(h.id)}
                        onChange={(e) => {
                          setDeleteReview(false);
                          setSelected(
                            e.target.checked
                              ? [...selected, h.id]
                              : selected.filter((id) => id !== h.id),
                          );
                        }}
                      />
                      <button
                        className="history"
                        disabled={locked}
                        onClick={() => load(h.id)}
                      >
                        <span>
                          {new Date(h.created_at).toLocaleString("ko-KR")}
                          <br />
                          <code>{h.id.slice(0, 12)}</code>
                        </span>
                        <b>
                          {stateLabel(h.result)} · {h.result.assignment_count}대
                          / Client {h.result.allocated_clients}개 /{" "}
                          {h.result.processed_messages}건
                        </b>
                      </button>
                    </div>
                  ))
                )}
              </section>
            </div>
          </div>
        )}
        <footer>
          개발용 잠정 OBD 규격 · 로컬 확인 전용 · AWS 운영 환경은 추후 선택
        </footer>
      </main>
    </>
  );
}
createRoot(document.getElementById("root")!).render(<App />);

function DrivingResult({
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
  const [drag, setDrag] = useState<{ from: number; to: number } | null>(null);
  const dragStart = useRef<number | null>(null);
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
  const firstTime = trace[0]?.elapsed_seconds || 0;
  const duration = Math.max(
    1,
    (trace[trace.length - 1]?.elapsed_seconds || 0) - firstTime,
  );
  const maxSpeed = Math.max(10, ...trace.map((p) => p.speed_kmh));
  const points = trace
    .map(
      (p) =>
        `${48 + ((p.elapsed_seconds - firstTime) / duration) * 510},${170 - (p.speed_kmh / maxSpeed) * 130}`,
    )
    .join(" ");
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
      <svg
        className="drive-chart"
        viewBox="0 0 600 210"
        role="img"
        preserveAspectRatio="none"
        aria-label={
          "차량 " + shownVehicle + " 속도 변화 그래프 · 드래그로 구간 선택"
        }
        onPointerDown={(e) => {
          if (queryBusy || trace.length < 2 || e.button !== 0) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const x = ((e.clientX - rect.left) / rect.width) * 600;
          const y = ((e.clientY - rect.top) / rect.height) * 210;
          if (x < 48 || x > 558 || y < 30 || y > 170) return;
          e.currentTarget.setPointerCapture(e.pointerId);
          dragStart.current = x;
          setDrag({ from: x, to: x });
        }}
        onPointerMove={(e) => {
          if (dragStart.current === null) return;
          const rect = e.currentTarget.getBoundingClientRect();
          setDrag({
            from: dragStart.current,
            to: Math.max(
              48,
              Math.min(558, ((e.clientX - rect.left) / rect.width) * 600),
            ),
          });
        }}
        onPointerCancel={() => {
          dragStart.current = null;
          setDrag(null);
        }}
        onLostPointerCapture={() => {
          dragStart.current = null;
          setDrag(null);
        }}
        onPointerUp={(e) => {
          const from = dragStart.current;
          dragStart.current = null;
          setDrag(null);
          if (e.currentTarget.hasPointerCapture(e.pointerId))
            e.currentTarget.releasePointerCapture(e.pointerId);
          if (from === null) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const to = Math.max(
            48,
            Math.min(558, ((e.clientX - rect.left) / rect.width) * 600),
          );
          if (Math.abs(to - from) < 4) return;
          const begin =
            Math.round(
              (firstTime + ((Math.min(from, to) - 48) / 510) * duration) * 1000,
            ) / 1000;
          const finish =
            Math.round(
              (firstTime + ((Math.max(from, to) - 48) / 510) * duration) * 1000,
            ) / 1000;
          void query(begin, String(finish), shownVehicle);
        }}
      >
        <path d="M48 30 V170 H560" fill="none" stroke="#9bb1bf" />
        <path
          d="M48 105 H560 M48 40 H560"
          fill="none"
          stroke="#dce6eb"
          strokeDasharray="4 4"
        />
        <polyline
          points={points}
          fill="none"
          stroke="#087c70"
          strokeWidth="3"
        />
        {drag && (
          <rect
            x={Math.min(drag.from, drag.to)}
            y={30}
            width={Math.abs(drag.to - drag.from)}
            height={140}
            fill="#087c70"
            fillOpacity={0.18}
            stroke="#087c70"
            pointerEvents="none"
          />
        )}
        <text x="6" y="20">
          km/h
        </text>
        <text x="9" y="45">
          {maxSpeed.toFixed(0)}
        </text>
        <text x="28" y="175">
          0
        </text>
        <text x="45" y="197">
          {firstTime}초
        </text>
        <text x="515" y="197">
          {firstTime + duration}초
        </text>
      </svg>
      <div className="table-scroll">
        <table className="drive-trace">
          <thead>
            <tr>
              <th>경과 (초)</th>
              <th>상태</th>
              <th>속도 (km/h)</th>
              <th>RPM</th>
              <th>누적거리 (m)</th>
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
                <td>{p.rpm}</td>
                <td>{p.distance_m.toFixed(1)}</td>
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

const explanations: Record<string, string> = {
  "데이터 생성 방식":
    "수동 입력은 입력 JSON을 차량별로 복제합니다. 자동 주행 생성은 운행 설정과 seed로 Microtrip과 종료 Trip을 만듭니다.",
  "실행 시간":
    "실제 시간은 설정한 주기에 맞춰 기다립니다. 가상시간은 송신 없이 데이터 시각만 증가시켜 즉시 생성합니다.",
  "전송 방식":
    "송신 없는 확인은 MQTT에 연결하지 않습니다. 실제 MQTT는 서버 접속 설정과 별도 송신 확인이 필요합니다.",
  "차량 수":
    "생성할 가상 차량의 수입니다. 차량 수와 MQTT 연결 수는 별개의 설정입니다.",
  "연결 방식":
    "연결 풀은 여러 차량이 Client를 공유합니다. 차량별 전체 연결은 차량마다 Client 한 개를 배정합니다. 실행 중 배정은 고정됩니다.",
  "Client 수":
    "MQTT 연결에 사용할 Client ID 개수입니다. 풀에서는 차량 수 이하, 차량별 연결에서는 차량 수와 같습니다. 실제 동시 연결 수와는 다를 수 있습니다.",
  "연결 유지 정책":
    "기간 내내 유지는 실행 시작부터 종료까지 연결합니다. 운행 중 유지는 첫 메시지부터 마지막 메시지 응답까지 유지합니다. 일시중지 중에도 기존 연결을 유지합니다.",
  "차량당 생성 건수":
    "수동 모드에서 차량마다 만들 메시지 수입니다. 자동 모드에서는 시작·종료 Microtrip과 종료 Trip을 포함해 자동 계산합니다.",
  "생성 주기 (밀리초)":
    "차량별 메시지 생성 간격입니다. 2,000ms는 2초입니다. 실제 발행 속도 제한이나 응답 지연 때문에 전송 간격은 길어질 수 있습니다.",
  "날씨 조건":
    "v4의 실행 전체에 적용할 가상 날씨입니다. 강우·젖은 노면은 도로 기본 속도 상한의 80%를 제어 상한으로 쓰고 가감속도 완화합니다. 실제 기상 조회, 폭우·결빙·가변 속도표지는 포함하지 않습니다.",
  "운행 프로필":
    "v4는 도심 기본 속도와 선택한 가상 날씨를 적용하고, 합성 경로의 연결 순서와 구간 지속시간·교통 상태를 유지합니다. 고속 중심은 900초 이상에서 진출입로를 거칩니다. 실제 지도·실차 보정은 없으며 v3/v2/v1 기록은 기존 모델로 재생성합니다.",
  "생성 확인":
    "Worker가 생성한 메시지 수입니다. 실제 MQTT에 전달됐거나 저장됐다는 뜻은 아닙니다.",
  미생성:
    "전체 계획 중 아직 생성하지 않은 메시지 수입니다. 중단하면 이 수가 남을 수 있습니다.",
  "발행 미시도":
    "MQTT 계획 중 아직 Publish를 시도하지 않은 수입니다. 송신 없는 실행에서는 발행 대상 자체가 없어 0으로 표시합니다.",
  "최대 주기 지연":
    "예정 생성 시각에 비해 가장 늦었던 시간입니다. 일시중지 시간은 제외합니다. 목표 속도 달성률이나 네트워크 왕복시간과는 다릅니다.",
  Microtrip:
    "운행 중 주기적으로 생성하는 위치·속도·엔진 등의 상세 메시지입니다. 개발 규격 ty=4입니다.",
  Trip: "운행 종료 시 같은 운행 데이터로 집계한 요약 메시지입니다. 개발 규격 ty=3이며 중단된 운행에는 새 종료 Trip을 만들지 않습니다.",
  "배정 Client":
    "실행 전에 고정 배정한 Client ID 수입니다. 실제 접속에 성공한 수와 구분합니다.",
  "접속 확인 Client":
    "한 번 이상 접속 성공을 확인한 고유 Client 수입니다. 현재 열려 있는 연결 수가 아닙니다.",
  "현재 연결":
    "Worker가 마지막으로 보고한 열려 있는 MQTT 연결 수입니다. 서버에서 직접 조회한 실시간 계정 전체 연결 수는 아닙니다.",
  "최종 보고 연결":
    "마지막 Worker 보고의 연결 수입니다. 강제 종료·연결 정리 미확인 상태라면 확정된 최종 값으로 해석하지 않습니다.",
  "발행 시도":
    "MQTT Publish 요청을 시도한 메시지 수입니다. PUBACK를 받지 못한 요청도 포함합니다.",
  "응답 미확인":
    "발행을 시도했지만 PUBACK를 확인하지 못한 수입니다. 실제 브로커에 도착했을 수 있어 자동 재발행하지 않습니다.",
  "재연결 시도":
    "이전에 접속한 Client ID로 다시 접속을 시도한 횟수입니다. 성공 횟수와는 다릅니다.",
  "MQTT 발행":
    "현재 QoS 1 구현에서 PUBACK까지 확인한 발행 건수입니다. 발행 시도 수와 구분하며 PUBACK 지표와 같은 기준입니다.",
  PUBACK:
    "QoS 1 발행에 대한 MQTT 브로커의 수신 확인 응답입니다. 후속 DB·S3 저장이나 파이프라인 처리 완료를 보장하지 않습니다.",
  "생성 JSON 크기":
    "생성한 JSON 본문 크기의 합계입니다. MQTT·TLS·네트워크 헤더와 실제 AWS 과금 단위는 포함하지 않습니다.",
};
function Info({ name }: { name: string }) {
  return (
    <span className="hint">
      <button
        type="button"
        className="info-button"
        aria-label={name + " 도움말"}
        onClick={(e) => e.preventDefault()}
      >
        i
      </button>
      <span role="tooltip" className="hint-content">
        {explanations[name] || name}
      </span>
    </span>
  );
}
function MessageViewer({
  samples,
  catalog,
}: {
  samples: Sample[];
  catalog: Catalog;
}) {
  const [table, setTable] = useState(true);
  function display(key: string, value: unknown) {
    if (
      ["ts", "clt", "stt", "edt"].includes(key) &&
      typeof value === "number"
    ) {
      const date = new Date(value);
      if (Number.isFinite(date.getTime()))
        return (
          date.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" }) +
          " (UTC ms: " +
          value +
          ")"
        );
    }
    if (typeof value === "number")
      return value.toLocaleString("ko-KR", { maximumFractionDigits: 6 });
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }
  return (
    <div className="message-viewer">
      <div className="view-switch" role="group" aria-label="메시지 보기 형식">
        <button aria-pressed={table} onClick={() => setTable(true)}>
          표로 보기
        </button>
        <button aria-pressed={!table} onClick={() => setTable(false)}>
          JSON으로 보기
        </button>
      </div>
      {!samples.length ? (
        <p className="note">아직 생성·보관된 메시지가 없습니다.</p>
      ) : !table ? (
        <pre>
          {JSON.stringify(
            samples.map((sample) => sample.message),
            null,
            2,
          )}
        </pre>
      ) : (
        samples.map((sample) => {
          const profile = catalog.profiles[String(sample.message.ty)];
          const rows: [string, string, unknown][] = [
            ["ty", "메시지 종류", profile?.title || sample.message.ty],
            ["ts", "메시지 시각", sample.message.ts],
            ...Object.entries(sample.message.pld).map(
              ([key, value]) =>
                [
                  key,
                  profile?.schema.properties.pld.properties[key]?.title ||
                    "규격 설명 없음",
                  value,
                ] as [string, string, unknown],
            ),
          ];
          return (
            <section
              className="message-card"
              key={sample.vehicle_id + ":" + sample.sequence}
            >
              <h4>
                {sample.vehicle_id} · {profile?.title || "메시지"} · 순번{" "}
                {sample.sequence}
              </h4>
              <p className="note">
                Client <code>{sample.client_id}</code>
                <br />
                토픽 <code>{sample.topic}</code>
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>항목</th>
                      <th>필드</th>
                      <th>값</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(([key, title, value]) => (
                      <tr key={key}>
                        <td>{title}</td>
                        <td>
                          <code>{key}</code>
                        </td>
                        <td title={String(value)}>{display(key, value)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          );
        })
      )}
      {!!samples.length && (
        <details>
          <summary>로컬 부가 정보 보기 · MQTT 전송 제외</summary>
          <p className="note">
            차량·Client·참고 토픽·순번·상황 등 로컬 확인용 정보입니다. 재생성
            조회의 토픽은 당시 사용한 전송 토픽을 보장하지 않습니다. MQTT에는 각
            항목의 message 내용만 전송합니다.
          </p>
          <pre>
            {JSON.stringify(
              samples.map(({ message: _message, ...metadata }) => metadata),
              null,
              2,
            )}
          </pre>
        </details>
      )}
      <p className="note">
        표는 한국어 항목명과 소수 최대 6자리로 표시합니다. 미확정 단위 표시는
        개발 규격을 따르며 JSON 원본과 전송 내용은 바꾸지 않습니다.
      </p>
    </div>
  );
}
