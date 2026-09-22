import React, { StrictMode, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertTriangle,
  Boxes,
  CheckCircle2,
  Database,
  Globe2,
  ListChecks,
  Monitor,
  Moon,
  Play,
  Radio,
  RefreshCw,
  Search,
  Settings,
  Sun,
  MonitorSmartphone,
} from "lucide-react";
import "./console.css";

// Chamber Console — shadcn-style IA.
//
// Desk × Console contract (CHANGELOG 0.11.0): Desk and Console are separate
// apps sharing the snapshot/query contract; Desk stays server-independent.
// This app never touches the trace store directly — all history arrives via
// `display/queries.py`, over the Desk `__chamberQuery` binding when hosted
// in a Desk window, else same-origin `/api/query`.
//
//   Topbar: brand left · nav centre · status/theme/settings right
//   Settings (cog): own view with fixed sub-sidebar — Appearance / Server /
//     Data / About. The one place for "all things" app-level.
//   Session detail: Tabs — Overview · Sources · Steps.
//   Extra function: run search filter, auto-refresh switch, resizable split.

async function query(op, args = {}) {
  if (typeof window.__chamberQuery === "function") {
    try {
      const res = await window.__chamberQuery({ op, ...args });
      if (res && res.ok) return res;
      return res || { ok: false, error: "empty binding result" };
    } catch (err) {
      return { ok: false, error: String(err?.message || err) };
    }
  }
  const params = new URLSearchParams({ op, ...args });
  try {
    const res = await fetch(`/api/query?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch {
    return {
      ok: false,
      error: "no live Chamber here — open inside Chamber Desk, or serve via the console server",
    };
  }
}

const NAV = [
  { id: "sessions", label: "Sessions", icon: Database, phase: 1 },
  { id: "live", label: "Live", icon: Radio, phase: 1 },
  { id: "environment", label: "Environment", icon: Monitor, phase: 1 },
  { id: "profiles", label: "Profiles", icon: Globe2, phase: 1 },
  { id: "models", label: "Models", icon: Boxes, phase: 1 },
];

function SkeletonList({ rows = 6 }) {
  return (
    <div className="skel-list" aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skel" style={{ height: 54 }} />
      ))}
    </div>
  );
}

function SkeletonDetail() {
  return (
    <div className="skel-detail" aria-hidden="true">
      <div className="skel" style={{ height: 20, width: "40%" }} />
      <div className="skel" style={{ height: 14 }} />
      <div className="skel" style={{ height: 14, width: "70%" }} />
      <div className="skel" style={{ height: 120 }} />
    </div>
  );
}

/* ---------------- theme ---------------- */

function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("chamber-theme") || "system";
    } catch {
      return "system";
    }
  });
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
    try {
      localStorage.setItem("chamber-theme", theme);
    } catch {
      // private window — won't persist
    }
  }, [theme]);
  const cycle = useCallback(() => {
    setTheme((t) => (t === "light" ? "dark" : t === "dark" ? "system" : "light"));
  }, []);
  return [theme, cycle, setTheme];
}

function ThemeIcon({ theme }) {
  if (theme === "light") return <Sun size={16} />;
  if (theme === "dark") return <Moon size={16} />;
  return <MonitorSmartphone size={16} />;
}

/* ---------------- shadcn-style primitives ---------------- */

function Button({ variant = "default", size = "md", ...props }) {
  return <button className={`btn btn-${variant} btn-${size}`} {...props} />;
}

function IconButton({ label, ...props }) {
  return <button className="icon-btn" aria-label={label} title={label} {...props} />;
}

// Connection status in place of the old Refresh button. Auto-refresh is the
// heartbeat: green check while polls land, orange triangle when they fail.
// The popover exposes last-contact time + one manual refresh.
function StatusButton({ online, lastOk, refreshing, onRefresh }) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const close = (e) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [open ]);

  return (
    <div className="status-wrap">
      <button
        className={`icon-btn status-btn ${online ? "is-ok" : "is-bad"}`}
        aria-label={online ? "Server connected" : "Server unreachable. Activate for details."}
        title={online ? "Server connected" : "Server unreachable"}
        onClick={() => setOpen((o) => !o)}
      >
        {online ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
      </button>
      {open && (
        <>
          <div className="popover-backdrop" onClick={() => setOpen(false)} />
          <div className="popover" role="dialog" aria-label="Server status">
            <div className="popover-row">
              {online ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
              <strong>{online ? "Connected" : "Unreachable"}</strong>
            </div>
            <div className="muted">
              Last contact: {lastOk ? new Date(lastOk).toLocaleTimeString() : "never"}
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={onRefresh}
              disabled={refreshing}
              className={refreshing ? "is-loading" : ""}
            >
              <RefreshCw size={13} /> Refresh now
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

function Badge({ tone = "muted", children }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

function Card({ title, icon: Icon, action, children, tint, footer }) {
  return (
    <section className={`card${tint ? ` ${tint}` : ""}`}>
      {(title || action) && (
        <div className="card-head">
          <h2>
            {Icon && <Icon size={15} />}
            {title}
          </h2>
          {action}
        </div>
      )}
      <div className="card-body">{children}</div>
      {footer && <div className="card-foot">{footer}</div>}
    </section>
  );
}

function Tabs({ tabs, active, onChange }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={active === t.id}
          className={active === t.id ? "is-active" : ""}
          onClick={() => onChange(t.id)}
        >
          {t.label}
          {t.count != null && <span className="tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

function Field({ label, children, hint }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <div className="field-control">{children}</div>
      {hint && <p className="field-hint">{hint}</p>}
    </div>
  );
}

function KV({ label, children }) {
  return (
    <div className="kv">
      <span>{label}</span>
      <div>{children}</div>
    </div>
  );
}

function Placeholder({ title, phase = 3, children }) {
  return (
    <div className="placeholder">
      <h3>
        {title} <Badge tone="warn">Phase {phase}</Badge>
      </h3>
      <div className="muted">{children}</div>
    </div>
  );
}

/* ---------------- views ---------------- */

function ExchangeFull({ runId, exchangeId }) {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState(null);
  const toggle = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (body !== null) return;
    const res = await query("exchange_detail", { run_id: runId, exchange_id: exchangeId });
    setBody(res?.exchange || { error: res?.error || "not found" });
  };
  return (
    <div>
      <button className="text-button" onClick={toggle}>
        {open ? "Hide full exchange" : "View full prompt / response"}
      </button>
      {open &&
        (body === null ? (
          <p className="muted">loading…</p>
        ) : body.error ? (
          <p className="muted">{body.error}</p>
        ) : (
          <pre className="log-view">
            {body.truncated ? "(capped at 256KB per side)\n\n" : ""}
            {(body.request_json || "").slice(0, 6000)}
            {"\n\n———— response ————\n\n"}
            {(body.response_json || "").slice(0, 6000)}
          </pre>
        ))}
    </div>
  );
}

function StepCard({ runId, step }) {
  const ex = step.exchange;
  const tokens = ex ? (ex.input_tokens || 0) + (ex.output_tokens || 0) : 0;
  return (
    <div className="loop-card">
      <div className="loop-head">
        <strong>Step {step.n}</strong>
        <span className="muted">{step.url || "—"}</span>
        <span className="muted">
          {step.ms ? `${(step.ms / 1000).toFixed(1)}s` : ""} {tokens ? `· ${tokens} tok` : ""}
        </span>
      </div>
      {step.active_stage && (
        <div className="loop-stage">
          ➤ {step.active_stage.stage}{" "}
          <span className="muted">
            ({step.active_stage.current + 1}/{step.active_stage.total})
          </span>
        </div>
      )}
      {step.thought && (
        <div className="loop-block">
          <h4>Thought</h4>
          <p>“{step.thought}”</p>
        </div>
      )}
      {ex && (
        <div className="loop-block">
          <h4>
            Step model · {ex.model} {ex.ms ? `· ${(ex.ms / 1000).toFixed(1)}s` : ""}
          </h4>
          {ex.request?.last_user && (
            <p className="muted">
              asked: {ex.request.last_user.slice(0, 280)}
              {ex.request.last_user_truncated ? "… (excerpt)" : ""}
            </p>
          )}
          {ex.text && <p>{ex.text.slice(0, 800)}</p>}
          {ex.reasoning_summary && (
            <p className="muted">reasoning: {ex.reasoning_summary.slice(0, 500)}</p>
          )}
          {(ex.tool_calls || []).length > 0 && (
            <p>
              →{" "}
              {ex.tool_calls
                .map((c) => `${c.name}(${Object.entries(c.arguments || {}).slice(0, 3).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")})`)
                .join(" · ")}
            </p>
          )}
          <ExchangeFull runId={runId} exchangeId={ex.exchange_id} />
        </div>
      )}
      {(step.actions || []).length > 0 && (
        <div className="loop-block">
          <h4>Actions</h4>
          {step.actions.map((a, i) => (
            <div key={i} className="action-line">
              {a.outcome === "ok" ? "✓" : "✗"} <strong>{a.name}</strong>
              {a.why && <span className="muted"> — {a.why.slice(0, 160)}</span>}
              {a.message && <div className="muted">{a.message.slice(0, 200)}</div>}
            </div>
          ))}
        </div>
      )}
      {step.planner_turn?.plan && (
        <div className="loop-block loop-planner">
          <h4>Planner · {step.planner_turn.model}</h4>
          <p className="muted">{step.planner_turn.plan.assessment}</p>
          <ol className="plan-stages">
            {step.planner_turn.plan.stages.map((s, i) => (
              <li key={i} className={i === step.planner_turn.plan.current ? "is-now" : ""}>
                {i === step.planner_turn.plan.current ? "➤ " : ""}
                {s}
              </li>
            ))}
          </ol>
        </div>
      )}
      {step.planner_turn && !step.planner_turn.plan && step.planner_turn.raw_text && (
        <div className="loop-block loop-planner">
          <h4>Planner · {step.planner_turn.model}</h4>
          <p className="muted">{step.planner_turn.raw_text.slice(0, 500)}</p>
        </div>
      )}
      {(step.vision || []).length > 0 &&
        step.vision.map((v) => (
          <div key={v.exchange_id} className="loop-block">
            <h4>Vision · {v.model}</h4>
            <p className="muted">Q: {v.question.slice(0, 300)}</p>
            <p>A: {v.answer.slice(0, 600)}</p>
          </div>
        ))}
    </div>
  );
}

function ThoughtLoop({ runId }) {
  const [loop, setLoop] = useState(null);
  useEffect(() => {
    setLoop(null);
    let live = true;
    query("thought_loop", { run_id: runId }).then((res) => {
      if (live && res?.ok) setLoop(res);
      else if (live) setLoop({ error: res?.error || "not found" });
    });
    return () => {
      live = false;
    };
  }, [runId]);
  if (!loop) return <SkeletonDetail />;
  if (loop.error) return <p className="muted">{loop.error}</p>;
  if (!loop.steps || loop.steps.length === 0)
    return <p className="muted">no steps recorded for this run</p>;
  return (
    <div className="loop-feed">
      {loop.steps.map((s) => (
        <StepCard key={s.n} runId={runId} step={s} />
      ))}
    </div>
  );
}

function ThreadStrip({ thread, onSelect }) {
  if (!thread) return null;
  const items = [...(thread.ancestors || [])];
  const hasThread = items.length > 0 || (thread.children || []).length > 0;
  if (!hasThread && !thread.parent) return null;
  return (
    <div className="thread-strip">
      {items.map((a) => (
        <span key={a.id}>
          <button className="text-button" onClick={() => onSelect(a.id)} title={a.task}>
            ← {a.id.slice(-6)}
          </button>{" "}
        </span>
      ))}
      <strong>this run</strong>
      {(thread.children || []).map((c) => (
        <span key={c.id}>
          {" "}
          <button className="text-button" onClick={() => onSelect(c.id)} title={c.task}>
            {c.id.slice(-6)} →
          </button>
        </span>
      ))}
    </div>
  );
}

function CompareBlock({ runs, left }) {
  const [otherId, setOtherId] = useState("");
  const [other, setOther] = useState(null);
  useEffect(() => {
    if (!otherId) {
      setOther(null);
      return;
    }
    let live = true;
    query("get_run", { run_id: otherId }).then((res) => {
      if (live && res?.ok) setOther(res);
    });
    return () => {
      live = false;
    };
  }, [otherId]);
  useEffect(() => {
    setOtherId("");
    setOther(null);
  }, [left?.run?.id]);
  const row = (label, a, b) => (
    <tr key={label}>
      <td>{label}</td>
      <td className="wrap">{a}</td>
      <td className="wrap">{b}</td>
    </tr>
  );
  const steps = (d) => (d?.steps || []).length;
  return (
    <div>
      <h3>Compare</h3>
      <div className="search" style={{ marginBottom: 8 }}>
        <select
          value={otherId}
          onChange={(e) => setOtherId(e.target.value)}
          aria-label="Compare against"
          className="run-pick"
        >
          <option value="">against…</option>
          {runs
            .filter((r) => r.id !== left?.run?.id)
            .map((r) => (
              <option key={r.id} value={r.id}>
                {r.id} — {(r.task || "").slice(0, 40)}
              </option>
            ))}
        </select>
      </div>
      {other && (
        <table className="table">
          <thead>
            <tr>
              <th></th>
              <th>this run</th>
              <th>other run</th>
            </tr>
          </thead>
          <tbody>
            {row("task", left?.run?.task, other?.run?.task)}
            {row(
              "result",
              `${left?.run?.success ? "success" : "stopped"} · ${steps(left)} steps`,
              `${other?.run?.success ? "success" : "stopped"} · ${steps(other)} steps`,
            )}
            {row("model calls", left?.exchange_count, other?.exchange_count)}
            {row("models", (left?.models || []).join(", "), (other?.models || []).join(", "))}
            {row("sources", (left?.sources || []).length, (other?.sources || []).length)}
          </tbody>
        </table>
      )}
    </div>
  );
}

function SessionsView({ runs, selected, detail, thread, booted, onSelect, onContinue, search, onSearch }) {
  const [tab, setTab] = useState("overview");
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return runs;
    return runs.filter(
      (r) =>
        r.id.toLowerCase().includes(needle) || (r.task || "").toLowerCase().includes(needle),
    );
  }, [runs, search]);

  const actionsByStep = useMemo(() => {
    const map = {};
    for (const a of detail?.actions || []) (map[a.step_n] = map[a.step_n] || []).push(a);
    return map;
  }, [detail]);

  const sources = detail?.sources || [];

  // Resizable list/detail split, persisted per browser.
  const [listW, setListW] = useState(() => {
    try {
      const v = parseInt(localStorage.getItem("chamber-list-w") || "360", 10);
      return Number.isFinite(v) ? Math.min(Math.max(v, 240), 640) : 360;
    } catch {
      return 360;
    }
  });
  const onDragStart = useCallback(
    (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = listW;
      const move = (ev) => {
        const w = Math.min(Math.max(startW + ev.clientX - startX, 240), 640);
        setListW(w);
        try {
          localStorage.setItem("chamber-list-w", String(w));
        } catch {
          // private window — won't persist
        }
      };
      const up = () => document.removeEventListener("mousemove", move);
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up, { once: true });
    },
    [listW],
  );

  return (
    <div className="split" style={{ gridTemplateColumns: `${listW}px 5px minmax(0, 1fr)` }}>
      <Card
        title="Runs"
        icon={Database}
        action={
          <div className="card-tools">
            <span className="count">{filtered.length}</span>
          </div>
        }
      >
        <div className="search">
          <Search size={14} />
          <input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="Filter runs…"
            aria-label="Filter runs"
          />
        </div>
        {!booted ? (
          <SkeletonList />
        ) : (
          <>
            <ul className="run-list">
              {filtered.map((r) => (
                <li key={r.id}>
                  <button
                    className={selected === r.id ? "is-active" : ""}
                    onClick={() => onSelect(r.id)}
                    title={r.task}
                  >
                    <span className="run-id">
                      {r.success ? <Badge tone="ok">✓</Badge> : <Badge>•</Badge>} {r.id}
                      {r.live && <Badge tone="live">live</Badge>}
                    </span>
                    <span className="run-task">{r.task}</span>
                  </button>
                </li>
              ))}
            </ul>
            {filtered.length === 0 && <p className="muted">no runs match</p>}
          </>
        )}
      </Card>

      <div
        className="resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize list and detail"
        onMouseDown={onDragStart}
      />
      <Card
        title="Session detail"
        icon={ListChecks}
        action={
          selected && detail?.run && !detail.run.live ? (
            <Button variant="outline" size="sm" onClick={() => onContinue(selected)}>
              Continue →
            </Button>
          ) : null
        }
      >
        {!selected && <p className="muted">pick a run on the left</p>}
        {selected && !detail && <SkeletonDetail />}
        {detail?.error && <p className="muted">{detail.error}</p>}
        <ThreadStrip thread={thread} onSelect={onSelect} />
        {detail?.run && (
          <>
            <Tabs
              active={tab}
              onChange={setTab}
              tabs={[
                { id: "overview", label: "Overview" },
                { id: "loop", label: "Loop", count: (detail.steps || []).length },
                { id: "sources", label: "Sources", count: sources.length },
                { id: "steps", label: "Steps", count: (detail.steps || []).length },
              ]}
            />
            {tab === "loop" && <ThoughtLoop runId={selected} />}
            {tab === "overview" && (
              <>
                <KV label="task">
                  <strong>{detail.run.task}</strong>
                </KV>
                <KV label="result">
                  {detail.run.success ? "success" : "stopped"} · {(detail.steps || []).length}{" "}
                  steps · {detail.exchange_count} model calls · profile {detail.run.profile || "—"}
                </KV>
                <KV label="models seen">{(detail.models || []).join(", ") || "—"}</KV>
                <h3>Calls by role</h3>
                <table className="table">
                  <thead>
                    <tr>
                      <th>role</th>
                      <th>calls</th>
                      <th>model</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(detail.exchanges_by_role || {}).map(([role, rows]) => (
                      <tr key={role}>
                        <td>{role}</td>
                        <td>{rows.length}</td>
                        <td>{rows[0]?.model || ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
            {tab === "sources" && (
              <table className="table">
                <thead>
                  <tr>
                    <th></th>
                    <th>page</th>
                    <th>visits</th>
                  </tr>
                </thead>
                <tbody>
                  {sources.map((s) => (
                    <tr key={s.canonical}>
                      <td>{s.used ? "★" : "·"}</td>
                      <td className="wrap">{s.canonical}</td>
                      <td>{s.visits > 1 ? `×${s.visits}` : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {tab === "steps" && (
              <ol className="steps">
                {(detail.steps || []).map((s) => (
                  <li key={s.n}>
                    <span className="step-head">
                      {s.n} · {s.url || "—"}
                    </span>
                    {s.thought && <div className="muted">“{s.thought.slice(0, 160)}”</div>}
                    {(actionsByStep[s.n] || []).map((a) => (
                      <div key={`${a.step_n}:${a.seq}`} className="action-line">
                        {a.outcome === "ok" ? "✓" : "✗"} {a.name} — {(a.message || "").slice(0, 90)}
                      </div>
                    ))}
                  </li>
                ))}
              </ol>
            )}
            {tab === "overview" && (
              <CompareBlock runs={runs} left={detail} />
            )}
          </>
        )}
      </Card>
    </div>
  );
}

async function post(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.json();
}

function rowKey(r) {
  return `${r.kind}:${r.id ?? r.rowid}`;
}

// Group streamed rows into thought-cards, same shape as Session `thought_loop`
// cards so both tabs share StepCard. Steps are written at step END, so an
// exchange belongs to the first step whose `at` covers its `started_at`.
function groupLiveRows(rows) {
  const steps = rows.filter((r) => r.kind === "step").sort((a, b) => a.n - b.n);
  const cards = steps.map((s) => ({
    n: s.n,
    url: s.url,
    thought: s.thought,
    ms: s.ms,
    active_stage: null,
    exchange: null,
    actions: [],
    planner_turn: null,
    vision: [],
  }));
  const byN = new Map(cards.map((c) => [c.n, c]));
  // Steps carry `at` (written at step end); see group docstring.
  const atByN = new Map(steps.map((s) => [s.n, s.at || 0]));
  for (const r of rows) {
    if (r.kind === "action") {
      const c = byN.get(r.step_n);
      if (c)
        c.actions.push({ name: r.name, outcome: r.outcome, message: r.message, why: r.why });
    } else if (r.kind === "exchange") {
      const at = Number(r.started_at) || 0;
      let target = null;
      for (const c of cards) {
        const stepAt = Number(atByN.get(c.n)) || 0;
        if (stepAt >= at && at > 0) {
          target = c;
          break;
        }
      }
      target = target || cards[cards.length - 1];
      if (!target) continue;
      if (r.role === "planner") {
        target.planner_turn = { model: r.model, plan: null, raw_text: r.text || "" };
      } else if (r.role === "vision") {
        target.vision.push({
          exchange_id: r.id,
          model: r.model,
          question: r.question || "",
          answer: r.text || "",
        });
      } else {
        target.exchange = {
          exchange_id: r.id,
          model: r.model,
          ms: r.ms,
          input_tokens: r.input_tokens,
          output_tokens: r.output_tokens,
          text: r.text || "",
          reasoning_summary: "",
          tool_calls: r.tool_calls || [],
          request: null,
        };
      }
    }
  }
  return cards.slice(-60);
}

function NewSessionCard({ env, prefill, onPrefilled, onStarted }) {
  const [task, setTask] = useState("");
  const [profile, setProfile] = useState("default");
  const [url, setUrl] = useState("");
  const [steps, setSteps] = useState(40);
  const [model, setModel] = useState("");
  const [whatNext, setWhatNext] = useState("");
  const [fromRun, setFromRun] = useState("");
  const [state, setState] = useState("");
  const profiles = (env?.profiles || []).map((p) => p.name);
  if (!profiles.includes("default")) profiles.unshift("default");
  const stepModel = env?.config?.models?.step;

  // Restart-as-new prefill: task from the old run, steering as first note.
  useEffect(() => {
    if (prefill?.from) {
      setFromRun(prefill.from);
      if (prefill.task) setTask(prefill.task);
      onPrefilled?.();
    }
    if (prefill?.profile) {
      setProfile(prefill.profile);
      onPrefilled?.();
    }
  }, [prefill]);

  const start = async () => {
    if (!task.trim()) {
      setState("a task is required");
      return;
    }
    setState("starting…");
    try {
      const res = await post("/api/runs", {
        task: task.trim(),
        profile,
        start_url: url.trim(),
        max_steps: Number(steps) || 40,
        model: model.trim() || undefined,
        from_run_id: fromRun || undefined,
        what_next: whatNext.trim() || undefined,
      });
      if (res?.ok) {
        setState(`started ${res.run_id} — pick it above to watch`);
        setTask("");
        setWhatNext("");
        setFromRun("");
        onStarted?.(res.run_id);
      } else {
        setState(`failed: ${res?.error || "unknown"}`);
      }
    } catch (err) {
      setState(`failed: ${String(err?.message || err)}`);
    }
  };

  return (
    <Card title="New session" icon={Boxes}>
      <p className="muted">
        Runs as a supervised child on this machine — same <code>.env</code>, same models.
        {stepModel?.model && (
          <>
            {" "}Will run <strong>{stepModel.model}</strong> on profile <strong>{profile}</strong>.
          </>
        )}
      </p>
      {fromRun && (
        <p className="muted">
          Continuing <strong>{fromRun}</strong> — its notes and cited sources ride along.{" "}
          <button className="text-button" onClick={() => setFromRun("")}>
            start fresh instead
          </button>
        </p>
      )}
      <Field label="Task">
        <div className="search" style={{ marginBottom: 6 }}>
          <input
            value={task}
            onChange={(e) => setTask(e.target.value)}
            placeholder="e.g. compare laptop prices…"
            aria-label="Task"
          />
        </div>
      </Field>
      {fromRun && (
        <Field label="What next? (first instruction to the new run)">
          <div className="search" style={{ marginBottom: 6 }}>
            <input
              value={whatNext}
              onChange={(e) => setWhatNext(e.target.value)}
              placeholder="e.g. now check amazon.com too…"
              aria-label="What next"
            />
          </div>
        </Field>
      )}
      <Field label="Profile">
        <div className="search" style={{ marginBottom: 6 }}>
          <select
            value={profile}
            onChange={(e) => setProfile(e.target.value)}
            aria-label="Profile"
            className="run-pick"
          >
            {profiles.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
      </Field>
      <Field label="Start URL (optional)">
        <div className="search" style={{ marginBottom: 6 }}>
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
            aria-label="Start URL"
          />
        </div>
      </Field>
      <Field label={`Model override (default ${stepModel?.model || "…"})`}>
        <div className="search" style={{ marginBottom: 6 }}>
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={stepModel?.model || "e.g. cohere/north-mini-code:free"}
            aria-label="Model override"
          />
        </div>
      </Field>
      <Field label="Max steps">
        <div className="search" style={{ marginBottom: 6 }}>
          <input
            value={steps}
            onChange={(e) => setSteps(e.target.value)}
            type="number"
            min="1"
            max="200"
            aria-label="Max steps"
          />
        </div>
      </Field>
      <Button variant="default" size="sm" onClick={start}>
        <Play size={13} /> Start run
      </Button>{" "}
      <span className="muted">{state}</span>
    </Card>
  );
}

const MAX_TABS = 6;

function LiveView({ runs, env, prefill, onPrefilled, onRunFinished }) {
  // VS Code-style tabs: single-click previews (italic, replaceable),
  // double-click pins. One stream per open tab would fan out polls, so only
  // the active tab streams; the rest restore from cache + status dots.
  const [tabs, setTabs] = useState([]);
  const [runId, setRunId] = useState(null);
  const tabCache = React.useRef(new Map());

  const openTab = useCallback(
    (id, pin = false) => {
      setTabs((prev) => {
        const at = prev.findIndex((t) => t.id === id);
        if (at >= 0) {
          const next = prev.map((t, i) =>
            i === at ? { ...t, pinned: t.pinned || pin } : t,
          );
          return next;
        }
        let next = prev;
        if (!pin) {
          const previewAt = prev.findIndex((t) => !t.pinned);
          next = previewAt >= 0 ? prev.filter((_, i) => i !== previewAt) : prev;
        }
        next = [...next, { id, pinned: pin }];
        while (next.length > MAX_TABS) {
          const evict = next.findIndex((t) => !t.pinned && t.id !== id);
          if (evict < 0) break;
          next = next.filter((_, i) => i !== evict);
        }
        return next;
      });
      setRunId(id);
    },
    [],
  );

  const closeTab = useCallback(
    (id) => {
      setTabs((prev) => prev.filter((t) => t.id !== id));
      if (runId === id) {
        setRunId((current) => {
          if (current !== id) return current;
          const rest = tabs.filter((t) => t.id !== id);
          return rest.length > 0 ? rest[rest.length - 1].id : null;
        });
      }
    },
    [runId, tabs],
  );

  const togglePin = useCallback((id) => {
    setTabs((prev) => prev.map((t) => (t.id === id ? { ...t, pinned: !t.pinned } : t)));
  }, []);
  const [rows, setRows] = useState([]);
  const cards = useMemo(() => groupLiveRows(rows), [rows]);
  const [paused, setPaused] = useState(false);
  const [pending, setPending] = useState([]);
  const [note, setNote] = useState("");
  const [stream, setStream] = useState("idle"); // idle | live | down
  const [sendState, setSendState] = useState("");
  const [managed, setManaged] = useState([]);
  const [stopState, setStopState] = useState("");
  const [logText, setLogText] = useState(null);
  const seen = React.useRef(new Set());

  // Liveness is the server heartbeat flag: crashed runs never close their
  // row, so an open `ended_at` alone would crown corpses as live.
  // Live-only scope: tabs hold heartbeat-alive runs. A finished run
  // auto-closes below; replay lives in Sessions, never here.
  const liveRuns = useMemo(() => runs.filter((r) => r.live), [runs]);

  const refreshManaged = useCallback(async () => {
    try {
      const res = await query("managed_runs");
      if (res?.ok) setManaged(res.runs || []);
    } catch {
      // heartbeat only — the status button owns server health
    }
  }, []);

  useEffect(() => {
    refreshManaged();
    const t = setInterval(refreshManaged, 8000);
    return () => clearInterval(t);
  }, [refreshManaged]);

  useEffect(() => {
    if (runId) return;
    const liveRun = runs.find((r) => r.live);
    if (liveRun) openTab(liveRun.id, true);
  }, [runs, runId, openTab]);

  // A watched run that ends leaves Live on its own: its tab closes and a
  // flash links to its Session detail. Live never shows finished runs.
  const [finishedFlash, setFinishedFlash] = useState(null);
  useEffect(() => {
    const dead = tabs.filter((t) => {
      const r = runs.find((x) => x.id === t.id);
      return r && !r.live;
    });
    if (dead.length === 0) return;
    dead.forEach((t) => closeTab(t.id));
    setFinishedFlash(dead[0].id);
  }, [runs, closeTab]);

  // Restore a tab's cached view instantly on switch; live ticks overwrite.
  useEffect(() => {
    if (!runId) return;
    const cached = tabCache.current.get(runId);
    if (cached) {
      setRows(cached.rows);
      seen.current = new Set(cached.rows.map(rowKey));
      setPaused(cached.paused);
      setPending(cached.pending);
    }
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    setRows([]);
    seen.current = new Set();
    setStream("idle");
    let closed = false;
    query("run_state", { run_id: runId }).then((res) => {
      if (closed || !res?.ok) return;
      setPaused(!!res.paused);
      setPending(res.pending_notes || []);
      tabCache.current.set(runId, {
        rows: tabCache.current.get(runId)?.rows || [],
        paused: !!res.paused,
        pending: res.pending_notes || [],
      });
    });
    // Same-origin server stream. EventSource reconnects on its own; the
    // seen-set makes redelivery a no-op.
    let es = null;
    try {
      es = new EventSource(`/api/live?run_id=${encodeURIComponent(runId)}&cursor=0:0:0:0`);
    } catch {
      setStream("down");
      return undefined;
    }
    es.onopen = () => !closed && setStream("live");
    es.onerror = () => !closed && setStream("down");
    es.addEventListener("tick", (e) => {
      if (closed) return;
      let batch = null;
      try {
        batch = JSON.parse(e.data);
      } catch {
        return;
      }
      setPaused(!!batch.paused);
      setPending(batch.pending_notes || []);
      const fresh = (batch.rows || []).filter((r) => {
        const k = rowKey(r);
        if (seen.current.has(k)) return false;
        seen.current.add(k);
        return true;
      });
      const p = !!batch.paused;
      const pend = batch.pending_notes || [];
      setPaused(p);
      setPending(pend);
      if (fresh.length > 0) {
        setRows((prev) => {
          const next = [...prev.slice(-500), ...fresh];
          tabCache.current.set(runId, { rows: next, paused: p, pending: pend });
          return next;
        });
      } else {
        setRows((prev) => {
          tabCache.current.set(runId, { rows: prev, paused: p, pending: pend });
          return prev;
        });
      }
    });
    return () => {
      closed = true;
      es.close();
    };
  }, [runId]);

  const sendNote = async () => {
    const text = note.trim();
    if (!text || !runId) return;
    setSendState("sending…");
    try {
      const res = await post("/api/inbox", { run_id: runId, text });
      setSendState(res?.ok ? "filed ✓" : `failed: ${res?.error || "?"}`);
      if (res?.ok) setNote("");
    } catch (err) {
      setSendState(`failed: ${String(err?.message || err)}`);
    }
    setTimeout(() => setSendState(""), 2500);
  };

  const togglePause = async () => {
    if (!runId) return;
    setPaused(!paused);
    try {
      await post("/api/pause", { run_id: runId, paused: !paused });
    } catch {
      setPaused(paused);
    }
  };

  const stopRun = async () => {
    if (!runId) return;
    setStopState("stopping…");
    try {
      const res = await post(`/api/runs/${encodeURIComponent(runId)}/stop`, {});
      setStopState(res?.ok ? `stopped (${res.result})` : `failed: ${res?.error || "?"}`);
      refreshManaged();
    } catch (err) {
      setStopState(`failed: ${String(err?.message || err)}`);
    }
  };

  const fetchLog = async () => {
    if (!runId) return;
    setLogText("loading…");
    try {
      const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/log?offset=0`).then((r) =>
        r.json(),
      );
      setLogText(res?.ok ? res.text.slice(-6000) || "(empty so far)" : `failed: ${res?.error}`);
    } catch (err) {
      setLogText(`failed: ${String(err?.message || err)}`);
    }
  };

  const managedIds = new Set(managed.filter((m) => m.managed).map((m) => m.run_id));
  const watchedManaged = runId && managedIds.has(runId);

  // Resizable stream/control split, persisted per browser.
  const [rightW, setRightW] = useState(() => {
    try {
      const v = parseInt(localStorage.getItem("chamber-live-w") || "320", 10);
      return Number.isFinite(v) ? Math.min(Math.max(v, 240), 560) : 320;
    } catch {
      return 320;
    }
  });
  const onLiveDragStart = useCallback(
    (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = rightW;
      const move = (ev) => {
        const w = Math.min(Math.max(startW - (ev.clientX - startX), 240), 560);
        setRightW(w);
        try {
          localStorage.setItem("chamber-live-w", String(w));
        } catch {
          // private window — won't persist
        }
      };
      const up = () => document.removeEventListener("mousemove", move);
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up, { once: true });
    },
    [rightW],
  );

  return (
    <div
      className="split live-split"
      style={{ gridTemplateColumns: `minmax(0, 1fr) 5px ${rightW}px` }}
    >
      <Card
        title="Live stream"
        icon={Radio}
        tint={runId ? "is-live" : ""}
        action={
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <Badge tone="live">● live</Badge>
            {stream === "down" && <Badge tone="warn">reconnecting…</Badge>}
            <Button
              variant={paused ? "default" : "outline"}
              size="sm"
              onClick={togglePause}
              disabled={!runId}
              title="Hold or release the loop"
            >
              {paused ? "▶ Resume" : "⏸ Pause"}
            </Button>
            {watchedManaged && (
              <Button
                variant="outline"
                size="sm"
                onClick={stopRun}
                disabled={!runId}
                title="Stop this run"
              >
                ■ Stop
              </Button>
            )}
          </div>
        }
        footer={
          runId ? (
            <div className="composer-col">
              <div className="composer-row">
                <div className="search" style={{ marginBottom: 0 }}>
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Instruct the model… (Enter sends)"
                    aria-label="Note text"
                    onKeyDown={(e) => e.key === "Enter" && sendNote()}
                  />
                </div>
                <Button variant="outline" size="sm" onClick={sendNote}>
                  Send
                </Button>
                <span className="muted">{sendState}</span>
              </div>
              {pending.length > 0 && (
                <div className="muted">
                  queued: {pending.map((n) => n.text.slice(0, 60)).join(" · ")}
                </div>
              )}
            </div>
          ) : null
        }
      >
        {finishedFlash && (
          <div className="notice" style={{ margin: "0 0 8px" }}>
            {finishedFlash} finished.{" "}
            <button className="text-button" onClick={() => onRunFinished?.(finishedFlash)}>
              Open in Sessions
            </button>{" "}
            <button className="text-button" onClick={() => setFinishedFlash(null)}>
              dismiss
            </button>
          </div>
        )}
        {runs.length === 0 ? (
          <p className="muted">no runs yet — start one below to watch it here</p>
        ) : tabs.length === 0 ? (
          <div className="notice" style={{ margin: "0 0 8px" }}>
            No live runs right now — start one below to watch it here, or review
            finished runs in Sessions.
          </div>
        ) : (
          <>
            <div className="tabbar" role="tablist" aria-label="Watched runs">
              {tabs.map((t) => {
                const r = runs.find((x) => x.id === t.id);
                return (
                  <button
                    key={t.id}
                    role="tab"
                    aria-selected={t.id === runId}
                    className={`${t.id === runId ? "is-active" : ""}${t.pinned ? "" : " is-preview"}`}
                    onClick={() => openTab(t.id)}
                    onDoubleClick={() => togglePin(t.id)}
                    title={`${r?.task || t.id}${t.pinned ? " (pinned)" : " (preview — double-click to pin)"}`}
                  >
                    {r?.live ? "● " : "■ "}
                    {t.id.slice(-6)}
                    <span
                      className="tab-pin"
                      role="button"
                      aria-label={t.pinned ? "Unpin tab" : "Pin tab"}
                      onClick={(e) => {
                        e.stopPropagation();
                        togglePin(t.id);
                      }}
                    >
                      {t.pinned ? "◈" : "◇"}
                    </span>
                    <span
                      className="tab-close"
                      role="button"
                      aria-label="Close tab"
                      onClick={(e) => {
                        e.stopPropagation();
                        closeTab(t.id);
                      }}
                    >
                      ×
                    </span>
                  </button>
                );
              })}
            </div>
            {liveRuns.filter((r) => !tabs.some((t) => t.id === r.id)).length > 0 && (
              <div className="live-rail">
                <span className="muted">live now:</span>
                {liveRuns
                  .filter((r) => !tabs.some((t) => t.id === r.id))
                  .map((r) => (
                    <button key={r.id} className="text-button" onClick={() => openTab(r.id)}>
                      ● {r.id.slice(-6)}
                    </button>
                  ))}
              </div>
            )}
          </>
        )}
        {paused && (
          <div className="notice" style={{ margin: "0 0 8px" }}>
            Paused — the loop holds at the next step boundary.
          </div>
        )}
        {!runId ? (
          <p className="muted">pick a run above to watch</p>
        ) : (
          <>
            <div className="loop-feed">
              {cards.map((c) => (
                <StepCard key={c.n} runId={runId} step={c} />
              ))}
            </div>
            {cards.length === 0 && (
              <p className="muted">waiting for rows — new steps land here as the loop writes them</p>
            )}
          </>
        )}
      </Card>

      <div
        className="resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize stream and controls"
        onMouseDown={onLiveDragStart}
      />
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {stopState && <p className="muted">{stopState}</p>}
        <NewSessionCard
          env={env}
          prefill={prefill}
          onPrefilled={onPrefilled}
          onStarted={(id) => {
            openTab(id, true);
            refreshManaged();
          }}
        />
        <Card title="Run log" icon={ListChecks}>
          <Button variant="outline" size="sm" onClick={fetchLog}>
            Load tail
          </Button>
          {logText !== null && <pre className="log-view">{logText}</pre>}
        </Card>
      </div>
    </div>
  );
}

function ModelLine({ label, model }) {
  if (!model || !model.model) return null;
  return (
    <KV label={label}>
      <strong>{model.model}</strong>
      <div className="muted">
        {model.provider || ""} · {model.base_url || "default"}
        {model.api_style ? ` · ${model.api_style}` : ""}
      </div>
    </KV>
  );
}

function EnvironmentView({ env, booted }) {
  return (
    <Card title="Environment" icon={Monitor}>
      <p className="muted">
        Current defaults — what the <em>next</em> run uses. A past run's reality is in its Session
        detail, because defaults change over time.
      </p>
      {!booted || !env ? (
        <SkeletonDetail />
      ) : (
        <>
          <KV label="profile">
            <strong>{env.config?.profile || "—"}</strong>
          </KV>
          <KV label="browser">
            <strong>{env.browser?.label || env.browser?.error || "—"}</strong>
          </KV>
          <ModelLine label="step" model={env.config?.models?.step} />
          <ModelLine label="planner" model={env.config?.models?.planner} />
          <ModelLine label="vision" model={env.config?.models?.vision} />
          {(env.config?.models?.vision_fallbacks || []).map((m, i) => (
            <ModelLine key={i} label={`vision fb ${i + 1}`} model={m} />
          ))}
        </>
      )}
    </Card>
  );
}

function ProfilesView({ env, booted, onStart }) {
  return (
    <Card
      title="Profiles"
      icon={Globe2}
      action={<Badge tone="warn">switch · Phase 3</Badge>}
    >
      {!booted || !env ? (
        <SkeletonDetail />
      ) : (
        <>
      <table className="table">
        <thead>
          <tr>
            <th>profile</th>
            <th>size</th>
            <th>logins</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {(env?.profiles || []).map((p) => (
            <tr key={p.name}>
              <td>{p.name}</td>
              <td>{p.size_mb} MB</td>
              <td>{p.logins ? "yes" : "—"}</td>
              <td>
                <button className="text-button" onClick={() => onStart?.(p.name)}>
                  start session
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted">
        Profiles bind at session start — a running browser context cannot be re-homed
        (tabs, JS state and refs die with the process; only cookies persist, and those
        already live in the profile). To "switch," start a session on the other profile.
      </p>
        </>
      )}
    </Card>
  );
}

function ModelsView({ env, booted }) {
  const [usage, setUsage] = useState(null);
  useEffect(() => {
    let live = true;
    query("usage").then((res) => {
      if (live && res?.ok) setUsage(res.models || []);
    });
    return () => {
      live = false;
    };
  }, []);
  return (
    <Card title="Models" icon={Boxes}>
      <p className="muted">
        <em>step</em> acts every step (cheap) · <em>planner</em> plans rarely (strong) ·{" "}
        <em>vision</em> describes screenshots (local first, hosted fallback).
      </p>
      {!booted || !env ? (
        <SkeletonDetail />
      ) : (
        <>
          <ModelLine label="step" model={env.config?.models?.step} />
          <ModelLine label="planner" model={env.config?.models?.planner} />
          <ModelLine label="vision" model={env.config?.models?.vision} />
          {(env.config?.models?.vision_fallbacks || []).map((m, i) => (
            <ModelLine key={i} label={`vision fb ${i + 1}`} model={m} />
          ))}
        </>
      )}
      <h3>Usage by model</h3>
      {!usage ? (
        <p className="muted">loading…</p>
      ) : usage.length === 0 ? (
        <p className="muted">no runs recorded yet</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>model</th>
              <th>runs</th>
              <th>steps</th>
              <th>in / out tokens</th>
              <th>cost</th>
            </tr>
          </thead>
          <tbody>
            {usage.map((u) => (
              <tr key={u.model}>
                <td className="wrap">{u.model}</td>
                <td>{u.runs}</td>
                <td>{u.steps}</td>
                <td>
                  {u.input_tokens.toLocaleString()} / {u.output_tokens.toLocaleString()}
                </td>
                <td>{u.free_tier ? "$0" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted">
        Per-session override lives in New session (model field). Prices rot, so only
        free-tier $0 is stated; paid spend is tokens × provider price.
      </p>
    </Card>
  );
}

const SETTINGS_NAV = [
  { id: "appearance", label: "Appearance" },
  { id: "server", label: "Server" },
  { id: "data", label: "Data" },
  { id: "about", label: "About" },
];

function SettingsView({ theme, setTheme }) {
  const [section, setSection] = useState("appearance");
  return (
    <div className="settings">
      <aside className="settings-side">
        {SETTINGS_NAV.map((s) => (
          <button
            key={s.id}
            className={section === s.id ? "is-active" : ""}
            onClick={() => setSection(s.id)}
          >
            {s.label}
          </button>
        ))}
      </aside>
      <div className="settings-body">
        {section === "appearance" && (
          <Card title="Appearance" icon={MonitorSmartphone}>
            <Field label="Theme" hint="System follows your OS. Saved in this browser.">
              <div className="seg">
                {["system", "light", "dark"].map((t) => (
                  <button
                    key={t}
                    className={theme === t ? "is-active" : ""}
                    onClick={() => setTheme(t)}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </Field>
          </Card>
        )}
        {section === "server" && (
          <Card title="Server" icon={Radio}>
            <table className="table">
              <thead>
                <tr>
                  <th>endpoint</th>
                  <th>address</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>console (data)</td>
                  <td>
                    <code>127.0.0.1:5192</code>
                  </td>
                </tr>
                <tr>
                  <td>vite dev (HMR)</td>
                  <td>
                    <code>127.0.0.1:5191</code>
                  </td>
                </tr>
                <tr>
                  <td>query API</td>
                  <td>
                    <code>/api/query?op=list_runs|get_run|run_state|list_profiles|get_environment</code>
                  </td>
                </tr>
              </tbody>
            </table>
            <p className="muted">
              Live sync: <code>/api/live</code> SSE tail (stdlib, reconnects on its own) plus{" "}
              <code>POST /api/inbox</code> and <code>POST /api/pause</code>.
            </p>
          </Card>
        )}
        {section === "data" && (
          <Card title="Data" icon={Database}>
            <KV label="trace db">
              <code>~/.chamber/chamber.sqlite</code> (WAL — safe to read live runs)
            </KV>
            <KV label="profiles">
              <code>~/.chamber/profiles/</code>
            </KV>
            <KV label="run artifacts">
              <code>~/.chamber/runs/&lt;run-id&gt;/</code>
            </KV>
            <KV label="override">
              <code>CHAMBER_HOME</code> moves the whole root
            </KV>
          </Card>
        )}
        {section === "about" && (
          <Card title="About" icon={Boxes}>
            <KV label="console">
              read-only console over the trace store · <Badge>Phase 1</Badge>
            </KV>
            <KV label="desk">
              live run window (<code>display/window.py</code>) — the loop's own read-out
            </KV>
            <KV label="roadmap">
              P2: live sync + loop controls + note inbox · P3: compare, profile switch, model
              overrides
            </KV>
          </Card>
        )}
      </div>
    </div>
  );
}

/* ---------------- app ---------------- */

function App() {
  const [theme, cycleTheme, setTheme] = useTheme();
  const [view, setView] = useState("sessions");
  const [env, setEnv] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [notice, setNotice] = useState("");
  const [search, setSearch] = useState("");
  const [thread, setThread] = useState(null);
  const [prefill, setPrefill] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [booted, setBooted] = useState(false);
  const [online, setOnline] = useState(true);
  const [lastOk, setLastOk] = useState(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setNotice("");
    // Minimum beat so the spinner/skeleton reads instead of flashing.
    // Stale data stays on screen until the fresh payload lands — no blanking.
    const beat = new Promise((r) => setTimeout(r, 450));
    const [e, r] = await Promise.all([
      query("get_environment"),
      query("list_runs", { limit: 30 }),
      beat,
    ]);
    if (e?.ok && r?.ok) {
      setEnv(e);
      setRuns(r.runs || []);
      setOnline(true);
      setLastOk(Date.now());
    } else {
      setOnline(false);
      if (e?.ok) setEnv(e);
      if (r?.ok) setRuns(r.runs || []);
      else if (!e?.ok) setNotice(r?.error || e?.error || "no data source");
    }
    setRefreshing(false);
    setBooted(true);
  }, []);

  // No auto-poll: SSE covers live runs, manual refresh + view-enter cover
  // the rest. Polling full payloads on a timer was the flicker source.
  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    refresh();
  }, [view]);

  // Stale detail stays mounted while the fresh one loads — never blanked.
  // The id guard drops a late response when the user already moved on.
  const fetchDetail = useCallback(async (id) => {
    const res = await query("get_run", { run_id: id });
    setDetail((prev) => {
      if (prev?.run && prev.run.id !== id) return prev;
      if (res?.ok) return res;
      return { error: res?.error || `run not found: ${id}` };
    });
  }, []);

  const openRun = useCallback(
    async (id) => {
      setSelected(id);
      setDetail(null); // user-initiated switch → skeleton, not stale run
      setThread(null);
      await fetchDetail(id);
      try {
        const t = await query("thread", { run_id: id });
        if (t?.ok) setThread(t);
      } catch {
        // thread strip is decorative; detail stands alone
      }
    },
    [fetchDetail],
  );

  // Continue: prefill a fresh run from a finished one and land on Live.
  const openContinue = useCallback(async (id) => {
    try {
      const res = await query("restart_prefill", { run_id: id });
      if (res?.ok) setPrefill({ from: id, task: res.task || "" });
      else setPrefill({ from: id, task: "" });
    } catch {
      setPrefill({ from: id, task: "" });
    }
    setView("live");
  }, []);



  return (
    <div className="console">
      <header className="topbar">
        <div className="top-left">
          <span className="brand-name">Chamber Console</span>
        </div>
        <nav className="nav top-centre" aria-label="Primary">
          {NAV.map((item) => (
            <button
              key={item.id}
              className={view === item.id ? "is-active" : ""}
              onClick={() => setView(item.id)}
            >
              <item.icon size={14} />
              <span>{item.label}</span>
              {item.phase > 1 && <Badge tone="warn">P{item.phase}</Badge>}
            </button>
          ))}
        </nav>
        <div className="top-right">
          <StatusButton
            online={online}
            lastOk={lastOk}
            refreshing={refreshing}
            onRefresh={refresh}
          />
          <IconButton label={`Theme: ${theme} (click to change)`} onClick={cycleTheme}>
            <ThemeIcon theme={theme} />
          </IconButton>
          <IconButton
            label="Settings"
            onClick={() => setView(view === "settings" ? "sessions" : "settings")}
          >
            <Settings size={16} />
          </IconButton>
        </div>
      </header>
      <div className="loadbar" aria-hidden="true">
        {refreshing && <div className="loadbar-bar" />}
      </div>

      {notice && <div className="notice">{notice}</div>}

      <main key={view} className="viewport animate-in">
          {view === "sessions" && (
            <SessionsView
              runs={runs}
              selected={selected}
              detail={detail}
              thread={thread}
              booted={booted}
              onSelect={openRun}
              onContinue={openContinue}
              search={search}
              onSearch={setSearch}
            />
          )}
          {view === "live" && (
            <LiveView
              runs={runs}
              env={env}
              prefill={prefill}
              onPrefilled={() => setPrefill(null)}
              onRunFinished={(id) => {
                setNotice(`${id.slice(-6)} finished — opened in Sessions.`);
                setView("sessions");
                openRun(id);
              }}
            />
          )}
        {view === "environment" && <EnvironmentView env={env} booted={booted} />}
          {view === "profiles" && (
            <ProfilesView
              env={env}
              booted={booted}
              onStart={(profile) => {
                setPrefill({ profile });
                setView("live");
              }}
            />
          )}
        {view === "models" && <ModelsView env={env} booted={booted} />}
        {view === "settings" && <SettingsView theme={theme} setTheme={setTheme} />}
      </main>

      <footer className="statusbar">
        <span>
          {runs.length} runs · {(env?.profiles || []).length} profiles ·{" "}
          {env?.browser?.label || "browser unknown"}
        </span>
        <span className="muted">
          <Badge>Phase 1</Badge> read-only
        </span>
      </footer>
    </div>
  );
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
