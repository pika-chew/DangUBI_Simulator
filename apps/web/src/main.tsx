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
type Sample = Assignment & {
  sequence: number;
  topic: string;
  message: Message;
};
type Result = {
  run_id: string;
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
};
type SavedRun = {
  id: string;
  created_at: string;
  request: { config: Config; message: Message };
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
      api<{ worker_ready: boolean }>("/api/health"),
      api<SavedRun[]>("/api/runs"),
    ])
      .then(([c, h, r]) => {
        setCatalog(c);
        setRaw(JSON.stringify(c.profiles["4"].example, null, 2));
        setHistory(r);
        setHealth(h.worker_ready ? "Worker 준비 완료" : "Worker 준비 필요");
      })
      .catch((e) => setError(String(e.message)));
  }, []);
  useEffect(() => {
    setPlan(null);
  }, [raw, config]);
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
        message: JSON.parse(raw),
        transport: "dry_run",
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
          <p>수동 입력부터 Worker 전달까지, 연결 방식별 배정을 확인합니다.</p>
        </div>
        <span className="status">{health}</span>
      </header>
      <main>
        <div className="banner">
          <b>송신 없는 확인 모드</b>
          <span>
            AWS 연결·MQTT 발행은 하지 않습니다. 생성 주기는 가상시간에만
            적용되며 데이터 값은 수동 입력값을 사용합니다.
          </span>
        </div>
        <nav aria-label="데이터 흐름">
          <span>수동 OBD 입력</span>
          <i>→</i>
          <span>JSON 미리보기</span>
          <i>→</i>
          <span>제어 API</span>
          <i>→</i>
          <span>Go Worker</span>
          <i>→</i>
          <span className="offline">IoT Core · 미연결</span>
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
                        value={config.messages_per_vehicle}
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
                      config.vehicle_count * config.messages_per_vehicle
                    ).toLocaleString()}
                    건
                  </b>{" "}
                  · 한 실행 최대 10,000건. 유지 정책은 기록만 하며 접속·해제
                  동작은 아직 재현하지 않습니다.
                </div>
              </section>
              <section className="card">
                <h2>
                  <em>02</em> 수동 OBD 데이터
                </h2>
                <p className="note">
                  {catalog.status}. 단위·자료형이 불명확한 필드는 표시된 개발
                  기준으로만 확인합니다.
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
                            !!message?.pld && Object.hasOwn(message.pld, key);
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
                              value={enabled ? String(message!.pld[key]) : ""}
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
              </section>
            </div>
            <div>
              <section className="card">
                <h2>
                  <em>03</em> JSON 미리보기
                </h2>
                <p className="note">
                  필드 입력과 JSON은 함께 갱신됩니다. 아래에서 직접 편집할 수
                  있습니다. 시각은 UTC 밀리초입니다.
                </p>
                <textarea
                  aria-label="OBD JSON"
                  spellCheck={false}
                  disabled={busy}
                  value={raw}
                  onChange={(e) => setRaw(e.target.value)}
                />
                <div className="actions">
                  <button disabled={busy} onClick={() => execute(true)}>
                    입력·배정 확인
                  </button>
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() => execute(false)}
                  >
                    {busy ? "처리 중…" : "Worker 생성 확인"}
                  </button>
                </div>
                <p className="note">
                  여러 차량에 입력값을 복제하고 차량별 운행 번호와 시각을
                  갱신합니다. 상태 기반 자동 주행은 후속 기능입니다.
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
                    <span className="tag">송신 없는 생성 확인 완료</span>
                    <p className="note">
                      실행 ID <code>{run.id}</code>
                      <br />
                      확인 시각{" "}
                      {new Date(run.created_at).toLocaleString("ko-KR")}
                    </p>
                    <div className="metrics">
                      {[
                        ["생성 확인", run.result.processed_messages + "건"],
                        ["배정 Client", run.result.allocated_clients + "개"],
                        ["실제 연결", run.result.connected_clients + "개"],
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
                      <pre>{JSON.stringify(run.request.config, null, 2)}</pre>
                    </details>
                  </>
                )}
              </section>
              <section className="card">
                <h2>최근 실행</h2>
                <p className="note">
                  최대 30개 조회 · 로컬 실행 기록이며 최신 입력값과 별개입니다.
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
