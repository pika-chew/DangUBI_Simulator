import { useEffect, useState } from "react";
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
  profile: "urban" | "highway" | "congested";
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
  model_version?: string;
  microtrip_count?: number;
  trip_count?: number;
};
type SavedRun = {
  id: string;
  created_at: string;
  request: { config: Config; message?: Message; generation?: Generation };
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
function App() {
  const [mode, setMode] = useState<"manual" | "automatic">("manual");
  const [generation, setGeneration] = useState<Generation>({
    profile: "urban",
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
      api<{ worker_ready: boolean; mqtt_ready: boolean }>("/api/health"),
      api<SavedRun[]>("/api/runs"),
    ])
      .then(([c, h, r]) => {
        setCatalog(c);
        setRaw(JSON.stringify(c.profiles["4"].example, null, 2));
        setHistory(r);
        setMqttReady(h.mqtt_ready);
        setHealth(h.worker_ready ? "Worker 준비 완료" : "Worker 준비 필요");
      })
      .catch((e) => setError(String(e.message)));
  }, []);
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
      };
      if (preview) {
        const data = await api<{ plan: Result }>("/api/preview", payload);
        setPlan(data.plan);
      } else {
        const saved = await api<SavedRun>("/api/runs", payload);
        setRun(saved);
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
              ? "접속·발행하지 않고 입력과 배정만 확인합니다. 생성 주기는 가상시간에만 적용됩니다."
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
                <fieldset disabled={busy}>
                  <div className="fields">
                    <label>
                      데이터 생성 방식
                      <select
                        value={mode}
                        onChange={(e) =>
                          setMode(e.target.value as "manual" | "automatic")
                        }
                      >
                        <option value="manual">수동 입력</option>
                        <option value="automatic">자동 주행 생성</option>
                      </select>
                    </label>
                    <label>
                      전송 방식
                      <select
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

                    <label>
                      차량 수
                      <input
                        type="number"
                        min="1"
                        max="10000"
                        value={config.vehicle_count}
                        onChange={(e) =>
                          changeConfig("vehicle_count", e.target.value)
                        }
                      />
                    </label>
                    <label>
                      연결 방식
                      <select
                        value={config.mapping_mode}
                        onChange={(e) =>
                          changeConfig("mapping_mode", e.target.value)
                        }
                      >
                        <option value="shared">연결 풀</option>
                        <option value="dedicated">차량별 전체 연결</option>
                      </select>
                    </label>
                    <label>
                      Client 수
                      <input
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
                    <label>
                      연결 유지 정책
                      <select
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
                      차량당 생성 건수
                      <input
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
                    <label>
                      생성 주기 (밀리초)
                      <input
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
                      차량별 운행 한 회를 생성합니다. 웜업 → 가속 → 정속 → 감속
                      → 정차 → 재출발 → 운행 종료 순서입니다. 실차 보정 전 근사
                      모델이며 실제 도로를 따르지 않습니다.
                    </p>
                    <fieldset disabled={busy}>
                      <label>
                        운행 프로필
                        <select
                          value={generation.profile}
                          onChange={(e) =>
                            setGeneration({
                              ...generation,
                              profile: e.target.value as Generation["profile"],
                            })
                          }
                        >
                          <option value="urban">
                            도심 · 기준 최고속도 50km/h
                          </option>
                          <option value="highway">
                            고속 · 기준 최고속도 95km/h
                          </option>
                          <option value="congested">
                            정체 · 기준 최고속도 20km/h
                          </option>
                        </select>
                      </label>
                      <div className="fields">
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
                      생성 주기는 운행시간을 나누어 떨어지게 하고 최소 10구간을
                      확보해야 합니다. 시작·종료 Microtrip과 종료 Trip을 포함해
                      건수를 계산합니다. 같은 seed·시각·조건에서 같은 데이터를
                      생성합니다. 짧은 운행은 가속 한계에 따라 최고속도가
                      낮아집니다.
                    </p>
                    <p className="note">
                      개발 기준: 속도 km/h, 거리 m, 연료 누적 L, 탄소 g, 시간
                      필드 초. 일부 단위는 수신 계약 미확정입니다. 프로필은 같은
                      상태 순서에서 기준 속도를 바꿉니다.
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
                    <fieldset disabled={busy}>
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
                  disabled={busy || mode === "automatic"}
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
                  <button disabled={busy} onClick={() => execute(true)}>
                    입력·배정 확인
                  </button>
                  <button
                    className="primary"
                    disabled={busy || (transport === "mqtt" && !confirmed)}
                    onClick={() => execute(false)}
                  >
                    {busy
                      ? "처리 중…"
                      : transport === "mqtt"
                        ? "MQTT 연결·발행"
                        : "Worker 생성 확인"}
                  </button>
                </div>
                <p className="note">
                  {mode === "automatic"
                    ? "운행 조건으로 Microtrip과 Trip을 함께 생성합니다. 송신 없는 확인은 가상시간으로 즉시 실행합니다."
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
                    <span className="tag">
                      {run.result.state === "mqtt_completed"
                        ? "MQTT 송신 완료"
                        : run.result.state === "mqtt_failed"
                          ? "MQTT 송신 실패·부분 완료"
                          : "송신 없는 생성 확인 완료"}
                    </span>
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
                    <div className="metrics">
                      {[
                        ["생성 확인", run.result.processed_messages + "건"],
                        ["Microtrip", (run.result.microtrip_count ?? 0) + "건"],
                        ["Trip", (run.result.trip_count ?? 0) + "건"],
                        ["배정 Client", run.result.allocated_clients + "개"],
                        [
                          "접속 확인 Client",
                          run.result.connected_clients + "개",
                        ],
                        [
                          "종료 후 연결",
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
                          <span>{title}</span>
                          <b>{value}</b>
                        </div>
                      ))}
                    </div>
                    {!!run.result.trace?.length && (
                      <DrivingResult result={run.result} />
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
                      <pre>{JSON.stringify(run.result.samples, null, 2)}</pre>
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
                {history.length === 0 ? (
                  <p>아직 실행 기록이 없습니다.</p>
                ) : (
                  history.map((h) => (
                    <button
                      className="history"
                      key={h.id}
                      disabled={busy}
                      onClick={() => load(h.id)}
                    >
                      <span>
                        {new Date(h.created_at).toLocaleString("ko-KR")}
                        <br />
                        <code>{h.id.slice(0, 12)}</code>
                      </span>
                      <b>
                        {h.result.assignment_count}대 / Client{" "}
                        {h.result.allocated_clients}개 /{" "}
                        {h.result.processed_messages}건
                      </b>
                    </button>
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

function DrivingResult({ result }: { result: Result }) {
  const trace = result.trace!;
  const duration = trace[trace.length - 1].elapsed_seconds || 1;
  const maxSpeed = Math.max(10, ...trace.map((p) => p.speed_kmh));
  const points = trace
    .map(
      (p) =>
        `${48 + (p.elapsed_seconds / duration) * 510},${170 - (p.speed_kmh / maxSpeed) * 130}`,
    )
    .join(" ");
  return (
    <>
      <h3>첫 차량의 속도·상태 변화</h3>
      <p className="note">
        v000001 · 생성 모델 {result.model_version} · 생성 계획 기준이며 전송
        성공 건수와 별개입니다.
      </p>
      <svg
        className="drive-chart"
        viewBox="0 0 600 210"
        role="img"
        aria-label="첫 차량 속도 변화 그래프"
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
          0초
        </text>
        <text x="515" y="197">
          {duration}초
        </text>
      </svg>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>경과 (초)</th>
              <th>상태</th>
              <th>속도 (km/h)</th>
              <th>RPM</th>
              <th>누적거리 (m)</th>
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
              </tr>
            ))}
          </tbody>
        </table>
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
        <pre>{JSON.stringify(result.trip_summaries, null, 2)}</pre>
      </details>
    </>
  );
}
