import React, { useEffect, useState, useId, FormEvent } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowUpRight,
  Box,
  Check,
  ChevronRight,
  Clock,
  Copy,
  Database,
  Download,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Network,
  Plus,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  Users,
  X,
  Zap,
} from "lucide-react";
import "./style.css";

type Row = Record<string, any>;
let csrf = "";
async function api(path: string, body?: unknown, method?: string) {
  const result = await fetch("/api" + path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await result.json();
  if (!result.ok) throw new Error(data.error?.message || "请求失败");
  return data;
}
const amount = (n: any) =>
  n == null
    ? "未知"
    : (n / 1000000).toLocaleString("zh-CN", { maximumFractionDigits: 4 });
const tokens = (n: any) =>
  n == null
    ? "—"
    : n >= 1000000
      ? (n / 1000000).toFixed(2) + "M"
      : n.toLocaleString("zh-CN");
const DONUT_COLORS = ["#2f6bff", "#0ea5e9", "#7c9bff", "#1e40af", "#a5c0ff", "#94a3b8"];
function DonutCard({
  title,
  items,
  unit,
}: {
  title: string;
  items: { name: string; value: number }[];
  unit: string;
}) {
  const head = items.filter((i) => i.value > 0).slice(0, 5);
  const tail = items.filter((i) => i.value > 0).slice(5);
  const shown = tail.length
    ? [
        ...head,
        { name: "其他", value: tail.reduce((sum, i) => sum + i.value, 0) },
      ]
    : head;
  const total = shown.reduce((sum, i) => sum + i.value, 0);
  const radius = 46;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  return (
    <section className="panel donut-card">
      <div className="panel-title">
        <h3>{title}</h3>
      </div>
      {shown.length ? (
        <div className="donut-body">
          <svg viewBox="0 0 120 120" className="donut" role="img" aria-label={title}>
            <circle
              cx="60"
              cy="60"
              r={radius}
              fill="none"
              stroke="#eef2f8"
              strokeWidth="17"
            />
            {shown.map((item, index) => {
              const length = total ? (item.value / total) * circumference : 0;
              const node = (
                <circle
                  key={item.name}
                  cx="60"
                  cy="60"
                  r={radius}
                  fill="none"
                  stroke={DONUT_COLORS[index % DONUT_COLORS.length]}
                  strokeWidth="17"
                  strokeDasharray={`${length} ${circumference - length}`}
                  strokeDashoffset={-offset}
                  transform="rotate(-90 60 60)"
                />
              );
              offset += length;
              return node;
            })}
          </svg>
          <div className="donut-legend">
            {shown.map((item, index) => (
              <div key={item.name} title={item.name}>
                <i
                  style={{ background: DONUT_COLORS[index % DONUT_COLORS.length] }}
                />
                <span>{item.name}</span>
                <b>
                  {item.value.toLocaleString()}
                  {unit}
                </b>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <Empty text="该时间段没有数据" />
      )}
    </section>
  );
}
function TrendChart({ series }: { series: Row[] }) {
  if (!series.length) return <Empty text="该时间段没有请求" />;
  const width = 660;
  const height = 170;
  const pad = 10;
  const peak = Math.max(
    1,
    ...series.map((p) => Math.max(p.input_tokens || 0, p.output_tokens || 0)),
  );
  // A single bucket has no horizontal span, so a line/area path would collapse to
  // zero width. Draw paired columns instead of an invisible chart.
  if (series.length === 1) {
    const row = series[0];
    const baseline = height - pad;
    const bar = (value: number, offset: number, fill: string) => {
      const top = baseline - ((value || 0) / peak) * (height - pad * 2);
      return (
        <rect
          key={fill}
          x={width / 2 + offset}
          y={top}
          width="16"
          height={Math.max(1, baseline - top)}
          rx="3"
          fill={fill}
        />
      );
    };
    return (
      <div className="trend">
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="Token 使用趋势">
          <line x1={pad} y1={baseline} x2={width - pad} y2={baseline} stroke="#e6ebf3" strokeWidth="1" />
          {bar(row.input_tokens, -20, "#2f6bff")}
          {bar(row.output_tokens, 4, "#0ea5e9")}
        </svg>
        <div className="trend-axis">
          <span>{date(row.slot)}</span>
          <span>峰值 {tokens(peak)}</span>
          <span>{row.requests} 次请求</span>
        </div>
      </div>
    );
  }
  const step = (width - pad * 2) / (series.length - 1);
  const point = (index: number, value: number) => [
    pad + index * step,
    height - pad - (value / peak) * (height - pad * 2),
  ];
  const path = (field: string) =>
    series
      .map((row, index) => {
        const [x, y] = point(index, row[field] || 0);
        return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  const area = (field: string) =>
    `${path(field)} L${(pad + (series.length - 1) * step).toFixed(1)},${height - pad} L${pad},${height - pad} Z`;
  return (
    <div className="trend">
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="Token 使用趋势">
        <path d={area("input_tokens")} fill="rgba(47,107,255,0.14)" />
        <path d={area("output_tokens")} fill="rgba(14,165,233,0.18)" />
        <path d={path("input_tokens")} fill="none" stroke="#2f6bff" strokeWidth="2" />
        <path d={path("output_tokens")} fill="none" stroke="#0ea5e9" strokeWidth="2" />
      </svg>
      <div className="trend-axis">
        <span>{date(series[0].slot)}</span>
        <span>峰值 {tokens(peak)}</span>
        <span>{date(series[series.length - 1].slot)}</span>
      </div>
    </div>
  );
}
const date = (n: any) =>
  n ? new Date(n * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
function keyScope(k: Row): string[] {
  try {
    const parsed = JSON.parse(k.models || "[]");
    return Array.isArray(parsed)
      ? parsed.filter((m: unknown) => typeof m === "string")
      : [];
  } catch {
    return [];
  }
}
const states: Row = {
  ready: "可用",
  disabled: "已停用",
  reauth_required: "需要授权",
  cooldown: "冷却中",
  exhausted: "额度耗尽",
  pending: "进行中",
  success: "成功",
  failed: "失败",
  interrupted: "中断",
};
const pages = [
  ["overview", "总览", LayoutDashboard],
  ["accounts", "账号管理", Users],
  ["pools", "账号池", Network],
  ["models", "模型中心", Box],
  ["keys", "API Keys", KeyRound],
  ["logs", "用量与日志", Activity],
  ["settings", "系统设置", Settings],
] as const;
function Badge({ value }: { value: string }) {
  return (
    <span
      className={
        "badge " +
        (["ready", "success"].includes(value)
          ? "good"
          : ["failed", "reauth_required", "interrupted"].includes(value)
            ? "bad"
            : "neutral")
      }
    >
      {states[value] || value}
    </span>
  );
}
function Empty({ text = "暂无记录" }: { text?: string }) {
  return (
    <div className="empty">
      <Database size={30} />
      <strong>{text}</strong>
      <span>完成账号授权与同步后，真实数据将显示在这里。</span>
    </div>
  );
}
function Field({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const id = useId();
  return (
    <label className="field">
      <span id={id}>{title}</span>
      {React.cloneElement(children as React.ReactElement<any>, {
        "aria-labelledby": id,
      })}
    </label>
  );
}
function Table({
  headers,
  children,
}: {
  headers: string[];
  children: React.ReactNode;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {headers.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
function App() {
  const [signed, setSigned] = useState<boolean | null>(null),
    [page, setPage] = useState("overview"),
    [data, setData] = useState<Row>({
      accounts: [],
      pools: [],
      keys: [],
      logs: [],
      models: { models: [], aliases: [] },
      jobs: [],
      usage: null,
    }),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [modal, setModal] = useState(""),
    [selected, setSelected] = useState<Row | null>(null),
    [filter, setFilter] = useState(""),
    [keySecret, setKeySecret] = useState(""),
    [oauth, setOauth] = useState<Row | null>(null),
    [settings, setSettings] = useState<Row>({
      sync_interval: 900,
      retention_days: 30,
    });
  const [logFilters, setLogFilters] = useState<Row>({
      account_id: "",
      key_id: "",
      model: "",
      state: "",
      since: "",
      until: "",
    }),
    [logPage, setLogPage] = useState(0);
  const modelOptions: string[] = Array.from(
    new Set([
      ...data.models.models.map((m: Row) => String(m.model)),
      ...data.models.aliases.map((a: Row) => String(a.alias)),
    ]),
  ).sort();
  const fail = (error: any) => setNotice(error.message || String(error));
  async function load() {
    const [summary, accounts, pools, keys, models, logs, jobs, config, usage] =
      await Promise.all(
        [
          "/summary",
          "/accounts",
          "/pools",
          "/keys",
          "/models",
          "/logs",
          "/jobs",
          "/settings",
          "/usage",
        ].map((p) => api(p)),
      );
    setData({ summary, accounts, pools, keys, models, logs, jobs, usage });
    setSettings(config);
  }
  useEffect(() => {
    api("/session")
      .then((s) => {
        csrf = s.csrf;
        setSigned(true);
        load().catch(fail);
      })
      .catch(() => setSigned(false));
  }, []);
  useEffect(() => {
    if (!signed) return;
    const id = setInterval(() => {
      if (!busy && !modal && page !== "logs" && page !== "settings")
        load().catch(fail);
    }, 15000);
    return () => clearInterval(id);
  }, [signed, busy, modal, page]);
  useEffect(() => {
    if (!oauth || !modal) return;
    const id = setInterval(async () => {
      try {
        const r = await api("/oauth/" + oauth.login_id + "/poll", {});
        if (r.done) {
          setOauth(null);
          setModal("");
          setNotice("授权完成，账号已加入默认池");
          await load();
        }
      } catch (e) {
        fail(e);
        setOauth(null);
      }
    }, 3000);
    return () => clearInterval(id);
  }, [oauth, modal]);
  async function act(fn: () => Promise<any>, close = false) {
    setBusy(true);
    setNotice("");
    try {
      await fn();
      if (close) {
        setModal("");
        setSelected(null);
      }
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function login(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    try {
      const r = await api("/login", { password: f.get("password") });
      csrf = r.csrf;
      await load();
      setSigned(true);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  function open(name: string, row: Row | null = null) {
    setModal(name);
    setSelected(row);
    setNotice("");
  }
  async function removePool(p: Row) {
    if (
      !window.confirm(
        `删除账号池「${p.name}」？\n\n成员关系会解除，账号与历史记录保留。` +
          `\n如果仍有 API Key 指向该池，需要先改到其他池。`,
      )
    )
      return;
    await act(() => api("/pools/" + p.id, undefined, "DELETE"));
  }
  async function revealKey(k: Row) {
    try {
      const r = await api("/keys/" + k.id + "/secret");
      setKeySecret(r.key);
      setSelected(k);
      setModal("key-secret");
    } catch (e) {
      fail(e);
    }
  }
  async function rotateKey(k: Row) {
    if (
      !window.confirm(
        `重新生成「${k.name}」的密钥？\n\n旧密钥会立即失效，使用它的客户端需要更新配置。`,
      )
    )
      return;
    await act(async () => {
      const r = await api("/keys/" + k.id + "/rotate", {});
      setKeySecret(r.key);
    });
  }
  async function removeKey(k: Row) {
    if (
      !window.confirm(
        `删除 API Key「${k.name}」？\n\n该密钥立即失效且无法恢复。` +
          `\n历史请求日志与账本会保留，但不再关联到这个 Key。`,
      )
    )
      return;
    await act(() => api("/keys/" + k.id, undefined, "DELETE"));
  }
  async function detail(a: Row) {
    try {
      setSelected({ ...a, ...(await api("/accounts/" + a.id)) });
      setModal("detail");
    } catch (e) {
      fail(e);
    }
  }
  function filtersQuery(offset = 0) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(logFilters)) {
      if (v)
        q.set(
          k,
          k === "since" || k === "until"
            ? String(new Date(v).getTime() / 1000)
            : String(v),
        );
    }
    q.set("offset", String(offset));
    q.set("limit", "50");
    return q.toString();
  }
  async function applyLogFilters(offset = 0) {
    try {
      setData((d) => ({ ...d, logs: [] }));
      const rows = await api("/logs?" + filtersQuery(offset));
      setData((d) => ({ ...d, logs: rows }));
      setLogPage(offset);
    } catch (e) {
      fail(e);
    }
  }
  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setNotice("已复制");
    } catch {
      setNotice("浏览器未允许复制，请手动选择文本");
    }
  }
  if (signed === null) return <div className="loading">正在连接本地服务…</div>;
  if (!signed)
    return (
      <main className="login-layout">
        <div className="login-story">
          <div className="brand">
            <span className="brand-icon">
              <Network />
            </span>
            WorkBuddy Hub
          </div>
          <h1>
            让每一次调用，
            <br />
            都有迹可循。
          </h1>
          <p>账号、模型与额度，在你的本地工作台集中管理。</p>
          <div className="story-line">
            <ShieldCheck />
            本机运行 · 凭据加密 · 用量可追溯
          </div>
        </div>
        <form className="login-card" onSubmit={login}>
          <span className="eyebrow">YOUR LOCAL WORKSPACE</span>
          <h2>进入管理工作台</h2>
          <p>首次密码保存在数据目录的 initial-admin.txt，启动时会自动打开。</p>
          <Field title="管理员密码">
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              required
              autoFocus
            />
          </Field>
          {notice && (
            <div className="notice" role="alert">
              {notice}
            </div>
          )}
          <button className="primary" disabled={busy}>
            {busy ? "正在登录…" : "登录工作台"}
            <ArrowUpRight size={17} />
          </button>
          <small>默认入口 http://127.0.0.1:8792</small>
        </form>
      </main>
    );
  const summary = data.summary || {},
    stats = summary.last24h || {},
    usage: Row = (data.usage as Row) || {},
    usageTotals: Row = usage.totals || {};
  const filtered = data.accounts.filter((a: Row) =>
    (a.name + " " + a.uid).toLowerCase().includes(filter.toLowerCase()),
  );
  const current = pages.find((p) => p[0] === page)!;
  return (
    <div className="layout">
      <aside>
        <div className="brand">
          <span className="brand-icon">
            <Network size={22} />
          </span>
          <div>
            WorkBuddy<span className="brand-sub">HUB / 本地工作台</span>
          </div>
        </div>
        <div className="workspace">
          <span className="dot" />
          个人工作空间<span className="local">LOCAL</span>
        </div>
        <div className="nav-label">工作台</div>
        <nav>
          {pages.map(([id, label, Icon]) => (
            <button
              key={id}
              className={page === id ? "active" : ""}
              onClick={() => {
                setPage(id);
                setFilter("");
              }}
            >
              <Icon size={18} />
              {label}
              {page === id && <ChevronRight size={14} />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <ShieldCheck size={17} />
          <span>
            数据保存在你的电脑
            <br />
            <small>WorkBuddy Hub v{settings.version || "0.1.0"}</small>
          </span>
        </div>
      </aside>
      <div className="main">
        <header>
          <div className="breadcrumb">
            工作空间 <ChevronRight size={13} /> <strong>{current[1]}</strong>
          </div>
          <div className="header-right">
            <span className="status">
              <span className="dot" />
              服务运行中
            </span>
            <button
              className="icon-button"
              aria-label="刷新数据"
              disabled={busy}
              onClick={() => act(load)}
            >
              <RefreshCw size={17} />
            </button>
            <button
              className="icon-button"
              aria-label="退出登录"
              onClick={() =>
                act(async () => {
                  await api("/logout", {});
                  setSigned(false);
                  csrf = "";
                })
              }
            >
              <LogOut size={17} />
            </button>
          </div>
        </header>
        <main>
          <div className="page-title">
            <div>
              <span className="eyebrow">WORKBUDDY / {page.toUpperCase()}</span>
              <h1>{current[1]}</h1>
              <p>
                {
                  {
                    overview: "掌握账号池状态，追踪每一次调用与积分变化。",
                    accounts: "集中管理授权、有效期和各账号的积分资源。",
                    pools: "按用途组织账号，让调度规则清晰可控。",
                    models: "实际目录、能力与别名，统一管理。",
                    keys: "为每个客户端设置独立权限和请求预算。",
                    logs: "从一次请求追溯到账号、用量和执行结果。",
                    settings: "管理同步任务、备份和本地运行设置。",
                  }[page]
                }
              </p>
            </div>
            {page === "accounts" && (
              <button className="primary" onClick={() => open("account")}>
                <Plus size={17} />
                添加账号
              </button>
            )}
            {page === "pools" && (
              <button className="primary" onClick={() => open("pool")}>
                <Plus size={17} />
                创建账号池
              </button>
            )}
            {page === "keys" && (
              <button className="primary" onClick={() => open("key")}>
                <Plus size={17} />
                创建 API Key
              </button>
            )}
            {page === "models" && (
              <button className="primary" onClick={() => open("alias")}>
                <Plus size={17} />
                添加模型别名
              </button>
            )}
          </div>
          {notice && (
            <div className="notice" role="alert">
              {notice}
              <button aria-label="关闭提示" onClick={() => setNotice("")}>
                <X size={15} />
              </button>
            </div>
          )}
          {page === "overview" && (
            <>
              <div className="hero">
                <div>
                  <span className="eyebrow">一处连接，自如调用</span>
                  <h2>你的模型服务，尽在掌握。</h2>
                  <p>从账号授权到调用记录，保持清晰、可控的工作流。</p>
                  <button onClick={() => setPage("accounts")}>
                    管理我的账号 <ArrowUpRight size={16} />
                  </button>
                </div>
                <div className="hero-art">
                  <div className="orbit">
                    <Network size={45} />
                  </div>
                  <span className="art-chip one">
                    <Check size={13} />
                    统一接入
                  </span>
                  <span className="art-chip two">
                    <Activity size={13} />
                    用量追踪
                  </span>
                </div>
              </div>
              <div className="metrics">
                <button onClick={() => setPage("accounts")}>
                  <Users />
                  <span>可用账号</span>
                  <strong>
                    {summary.ready || 0}
                    <em> / {summary.accounts || 0}</em>
                  </strong>
                  <small>按授权、冷却与余额状态统计</small>
                </button>
                <button onClick={() => setPage("accounts")}>
                  <Zap />
                  <span>已知剩余积分</span>
                  <strong>{amount(summary.balance_micro || 0)}</strong>
                  <small>
                    {summary.unknown_balances || 0} 个账号未知 ·{" "}
                    {summary.stale_balances || 0} 个快照过期
                  </small>
                </button>
                <button onClick={() => setPage("accounts")}>
                  <Clock />
                  <span>7 天内到期积分</span>
                  <strong>{amount(summary.expiring_micro || 0)}</strong>
                  <small>来自已同步的积分包</small>
                </button>
                <button onClick={() => setPage("logs")}>
                  <Activity />
                  <span>近 24 小时请求</span>
                  <strong>{stats.requests || 0}</strong>
                  <small>
                    成功 {stats.successes || 0} · 中断 {stats.interrupted || 0}
                  </small>
                </button>
              </div>
              <div className="split">
                <section className="panel">
                  <div className="panel-title">
                    <h3>请求趋势</h3>
                    <span>最近 14 天 · UTC+8</span>
                  </div>
                  {summary.daily?.length ? (
                    <div className="chart">
                      {summary.daily.map((d: Row) => (
                        <div className="bar-group" key={d.day}>
                          <strong>{d.requests}</strong>
                          <div
                            className="bar"
                            style={{
                              height: Math.max(
                                5,
                                (d.requests /
                                  Math.max(
                                    ...summary.daily.map(
                                      (v: Row) => v.requests,
                                    ),
                                  )) *
                                  120,
                              ),
                            }}
                          />
                          <small>{d.day.slice(5)}</small>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <Empty text="还没有调用记录" />
                  )}
                </section>
                <section className="panel">
                  <div className="panel-title">
                    <h3>需要关注</h3>
                    <span>{summary.alerts?.length || 0} 项</span>
                  </div>
                  {summary.alerts?.length ? (
                    summary.alerts.slice(0, 5).map((a: Row) => (
                      <button
                        className="alert-row"
                        key={a.id}
                        onClick={() => detail(a)}
                      >
                        <div>
                          <strong>{a.name}</strong>
                          <small>{a.last_error || states[a.state]}</small>
                        </div>
                        <Badge value={a.state} />
                      </button>
                    ))
                  ) : (
                    <div className="healthy">
                      <ShieldCheck size={32} />
                      <strong>当前没有账号异常</strong>
                      <p>授权过期、冷却与同步失败会显示在这里。</p>
                    </div>
                  )}
                </section>
              </div>
              <div className="info-strip">
                <Database size={18} />
                <span>
                  额度与用量分开记录。余额是上游快照，Token 与积分不会互相换算。
                </span>
                <button onClick={() => setPage("logs")}>
                  查看记录 <ArrowUpRight size={14} />
                </button>
              </div>
            </>
          )}
          {page === "accounts" && (
            <section className="panel">
              <div className="toolbar">
                <div className="search">
                  <Search size={16} />
                  <input
                    placeholder="搜索账号名称或 UID"
                    aria-label="搜索账号"
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                  />
                </div>
                <span>{filtered.length} 个账号</span>
              </div>
              {filtered.length ? (
                <Table
                  headers={[
                    "账号",
                    "状态",
                    "剩余积分",
                    "最近同步",
                    "并发",
                    "操作",
                  ]}
                >
                  {filtered.map((a: Row) => (
                    <tr key={a.id}>
                      <td>
                        <button className="text-link" onClick={() => detail(a)}>
                          {a.name}
                        </button>
                        <small>
                          {a.provider === "workbuddy_cn"
                            ? "WorkBuddy 国内版"
                            : "OpenAI 兼容"}{" "}
                          · {a.uid.slice(0, 14)}
                        </small>
                      </td>
                      <td>
                        <Badge value={a.state} />
                        {a.last_error && (
                          <small className="error-text">{a.last_error}</small>
                        )}
                      </td>
                      <td>
                        <strong>{amount(a.balance_micro)}</strong>
                        {a.balance_stale && <small>快照待更新</small>}
                      </td>
                      <td>{date(a.balance_at)}</td>
                      <td>
                        {a.inflight} / {a.max_concurrency}
                      </td>
                      <td>
                        <div className="row-actions">
                          <button
                            disabled={busy}
                            onClick={() =>
                              act(async () => {
                                const r = await api(
                                  "/accounts/" + a.id + "/sync",
                                  {},
                                );
                                setNotice(
                                  r.ok
                                    ? "模型与余额已同步"
                                    : r.errors.join("；"),
                                );
                              })
                            }
                          >
                            同步
                          </button>
                          <button onClick={() => open("account-edit", a)}>
                            设置
                          </button>
                          <button
                            disabled={busy}
                            onClick={() =>
                              act(() =>
                                api(
                                  "/accounts/" + a.id,
                                  { enabled: !a.enabled },
                                  "PATCH",
                                ),
                              )
                            }
                          >
                            {a.enabled ? "停用" : "启用"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </Table>
              ) : (
                <Empty text="添加你的第一个账号" />
              )}
            </section>
          )}
          {page === "pools" && (
            <div className="pool-grid">
              {data.pools.map((p: Row) => (
                <section className="panel pool-card" key={p.id}>
                  <div className="pool-icon">
                    <Network />
                  </div>
                  <h3>{p.name}</h3>
                  <p>
                    {p.strategy === "expiring" ? "临期积分优先" : "均衡使用"} ·
                    会话保持同一账号
                  </p>
                  <strong>
                    {p.account_ids.length}
                    <small> 个成员账号</small>
                  </strong>
                  <div className="member-list">
                    {p.account_ids.map((id: number) => (
                      <span key={id}>
                        {data.accounts.find((a: Row) => a.id === id)?.name ||
                          id}
                      </span>
                    ))}
                  </div>
                  <div className="pool-actions">
                    <button onClick={() => open("pool", p)}>
                      管理成员与策略 <ChevronRight size={15} />
                    </button>
                    <button
                      className="danger"
                      title={
                        p.id === 1
                          ? "默认账号池不能删除"
                          : "删除账号池，成员账号本身保留"
                      }
                      disabled={busy || p.id === 1}
                      onClick={() => removePool(p)}
                    >
                      删除
                    </button>
                  </div>
                </section>
              ))}
            </div>
          )}
          {page === "models" && (
            <>
              <section className="panel">
                <div className="panel-title">
                  <h3>模型目录</h3>
                  <button onClick={() => open("playground")}>
                    测试调用 <ArrowUpRight size={15} />
                  </button>
                </div>
                {data.models.models.length ? (
                  <Table
                    headers={["模型 ID", "来源账号", "能力声明", "验证状态"]}
                  >
                    {data.models.models.map((m: Row) => {
                      const c = JSON.parse(m.capabilities);
                      return (
                        <tr key={m.account_id + m.model}>
                          <td>
                            <code>{m.model}</code>
                          </td>
                          <td>{m.account_name}</td>
                          <td>
                            <span className="badge good">聊天 / 流式</span>{" "}
                            {c.tools && (
                              <span className="badge neutral">工具</span>
                            )}{" "}
                            {c.vision && (
                              <span className="badge neutral">图片</span>
                            )}
                            {c.efforts?.length ? (
                              <>
                                {" "}
                                <span
                                  className="badge neutral"
                                  title={
                                    "上游声明的推理档位；请求的档位若被上游以 11150 拒绝，会自动降一档重试一次" +
                                    (c.default_effort
                                      ? `，默认 ${c.default_effort}`
                                      : "")
                                  }
                                >
                                  档位 {c.efforts.join(" / ")}
                                </span>
                              </>
                            ) : null}
                          </td>
                          <td>
                            {m.verified ? (
                              <>
                                <Badge value="已调用成功" />
                                <small>{date(m.verified)}</small>
                              </>
                            ) : (
                              <span className="muted">
                                仅目录声明，待实际调用
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </Table>
                ) : (
                  <Empty text="同步账号以获取模型目录" />
                )}
              </section>
              <section className="panel spaced">
                <div className="panel-title">
                  <h3>模型别名</h3>
                  <span>客户端名称 → 实际模型</span>
                </div>
                {data.models.aliases.map((a: Row) => (
                  <div className="alias-row" key={a.alias}>
                    <code>{a.alias}</code>
                    <ChevronRight size={16} />
                    <code>{a.model}</code>
                  </div>
                ))}
                {!data.models.aliases.length && (
                  <p className="padded muted">尚未设置别名。</p>
                )}
              </section>
              <p className="footnote">
                Chat 支持文本及模型声明的工具、图片能力；Responses
                支持无状态文本和函数工具；Messages
                支持文本、工具和图片。未知字段会明确报错。
              </p>
            </>
          )}
          {page === "keys" && (
            <>
              <div className="connection">
                <KeyRound size={20} />
                <div>
                  <strong>统一 API 地址</strong>
                  <code>{location.origin}/v1</code>
                </div>
                <button
                  aria-label="复制 API 地址"
                  onClick={() => copy(location.origin + "/v1")}
                >
                  <Copy size={16} />
                </button>
              </div>
              <section className="panel">
                {data.keys.length ? (
                  <Table
                    headers={[
                      "名称 / 前缀",
                      "账号池",
                      "可用模型",
                      "请求预算",
                      "速率 / 并发",
                      "有效期",
                      "状态",
                      "操作",
                    ]}
                  >
                    {data.keys.map((k: Row) => (
                      <tr key={k.id}>
                        <td>
                          <strong>{k.name}</strong>
                          <small>
                            <code>{k.prefix}…</code>
                          </small>
                        </td>
                        <td>
                          {
                            data.pools.find((p: Row) => p.id === k.pool_id)
                              ?.name
                          }
                        </td>
                        <td>
                          {(() => {
                            const scope = keyScope(k);
                            if (!scope.length)
                              return <span className="muted">全部</span>;
                            return (
                              <span title={scope.join("、")}>
                                {scope.length === 1 ? (
                                  <code>{scope[0]}</code>
                                ) : (
                                  `${scope.length} 个模型`
                                )}
                              </span>
                            );
                          })()}
                        </td>
                        <td>
                          {k.used} / {k.request_limit ?? "不限"}
                          <small>{k.reserved} 进行中 · 单位：请求</small>
                        </td>
                        <td>
                          {k.rpm}/分钟 · {k.max_concurrency} 并发
                        </td>
                        <td>{date(k.expires)}</td>
                        <td>
                          <Badge value={k.enabled ? "ready" : "disabled"} />
                        </td>
                        <td>
                          <div className="row-actions">
                            <button
                              disabled={busy}
                              onClick={() => revealKey(k)}
                            >
                              查看密钥
                            </button>
                            <button
                              disabled={busy}
                              onClick={() => open("key-models", k)}
                            >
                              可用模型
                            </button>
                            <button
                              disabled={busy}
                              onClick={() =>
                                act(() =>
                                  api(
                                    "/keys/" + k.id,
                                    { enabled: !k.enabled },
                                    "PATCH",
                                  ),
                                )
                              }
                            >
                              {k.enabled ? "停用" : "启用"}
                            </button>
                            <button
                              className="danger"
                              title="删除后密钥立即失效，历史日志保留但不再关联"
                              disabled={busy}
                              onClick={() => removeKey(k)}
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </Table>
                ) : (
                  <Empty text="为客户端创建独立 API Key" />
                )}
              </section>
              <p className="footnote">
                密钥明文加密保存在本机，可在列表中随时查看与复制，查看会写入审计。
                本地预算按请求计数，与 WorkBuddy
                的真实积分余额独立。
              </p>
            </>
          )}
          {page === "logs" && (
            <>
              <div className="metrics">
                <div>
                  <span>总请求数</span>
                  <strong>{usage.requests ?? 0}</strong>
                  <small>成功 {usageTotals.successes ?? 0}</small>
                </div>
                <div>
                  <span>总 Token</span>
                  <strong>{tokens(usageTotals.tokens_total)}</strong>
                  <small>
                    输入 {tokens(usageTotals.input_tokens)} / 输出{" "}
                    {tokens(usageTotals.output_tokens)}
                  </small>
                </div>
                <div>
                  <span>消耗积分</span>
                  <strong>{amount(usageTotals.credits_micro)}</strong>
                  <small>
                    缓存命中{" "}
                    {usageTotals.input_tokens && usageTotals.cache_hit_rate != null
                      ? Math.round(usageTotals.cache_hit_rate * 100) + "%"
                      : "—"}
                  </small>
                </div>
                <div>
                  <span>平均耗时</span>
                  <strong>
                    {usageTotals.avg_latency_ms != null
                      ? (usageTotals.avg_latency_ms / 1000).toFixed(2) + "s"
                      : "—"}
                  </strong>
                  <small>
                    首字{" "}
                    {usageTotals.avg_ttft_ms != null
                      ? usageTotals.avg_ttft_ms + " ms"
                      : "—"}
                  </small>
                </div>
              </div>
              <div className="usage-grid">
                <DonutCard
                  title="模型分布"
                  unit="次"
                  items={(usage.by_model || []).map((m: Row) => ({
                    name: m.name,
                    value: m.requests,
                  }))}
                />
                <DonutCard
                  title="账号分布"
                  unit="次"
                  items={(usage.by_account || []).map((m: Row) => ({
                    name: m.name,
                    value: m.requests,
                  }))}
                />
                <DonutCard
                  title="端点分布"
                  unit="次"
                  items={(usage.by_endpoint || []).map((m: Row) => ({
                    name: m.name,
                    value: m.requests,
                  }))}
                />
              </div>
              <section className="panel trend-card">
                <div className="panel-title">
                  <h3>Token 使用趋势</h3>
                  <div className="trend-legend">
                    <span>
                      <i style={{ background: "#2f6bff" }} />
                      输入
                    </span>
                    <span>
                      <i style={{ background: "#0ea5e9" }} />
                      输出
                    </span>
                  </div>
                </div>
                <TrendChart series={usage.series || []} />
              </section>
              <section className="panel">
                <div className="log-filters">
                  <select
                    aria-label="按账号筛选"
                    value={logFilters.account_id}
                    onChange={(e) =>
                      setLogFilters({
                        ...logFilters,
                        account_id: e.target.value,
                      })
                    }
                  >
                    <option value="">全部账号</option>
                    {data.accounts.map((a: Row) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                  </select>
                  <select
                    aria-label="按 Key 筛选"
                    value={logFilters.key_id}
                    onChange={(e) =>
                      setLogFilters({ ...logFilters, key_id: e.target.value })
                    }
                  >
                    <option value="">全部 Key</option>
                    {data.keys.map((k: Row) => (
                      <option key={k.id} value={k.id}>
                        {k.name}
                      </option>
                    ))}
                  </select>
                  <input
                    placeholder="模型 ID"
                    aria-label="按模型筛选"
                    value={logFilters.model}
                    onChange={(e) =>
                      setLogFilters({ ...logFilters, model: e.target.value })
                    }
                  />
                  <select
                    aria-label="按状态筛选"
                    value={logFilters.state}
                    onChange={(e) =>
                      setLogFilters({ ...logFilters, state: e.target.value })
                    }
                  >
                    <option value="">全部状态</option>
                    {["success", "failed", "interrupted", "pending"].map(
                      (s) => (
                        <option key={s} value={s}>
                          {states[s]}
                        </option>
                      ),
                    )}
                  </select>
                  <input
                    aria-label="开始时间"
                    type="datetime-local"
                    value={logFilters.since}
                    onChange={(e) =>
                      setLogFilters({ ...logFilters, since: e.target.value })
                    }
                  />
                  <input
                    aria-label="结束时间"
                    type="datetime-local"
                    value={logFilters.until}
                    onChange={(e) =>
                      setLogFilters({ ...logFilters, until: e.target.value })
                    }
                  />
                  <button onClick={() => applyLogFilters()}>筛选</button>
                  <a
                    className="button"
                    href={"/api/logs/export?" + filtersQuery()}
                  >
                    <Download size={14} />
                    导出 CSV
                  </a>
                </div>
                {data.logs.length ? (
                  <Table
                    headers={[
                      "时间 / 请求",
                      "模型",
                      "账号",
                      "结果",
                      "Token 输入 / 输出",
                      "积分",
                      "首字 / 总耗时",
                    ]}
                  >
                    {data.logs.map((r: Row) => (
                      <tr key={r.id}>
                        <td>
                          <button
                            className="text-link"
                            onClick={() =>
                              act(async () => {
                                setSelected(await api("/logs/" + r.id));
                                setModal("request");
                              })
                            }
                          >
                            {date(r.created)}
                          </button>
                          <small>{r.id.slice(0, 16)}…</small>
                        </td>
                        <td>{r.model}</td>
                        <td>
                          {data.accounts.find((a: Row) => a.id === r.account_id)
                            ?.name || r.account_id}
                        </td>
                        <td>
                          <Badge value={r.state} />
                          <small>{r.error || ""}</small>
                        </td>
                        <td>
                          {r.input_tokens ?? "未知"} /{" "}
                          {r.output_tokens ?? "未知"}
                        </td>
                        <td>{amount(r.credits_micro)}</td>
                        <td>
                          {r.ttft_ms ?? "—"} / {r.latency_ms ?? "—"} ms
                        </td>
                      </tr>
                    ))}
                  </Table>
                ) : (
                  <Empty text="没有匹配的请求记录" />
                )}
                <div className="pagination">
                  <button
                    disabled={!logPage}
                    onClick={() => applyLogFilters(Math.max(0, logPage - 50))}
                  >
                    上一页
                  </button>
                  <span>第 {Math.floor(logPage / 50) + 1} 页</span>
                  <button
                    disabled={data.logs.length < 50}
                    onClick={() => applyLogFilters(logPage + 50)}
                  >
                    下一页
                  </button>
                </div>
              </section>
            </>
          )}
          {page === "settings" && (
            <>
              <div className="split">
                <section className="panel padded">
                  <h3>同步与记录</h3>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      act(() =>
                        api(
                          "/settings",
                          {
                            sync_interval: Number(settings.sync_interval),
                            retention_days: Number(settings.retention_days),
                          },
                          "PUT",
                        ),
                      );
                    }}
                  >
                    <Field title="余额与模型同步间隔（秒）">
                      <input
                        type="number"
                        min="60"
                        max="86400"
                        value={settings.sync_interval}
                        onChange={(e) =>
                          setSettings({
                            ...settings,
                            sync_interval: e.target.value,
                          })
                        }
                      />
                    </Field>
                    <Field title="日志保留天数">
                      <input
                        type="number"
                        min="1"
                        max="3650"
                        value={settings.retention_days}
                        onChange={(e) =>
                          setSettings({
                            ...settings,
                            retention_days: e.target.value,
                          })
                        }
                      />
                    </Field>
                    <button className="primary" disabled={busy}>
                      保存设置
                    </button>
                  </form>
                </section>
                <section className="panel padded">
                  <h3>备份与安全</h3>
                  <p>
                    备份包含账号凭据，使用你设置的密码加密。恢复会写入新目录，可单独启动验收。
                  </p>
                  <div className="stack">
                    <button onClick={() => open("backup")}>
                      <Download size={16} />
                      创建加密备份
                    </button>
                    <button onClick={() => open("restore")}>
                      <Database size={16} />
                      恢复备份到新目录
                    </button>
                    <button onClick={() => open("password")}>
                      <KeyRound size={16} />
                      修改管理员密码
                    </button>
                  </div>
                </section>
              </div>
              <section className="panel padded spaced">
                <h3>本地运行信息</h3>
                <dl>
                  <dt>数据目录</dt>
                  <dd>
                    <code>{settings.data_dir}</code>
                  </dd>
                  <dt>凭据保护</dt>
                  <dd>{settings.credential_protection}</dd>
                  <dt>API 基址</dt>
                  <dd>
                    <code>{location.origin}/v1</code>
                  </dd>
                  <dt>版本 / 数据结构</dt>
                  <dd>
                    {settings.version} / {settings.schema}
                  </dd>
                </dl>
              </section>
              <section className="panel spaced">
                <div className="panel-title">
                  <h3>后台任务记录</h3>
                  <span>最近 100 条</span>
                </div>
                {data.jobs.length ? (
                  <Table headers={["时间", "账号", "任务", "结果", "说明"]}>
                    {data.jobs.map((j: Row) => (
                      <tr key={j.id}>
                        <td>{date(j.created)}</td>
                        <td>{j.account_id || "系统"}</td>
                        <td>{j.kind}</td>
                        <td>
                          <Badge value={j.state} />
                        </td>
                        <td>{j.message || "—"}</td>
                      </tr>
                    ))}
                  </Table>
                ) : (
                  <p className="padded muted">暂无后台任务。</p>
                )}
              </section>
              <section className="panel spaced">
                <div className="panel-title">
                  <h3>管理操作审计</h3>
                </div>
                <Table headers={["时间", "操作", "对象"]}>
                  {settings.audit?.map((a: Row) => (
                    <tr key={a.id}>
                      <td>{date(a.created)}</td>
                      <td>{a.action}</td>
                      <td>{a.target}</td>
                    </tr>
                  ))}
                </Table>
              </section>
            </>
          )}
          <footer>
            WorkBuddy Hub <span>·</span> 本地管理 / 数据可追溯 <span>·</span>{" "}
            与腾讯官方无隶属关系
          </footer>
        </main>
      </div>
      {modal && (
        <div
          className="overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) {
              setModal("");
              setOauth(null);
            }
          }}
        >
          <section
            className={
              "dialog " + (["detail", "request"].includes(modal) ? "wide" : "")
            }
            role="dialog"
            aria-modal="true"
            aria-label="管理操作"
          >
            <button
              className="dialog-close icon-button"
              aria-label="关闭窗口"
              onClick={() => {
                setModal("");
                setOauth(null);
              }}
            >
              <X size={20} />
            </button>
            {notice && (
              <div className="notice" role="alert">
                {notice}
              </div>
            )}
            {modal === "account" && (
              <>
                <h2>添加账号</h2>
                <p>授权会直接在官方页面完成，凭据加密保存在本机。</p>
                <button
                  className="primary full"
                  disabled={busy}
                  onClick={() =>
                    act(async () => setOauth(await api("/oauth/start", {})))
                  }
                >
                  扫码授权 WorkBuddy 国内版
                </button>
                {oauth && (
                  <div className="qr">
                    <img src={oauth.qr} alt="官方登录二维码" />
                    <a href={oauth.url} target="_blank" rel="noreferrer">
                      打开官方授权页面 <ArrowUpRight size={14} />
                    </a>
                    <small>等待授权… 此会话有效期 10 分钟</small>
                  </div>
                )}
                <details>
                  <summary>导入已授权凭据 / 兼容上游</summary>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      const f = new FormData(e.currentTarget);
                      act(async () => {
                        const c = JSON.parse(String(f.get("credential")));
                        const r = await api("/accounts", {
                          name: f.get("name"),
                          provider: f.get("provider"),
                          credential: c,
                          pool_id: Number(f.get("pool")),
                        });
                        setNotice(
                          r.sync.ok
                            ? "账号已导入并同步"
                            : "账号已保存；同步结果：" +
                                r.sync.errors.join("；"),
                        );
                      }, true);
                    }}
                  >
                    <Field title="账号名称">
                      <input name="name" required />
                    </Field>
                    <Field title="连接器">
                      <select name="provider">
                        <option value="workbuddy_cn">WorkBuddy 国内版</option>
                        <option value="openai_compatible">
                          OpenAI-compatible
                        </option>
                      </select>
                    </Field>
                    <Field title="加入账号池">
                      <select name="pool">
                        {data.pools.map((p: Row) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Field title="授权 JSON（仅用于此账号导入）">
                      <textarea
                        name="credential"
                        rows={7}
                        required
                        spellCheck={false}
                        placeholder={
                          '国内版：{"auth":{"accessToken":"…","refreshToken":"…","domain":"www.codebuddy.cn","expiresAt":…},"account":{"uid":"…"}}\n兼容上游：{"endpoint":"https://example.com/v1","api_key":"…"}'
                        }
                      />
                    </Field>
                    <button className="primary" disabled={busy}>
                      导入并同步
                    </button>
                  </form>
                </details>
              </>
            )}
            {modal === "account-edit" && selected && (
              <>
                <h2>账号设置</h2>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    act(
                      () =>
                        api(
                          "/accounts/" + selected.id,
                          {
                            name: f.get("name"),
                            max_concurrency: Number(f.get("concurrency")),
                          },
                          "PATCH",
                        ),
                      true,
                    );
                  }}
                >
                  <Field title="名称">
                    <input name="name" defaultValue={selected.name} required />
                  </Field>
                  <Field title="账号并发上限">
                    <input
                      name="concurrency"
                      type="number"
                      min="1"
                      max="32"
                      defaultValue={selected.max_concurrency}
                    />
                  </Field>
                  <button className="primary" disabled={busy}>
                    保存
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      act(() =>
                        api("/accounts/" + selected.id + "/refresh", {}),
                      )
                    }
                  >
                    刷新授权令牌
                  </button>
                </form>
                <p>
                  重新授权请使用「添加账号」。同一 UID
                  会更新原账号，不会创建重复记录。
                </p>
              </>
            )}
            {modal === "detail" && selected && (
              <>
                <span className="eyebrow">ACCOUNT / {selected.id}</span>
                <h2>{selected.name}</h2>
                <Badge value={selected.state} />
                <p>
                  剩余积分 {amount(selected.balance_micro)} · 授权有效期{" "}
                  {date(selected.auth_expires)}
                </p>
                <h3>积分包</h3>
                <Table
                  headers={["名称", "剩余积分", "到期时间", "采集时间", "来源"]}
                >
                  {selected.packages?.map((p: Row) => (
                    <tr key={p.id}>
                      <td>{p.name}</td>
                      <td>{amount(p.remaining_micro)}</td>
                      <td>{date(p.expires)}</td>
                      <td>{date(p.observed)}</td>
                      <td>{p.source}</td>
                    </tr>
                  ))}
                </Table>
                {!selected.packages?.length && (
                  <p className="muted">没有可展示的积分包明细。</p>
                )}
                <h3>余额快照</h3>
                <Table headers={["采集时间", "剩余积分", "来源"]}>
                  {selected.snapshots?.slice(0, 10).map((s: Row, i: number) => (
                    <tr key={i}>
                      <td>{date(s.observed)}</td>
                      <td>{amount(s.remaining_micro)}</td>
                      <td>{s.source}</td>
                    </tr>
                  ))}
                </Table>
              </>
            )}
            {modal === "pool" && (
              <>
                <h2>{selected ? "编辑账号池" : "创建账号池"}</h2>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    act(
                      () =>
                        api(
                          "/pools" + (selected ? "/" + selected.id : ""),
                          {
                            name: f.get("name"),
                            strategy: f.get("strategy"),
                            account_ids: f.getAll("members").map(Number),
                            fallback_pool_id: f.get("fallback")
                              ? Number(f.get("fallback"))
                              : null,
                          },
                          selected ? "PUT" : "POST",
                        ),
                      true,
                    );
                  }}
                >
                  <Field title="名称">
                    <input name="name" required defaultValue={selected?.name} />
                  </Field>
                  <Field title="调度策略">
                    <select
                      name="strategy"
                      defaultValue={selected?.strategy || "balanced"}
                    >
                      <option value="balanced">均衡使用</option>
                      <option value="expiring">临期积分优先</option>
                    </select>
                  </Field>
                  <Field title="备用账号池（主池无可用账号时使用）">
                    <select
                      name="fallback"
                      defaultValue={selected?.fallback_pool_id || ""}
                    >
                      <option value="">不设置</option>
                      {data.pools
                        .filter((p: Row) => p.id !== selected?.id)
                        .map((p: Row) => (
                          <option value={p.id} key={p.id}>
                            {p.name}
                          </option>
                        ))}
                    </select>
                  </Field>
                  <h4>成员账号</h4>
                  <div className="check-list">
                    {data.accounts.map((a: Row) => (
                      <label key={a.id}>
                        <input
                          type="checkbox"
                          name="members"
                          value={a.id}
                          defaultChecked={selected?.account_ids.includes(a.id)}
                        />
                        {a.name}
                        <Badge value={a.state} />
                      </label>
                    ))}
                  </div>
                  <button className="primary" disabled={busy}>
                    保存账号池
                  </button>
                </form>
              </>
            )}
            {modal === "key" && (
              <>
                <h2>创建 API Key</h2>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    act(async () => {
                      const r = await api("/keys", {
                        name: f.get("name"),
                        pool_id: Number(f.get("pool")),
                        models: f.getAll("models").map(String),
                        request_limit: f.get("limit")
                          ? Number(f.get("limit"))
                          : null,
                        rpm: Number(f.get("rpm")),
                        max_concurrency: Number(f.get("concurrency")),
                        expires: f.get("expires")
                          ? new Date(String(f.get("expires"))).getTime() / 1000
                          : null,
                      });
                      setKeySecret(r.key);
                      setSelected(null);
                      setModal("key-secret");
                    });
                  }}
                >
                  <Field title="名称">
                    <input
                      name="name"
                      placeholder="例如：日常客户端"
                      required
                    />
                  </Field>
                  <Field title="账号池">
                    <select name="pool">
                      {data.pools.map((p: Row) => (
                        <option value={p.id} key={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <h4>允许的模型</h4>
                  <p className="muted">
                    不勾选表示允许该账号池（含备用池）内的全部模型。
                  </p>
                  <div className="check-list">
                    {modelOptions.length ? (
                      modelOptions.map((name) => (
                        <label key={name}>
                          <input type="checkbox" name="models" value={name} />
                          <code>{name}</code>
                        </label>
                      ))
                    ) : (
                      <span className="muted">
                        目录为空，请先在账号页同步模型
                      </span>
                    )}
                  </div>
                  <div className="form-grid">
                    <Field title="总请求预算（留空不限）">
                      <input type="number" name="limit" min="1" />
                    </Field>
                    <Field title="每分钟请求数">
                      <input
                        type="number"
                        name="rpm"
                        min="1"
                        max="10000"
                        defaultValue="60"
                      />
                    </Field>
                    <Field title="最大并发">
                      <input
                        type="number"
                        name="concurrency"
                        min="1"
                        max="100"
                        defaultValue="2"
                      />
                    </Field>
                    <Field title="到期时间（可选）">
                      <input type="datetime-local" name="expires" />
                    </Field>
                  </div>
                  <button className="primary" disabled={busy}>
                    创建密钥
                  </button>
                </form>
              </>
            )}
            {modal === "key-models" && selected && (
              <>
                <h2>限制「{selected.name}」可用的模型</h2>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    act(
                      () =>
                        api(
                          "/keys/" + selected.id,
                          { models: f.getAll("models").map(String) },
                          "PATCH",
                        ),
                      true,
                    );
                  }}
                >
                  <p className="muted">
                    不勾选任何项表示允许该账号池内的全部模型；勾选后只允许选中的模型。
                  </p>
                  <div className="check-list">
                    {modelOptions.length ? (
                      modelOptions.map((name) => (
                        <label key={name}>
                          <input
                            type="checkbox"
                            name="models"
                            value={name}
                            defaultChecked={keyScope(selected).includes(name)}
                          />
                          <code>{name}</code>
                        </label>
                      ))
                    ) : (
                      <span className="muted">目录为空，请先同步账号模型</span>
                    )}
                  </div>
                  <button className="primary" disabled={busy}>
                    保存模型范围
                  </button>
                </form>
              </>
            )}
            {modal === "key-secret" && (
              <>
                <h2>{selected ? "密钥明文" : "密钥已创建"}</h2>
                <p>
                  {selected
                    ? "这是该 Key 当前有效的明文，仅本机管理台可见，每次查看都会写入审计。"
                    : "请现在保存，也可以在列表里点「查看密钥」再次查看。"}
                </p>
                <pre className="secret">{keySecret}</pre>
                <button className="primary" onClick={() => copy(keySecret)}>
                  <Copy size={16} />
                  复制密钥
                </button>
                {selected && (
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => rotateKey(selected)}
                  >
                    重新生成（旧密钥立即失效）
                  </button>
                )}
                <button
                  onClick={() => {
                    setKeySecret("");
                    setModal("");
                    setSelected(null);
                  }}
                >
                  {selected ? "关闭" : "我已保存"}
                </button>
              </>
            )}
            {modal === "alias" && (
              <>
                <h2>添加模型别名</h2>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    act(
                      () =>
                        api("/models/aliases", {
                          alias: f.get("alias"),
                          model: f.get("model"),
                        }),
                      true,
                    );
                  }}
                >
                  <Field title="客户端模型名称">
                    <input name="alias" required />
                  </Field>
                  <Field title="实际模型">
                    <select name="model">
                      {[
                        ...new Set(
                          data.models.models.map((m: Row) => String(m.model)),
                        ),
                      ].map((m) => (
                        <option key={String(m)}>{String(m)}</option>
                      ))}
                    </select>
                  </Field>
                  <button className="primary" disabled={busy}>
                    保存映射
                  </button>
                </form>
              </>
            )}
            {modal === "playground" && (
              <>
                <h2>测试调用</h2>
                <p>这会通过网关发起真实模型调用，消耗上游额度。</p>
                <form
                  onSubmit={async (e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    setBusy(true);
                    try {
                      const r = await fetch("/v1/chat/completions", {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          Authorization: "Bearer " + f.get("key"),
                        },
                        body: JSON.stringify({
                          model: f.get("model"),
                          messages: [
                            { role: "user", content: f.get("prompt") },
                          ],
                          stream: false,
                        }),
                      });
                      const j = await r.json();
                      setSelected(j);
                      await load();
                    } catch (e) {
                      fail(e);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <Field title="网关 API Key">
                    <input name="key" type="password" required />
                  </Field>
                  <Field title="模型">
                    <input
                      name="model"
                      required
                      defaultValue={data.models.models[0]?.model}
                    />
                  </Field>
                  <Field title="消息">
                    <textarea
                      name="prompt"
                      rows={3}
                      defaultValue="请回复：连接成功。"
                      required
                    />
                  </Field>
                  <button className="primary" disabled={busy}>
                    {busy ? "调用中…" : "发送测试请求"}
                  </button>
                </form>
                {selected && <pre>{JSON.stringify(selected, null, 2)}</pre>}
              </>
            )}
            {modal === "request" && selected && (
              <>
                <h2>请求详情</h2>
                <p>请求、上游尝试与本地预算结算分别记录。</p>
                <pre>{JSON.stringify(selected, null, 2)}</pre>
              </>
            )}
            {modal === "backup" && (
              <>
                <h2>创建加密备份</h2>
                <p>请保管备份密码，跨机器恢复时需要它。</p>
                <form
                  onSubmit={async (e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    setBusy(true);
                    try {
                      const r = await fetch("/api/backup", {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          "X-CSRF-Token": csrf,
                        },
                        body: JSON.stringify({ passphrase: f.get("password") }),
                      });
                      if (!r.ok) throw new Error("备份失败");
                      const url = URL.createObjectURL(await r.blob());
                      const a = document.createElement("a");
                      a.href = url;
                      a.download = "workbuddy-hub.wbh";
                      a.click();
                      setTimeout(() => URL.revokeObjectURL(url), 1000);
                      setModal("");
                      setNotice("加密备份已生成");
                    } catch (e) {
                      fail(e);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <Field title="备份密码（至少 12 位）">
                    <input
                      name="password"
                      type="password"
                      minLength={12}
                      required
                    />
                  </Field>
                  <button className="primary" disabled={busy}>
                    加密并下载
                  </button>
                </form>
              </>
            )}
            {modal === "restore" && (
              <>
                <h2>恢复备份</h2>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    act(async () => {
                      const file = f.get("file") as File;
                      if (file.size > 44 * 1024 * 1024)
                        throw new Error("备份过大，请使用命令行恢复");
                      const bytes = new Uint8Array(await file.arrayBuffer());
                      let raw = "";
                      for (let i = 0; i < bytes.length; i += 8192)
                        raw += String.fromCharCode(
                          ...bytes.subarray(i, i + 8192),
                        );
                      const r = await api("/restore", {
                        passphrase: f.get("password"),
                        data: btoa(raw),
                      });
                      setNotice(
                        "恢复完成：" + r.directory + "。" + r.instruction,
                      );
                    }, true);
                  }}
                >
                  <Field title="加密备份文件">
                    <input name="file" type="file" accept=".wbh" required />
                  </Field>
                  <Field title="备份密码">
                    <input
                      name="password"
                      type="password"
                      minLength={12}
                      required
                    />
                  </Field>
                  <button className="primary" disabled={busy}>
                    校验并恢复到新目录
                  </button>
                </form>
              </>
            )}
            {modal === "password" && (
              <>
                <h2>修改管理员密码</h2>
                <form
                  onSubmit={async (e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    setBusy(true);
                    try {
                      await api("/password", {
                        current: f.get("current"),
                        password: f.get("password"),
                      });
                      setModal("");
                      setSigned(false);
                      csrf = "";
                      setNotice("密码已更新，请重新登录");
                    } catch (e) {
                      fail(e);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <Field title="当前密码">
                    <input name="current" type="password" required />
                  </Field>
                  <Field title="新密码（至少 12 位）">
                    <input
                      name="password"
                      type="password"
                      minLength={12}
                      required
                    />
                  </Field>
                  <button className="primary" disabled={busy}>
                    更新密码并重新登录
                  </button>
                </form>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
