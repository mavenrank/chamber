import React, { StrictMode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  Brain,
  CheckCircle2,
  CircleStop,
  Eye,
  Globe2,
  Hand,
  Image as ImageIcon,
  ListChecks,
  MousePointer2,
  Play,
  RefreshCw,
  UserRound,
} from "lucide-react";
import { ScrollArea } from "./components/ui/scroll-area";
import "./styles.css";

const EMPTY = {
  task: "",
  current: { title: "Waiting", detail: "Ready for a task.", tone: "neutral" },
  step: "",
  max_steps: 0,
  url: "",
  controlled: false,
  attention: null,
  finished: false,
  success: false,
  metrics: {
    actions_ok: 0,
    actions_failed: 0,
    input_tokens: 0,
    output_tokens: 0,
    model_calls: 0,
    vision_calls: 0,
  },
  events: [],
};

function formatTime(seconds) {
  if (!seconds) return "now";
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(seconds * 1000));
}

function formatNumber(value) {
  return new Intl.NumberFormat().format(Number(value || 0));
}

function safeSnapshot(next) {
  if (!next || typeof next !== "object") return EMPTY;
  return {
    ...EMPTY,
    ...next,
    current: { ...EMPTY.current, ...(next.current || {}) },
    metrics: { ...EMPTY.metrics, ...(next.metrics || {}) },
    events: Array.isArray(next.events) ? next.events : [],
  };
}

function requestAction(action) {
  try {
    const result = window.__chamberDeskAction?.(action);
    if (result?.catch) result.catch(() => {});
  } catch {
    // A closed companion window must never make the agent loop noisy.
  }
}

const EVENT_VISUALS = {
  run_start: { icon: Play, color: "blue" },
  observe: { icon: Globe2, color: "cyan" },
  plan: { icon: ListChecks, color: "amber" },
  llm_request: { icon: Brain, color: "violet" },
  llm_response: { icon: Brain, color: "violet" },
  thought: { icon: Brain, color: "violet" },
  action: { icon: MousePointer2, color: "green" },
  vision_request: { icon: Eye, color: "sky" },
  vision: { icon: ImageIcon, color: "sky" },
  screenshot: { icon: ImageIcon, color: "sky" },
  vision_ready: { icon: Eye, color: "sky" },
  stuck: { icon: AlertTriangle, color: "orange" },
  parse_retry: { icon: RefreshCw, color: "orange" },
  controlled: { icon: Hand, color: "amber" },
  human_request: { icon: UserRound, color: "amber" },
  human_resolved: { icon: CheckCircle2, color: "green" },
  run_end: { icon: CircleStop, color: "green" },
};

function eventVisual(event) {
  const visual = EVENT_VISUALS[event.event] || { icon: Activity, color: "slate" };
  if (event.tone === "error") return { icon: AlertTriangle, color: "red" };
  if (event.tone === "attention") return { icon: AlertTriangle, color: "orange" };
  return visual;
}

function groupEventsByStep(events) {
  return events.reduce((groups, event) => {
    const step = event.step == null ? "" : String(event.step);
    const previous = groups[groups.length - 1];
    if (previous && previous.step === step) {
      previous.events.push(event);
    } else {
      groups.push({ step, events: [event] });
    }
    return groups;
  }, []);
}

function groupTone(events) {
  if (events.some((event) => event.tone === "error")) return "error";
  if (events.some((event) => event.tone === "attention")) return "attention";
  if (events.some((event) => event.tone === "success")) return "success";
  if (events.some((event) => event.tone === "active")) return "active";
  return "neutral";
}

function App() {
  const [snapshot, setSnapshot] = useState(EMPTY);
  const [following, setFollowing] = useState(true);
  const feedRef = useRef(null);

  useEffect(() => {
    window.__chamberDesk = {
      apply(next) {
        setSnapshot(safeSnapshot(next));
      },
    };
    return () => {
      delete window.__chamberDesk;
    };
  }, []);

  useEffect(() => {
    document.title = snapshot.task ? `Chamber Desk — ${snapshot.task}` : "Chamber Desk";
  }, [snapshot.task]);

  useLayoutEffect(() => {
    if (!following || !feedRef.current) return;
    const feed = feedRef.current;
    feed.scrollTop = feed.scrollHeight;
    const frame = requestAnimationFrame(() => {
      if (following && feedRef.current === feed) feed.scrollTop = feed.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [snapshot.events.length, following]);

  const onFeedScroll = () => {
    const feed = feedRef.current;
    if (!feed) return;
    const atLive = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 48;
    setFollowing(atLive);
  };

  const current = snapshot.current || EMPTY.current;
  const hasCurrentStep = Boolean(snapshot.step || snapshot.max_steps);
  const stateDetail = snapshot.finished
    ? (snapshot.success ? "Task completed." : "Task stopped.")
    : (current.detail || "The loop is active.");
  const stepGroups = groupEventsByStep(snapshot.events);

  return (
    <div className={`desk tone-${current.tone || "neutral"}`}>
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <div>
            <h1>Chamber Desk</h1>
            <p>Loop display</p>
          </div>
        </div>
        <div className="topbar-context" title={snapshot.url || undefined}>
          {snapshot.url || "Local session"}
        </div>
      </header>

      <main>
        <section className="state-section" aria-live="polite">
          <div className="state-title-row">
            <h2 className="state-title">
              <span key={current.title || "working"} className={`shimmer-text ${snapshot.finished ? "is-finished" : ""}`}>
                {current.title || "Working"}
              </span>
            </h2>
            {hasCurrentStep && (
              <div className="current-step-display" aria-label={`Step ${snapshot.step || "unknown"} of ${snapshot.max_steps || "?"}`}>
                <span className="current-step-ring">{snapshot.step || "—"}</span>
                {snapshot.max_steps > 0 && <span className="current-step-total">of {snapshot.max_steps}</span>}
              </div>
            )}
          </div>
          <div className="state-copy">
            <p className="state-detail">{stateDetail}</p>
            {snapshot.task && <p className="task-line">Task · {snapshot.task}</p>}
          </div>
          {snapshot.attention && (
            <div className="attention-block">
              <strong>{snapshot.attention.heading}</strong>
              <span>{snapshot.attention.detail}</span>
            </div>
          )}
        </section>

        <section className="activity-section">
          <div className="activity-heading">
            <span className="section-heading"><ListChecks size={17} strokeWidth={2.1} aria-hidden="true" />Activity chain</span>
            {!following && (
              <button className="text-button" onClick={() => {
                setFollowing(true);
                feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: "smooth" });
              }}>
                Jump to live
              </button>
            )}
          </div>
          <ScrollArea className="activity-scroll" ref={feedRef} onScroll={onFeedScroll}>
            <div className="activity-feed">
              {snapshot.events.length === 0 ? (
                <div className="empty-state">
                  <span>The activity chain will appear here when the loop starts.</span>
                </div>
              ) : (
                stepGroups.map((group, index) => (
                  <section
                    className={`step-group step-tone-${groupTone(group.events)} ${index % 2 ? "is-alt" : ""}`}
                    key={`${group.step || "unassigned"}-${group.events[0]?.id || index}`}
                    aria-label={group.step ? `Step ${group.step}` : "Unnumbered activity"}
                  >
                    <div className="step-marker" aria-hidden="true">
                      <span className="step-marker-circle">{group.step || "—"}</span>
                    </div>
                    <div className="step-card">
                      {group.events.map((event) => <ActivityEntry key={event.id} event={event} />)}
                    </div>
                  </section>
                ))
              )}
            </div>
          </ScrollArea>
        </section>
      </main>

      <footer className="footer-bar">
        <div className="metrics">
          <span>{formatNumber(snapshot.metrics.actions_ok)} actions completed</span>
          <span>{formatNumber(snapshot.metrics.model_calls)} model calls</span>
          {snapshot.metrics.vision_calls > 0 && <span>{formatNumber(snapshot.metrics.vision_calls)} screenshots read</span>}
        </div>
        <button
          className={`control-button ${snapshot.controlled ? "is-controlled" : ""}`}
          onClick={() => requestAction(snapshot.controlled ? "give_control" : "take_control")}
        >
          {snapshot.controlled ? "Give control back" : "Take control"}
        </button>
      </footer>
    </div>
  );
}

function ActivityEntry({ event }) {
  const details = event.details && Object.keys(event.details).length > 0;
  const hasScreenshot = Boolean(event.screenshot);
  const meta = event.meta && Object.entries(event.meta).filter(([, value]) => value !== "" && value != null);
  const visual = eventVisual(event);
  const EventIcon = visual.icon;
  return (
    <article className={`activity-entry entry-${event.tone || "neutral"}`}>
      <div className="entry-body">
        <div className="entry-topline">
          <span className={`entry-source source-${visual.color}`}><EventIcon size={14} strokeWidth={2.1} aria-hidden="true" />{event.source || "Loop"}</span>
          <time>{formatTime(event.at)}</time>
        </div>
        <div className={`entry-title-row icon-${visual.color}`}>
          <span className="entry-icon" aria-hidden="true"><EventIcon size={17} strokeWidth={2.15} /></span>
          <h3>{event.title || "Activity"}</h3>
        </div>
        {event.detail && <p className="entry-detail">{event.detail}</p>}
        {meta?.length > 0 && (
          <div className="entry-meta">
            {meta.slice(0, 6).map(([key, value]) => (
              <span key={key}>{key.replaceAll("_", " ")} · {typeof value === "object" ? JSON.stringify(value) : String(value)}</span>
            ))}
          </div>
        )}
        {hasScreenshot && (
          <figure className="screenshot-card">
            <img src={event.screenshot} alt="Screenshot used by the vision model" loading="lazy" />
            {event.screenshot_path && <figcaption>{event.screenshot_path}</figcaption>}
          </figure>
        )}
        {details && (
          <details className="entry-details">
            <summary>View exchange details</summary>
            <pre>{JSON.stringify(event.details, null, 2)}</pre>
          </details>
        )}
      </div>
    </article>
  );
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
