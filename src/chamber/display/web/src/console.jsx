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

function Card({ title, icon: Icon, action, children }) {
  return (
    <section className="card">
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

function Switch({ checked, onChange, label }) {
  return (
    <label className="switch">
      <button
        role="switch"
        aria-checked={checked}
        aria-label={label}
        className={checked ? "is-on" : ""}
        onClick={(e) => {
          e.preventDefault();
          onChange(!checked);
        }}
      >
        <span className="knob" />
      </button>
      <span>{label}</span>
    </label>
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

function SessionsView({ runs, selected, detail, booted, onSelect, search, onSearch, auto, onAuto }) {
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
            <Switch checked={auto} onChange={onAuto} label="auto" />
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
                      {!r.ended_at && <Badge tone="live">live</Badge>}
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
      <Card title="Session detail" icon={ListChecks}>
        {!selected && <p className="muted">pick a run on the left</p>}
        {selected && !detail && <SkeletonDetail />}
        {detail?.error && <p className="muted">{detail.error}</p>}
        {detail?.run && (
          <>
            <Tabs
              active={tab}
              onChange={setTab}
              tabs={[
                { id: "overview", label: "Overview" },
                { id: "sources", label: "Sources", count: sources.length },
                { id: "steps", label: "Steps", count: (detail.steps || []).length },
              ]}
            />
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
                      <div key={a.id} className="action-line">
                        {a.outcome === "ok" ? "✓" : "✗"} {a.name} — {(a.message || "").slice(0, 90)}
                      </div>
                    ))}
                  </li>
                ))}
              </ol>
            )}
            {tab === "overview" && (
              <Placeholder title="Compare" phase={3}>
                Side-by-side against another run.
              </Placeholder>
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

function FeedLine({ row }) {
  if (row.kind === "step") {
    return (
      <div>
        <strong>step {row.n}</strong> · {row.url || "—"}
        {row.thought && <div className="muted">“{String(row.thought).slice(0, 140)}”</div>}
      </div>
    );
  }
  if (row.kind === "action") {
    return (
      <div>
        {row.outcome === "ok" ? "✓" : "✗"} {row.name} — {String(row.message || "").slice(0, 100)}
      </div>
    );
  }
  if (row.kind === "exchange") {
    const calls = (row.tool_calls || []).map((c) => c.name).join(", ");
    return (
      <div>
        {row.role} · {row.model}
        {row.text && <div className="muted">{String(row.text).slice(0, 140)}</div>}
        {calls && <div className="muted">→ {calls}</div>}
      </div>
    );
  }
  return <div className="muted">· {row.url || row.canonical || "visit"}</div>;
}

function LiveView({ runs }) {
  const [runId, setRunId] = useState(null);
  const [feed, setFeed] = useState([]);
  const [paused, setPaused] = useState(false);
  const [pending, setPending] = useState([]);
  const [note, setNote] = useState("");
  const [stream, setStream] = useState("idle"); // idle | live | down
  const [sendState, setSendState] = useState("");
  const seen = React.useRef(new Set());

  useEffect(() => {
    if (runId || runs.length === 0) return;
    const liveRun = runs.find((r) => !r.ended_at) || runs[0];
    setRunId(liveRun.id);
  }, [runs, runId]);

  useEffect(() => {
    if (!runId) return;
    setFeed([]);
    seen.current = new Set();
    setStream("idle");
    let closed = false;
    query("run_state", { run_id: runId }).then((res) => {
      if (closed || !res?.ok) return;
      setPaused(!!res.paused);
      setPending(res.pending_notes || []);
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
      if (fresh.length > 0) setFeed((prev) => [...prev.slice(-300), ...fresh]);
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

  return (
    <div className="split" style={{ gridTemplateColumns: "minmax(0, 1fr) 320px" }}>
      <Card
        title="Live stream"
        icon={Radio}
        action={
          stream === "live" ? (
            <Badge tone="live">live</Badge>
          ) : stream === "down" ? (
            <Badge tone="warn">reconnecting…</Badge>
          ) : (
            <Badge>connecting…</Badge>
          )
        }
      >
        <div className="search">
          <select
            value={runId || ""}
            onChange={(e) => setRunId(e.target.value)}
            aria-label="Watch a run"
            className="run-pick"
          >
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {!r.ended_at ? "● " : ""}{r.id} — {(r.task || "").slice(0, 50)}
              </option>
            ))}
          </select>
        </div>
        {paused && (
          <div className="notice" style={{ margin: "0 0 8px" }}>
            Paused — the loop holds at the next step boundary.
          </div>
        )}
        <ul className="run-list">
          {feed.map((r) => (
            <li key={rowKey(r)} className="feed-line">
              <FeedLine row={r} />
            </li>
          ))}
        </ul>
        {feed.length === 0 && (
          <p className="muted">waiting for rows — new steps land here as the loop writes them</p>
        )}
      </Card>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Card title="Loop control" icon={Settings}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button variant={paused ? "default" : "outline"} size="sm" onClick={togglePause}>
              {paused ? "▶ Resume" : "⏸ Pause"}
            </Button>
          </div>
          <Field label="Send the model a note" hint="Filed to the run mailbox; read at the next step.">
            <div className="search" style={{ marginBottom: 6 }}>
              <input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="e.g. compare prices first…"
                aria-label="Note text"
                onKeyDown={(e) => e.key === "Enter" && sendNote()}
              />
            </div>
            <Button variant="outline" size="sm" onClick={sendNote}>
              File note
            </Button>{" "}
            <span className="muted">{sendState}</span>
          </Field>
          {pending.length > 0 && (
            <>
              <h3>Waiting ({pending.length})</h3>
              <ul className="plain-list">
                {pending.map((n) => (
                  <li key={n.id}>✎ {n.text}</li>
                ))}
              </ul>
            </>
          )}
        </Card>
        <Card title="New session" icon={Boxes} action={<Badge tone="warn">Phase 3</Badge>}>
          <p className="muted">Task box + profile picker + start. Today's equivalent is a terminal.</p>
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

function ProfilesView({ env, booted }) {
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
              </tr>
            </thead>
            <tbody>
              {(env?.profiles || []).map((p) => (
                <tr key={p.name}>
                  <td>{p.name}</td>
                  <td>{p.size_mb} MB</td>
                  <td>{p.logins ? "yes" : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Placeholder title="Profile control">
            Switch profile (closes + relaunches the browser context) · start a session on a
            profile · cookie/login status.
          </Placeholder>
        </>
      )}
    </Card>
  );
}

function ModelsView({ env, booted }) {
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
      <Placeholder title="Model control">
        Per-session override + free-tier budget tracking. <Badge tone="warn">Phase 3</Badge>
      </Placeholder>
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
  const [auto, setAuto] = useState(true);
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

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!auto || view !== "sessions") return;
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [auto, view, refresh]);

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
      await fetchDetail(id);
    },
    [fetchDetail],
  );

  // Keep an open run's detail fresh while auto-refresh ticks.
  useEffect(() => {
    if (!auto || !selected || view !== "sessions") return;
    const t = setInterval(() => fetchDetail(selected), 8000);
    return () => clearInterval(t);
  }, [auto, selected, view, fetchDetail]);

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
            booted={booted}
            onSelect={openRun}
            search={search}
            onSearch={setSearch}
            auto={auto}
            onAuto={setAuto}
          />
        )}
          {view === "live" && <LiveView runs={runs} />}
        {view === "environment" && <EnvironmentView env={env} booted={booted} />}
        {view === "profiles" && <ProfilesView env={env} booted={booted} />}
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
