/**
 * chamber — the visible layer.
 *
 * A bar across the top of every page carrying the agent's running commentary, a
 * synthetic cursor, element highlights, and a **Take Control** button that stops
 * the agent dead and hands the browser back.
 *
 * Constraints that shaped this:
 *
 *  - **The real mouse is never touched.** Playwright's mouse and CDP's Input
 *    domain synthesize events inside the browser; the OS pointer does not move and
 *    the user keeps their machine. The dot is a read-out of where synthetic events
 *    are being sent, not a driver.
 *
 *  - **Nothing here may intercept a click** — except the one control that is meant
 *    to. The bar is `pointer-events: none` throughout; only the Take Control
 *    button re-enables them. Every node carries `data-chamber-overlay` so
 *    extraction and occlusion checks skip it, and the agent can neither see nor
 *    click its own furniture.
 *
 *  - **The bar pushes the page down rather than covering it.** Overlaying would
 *    hide the top of every site from the person watching, which is the one thing
 *    this is supposed to prevent.
 */
(() => {
  "use strict";
  if (window.__chamberOverlay) return;

  const ACCENT = "#0ea5e9";
  const WARN = "#fbbf24";
  const ERR = "#f87171";
  const OK = "#4ade80";
  const BAR_H = 46;

  const state = {
    x: window.innerWidth / 2,
    y: window.innerHeight / 2,
    mounted: false,
    anim: null,
    controlled: false, // true while the human has taken over
    feed: [],
  };

  let host, shade, cursor, ripple, halo, els;

  function css() {
    return `
      :host { all: initial; }
      .layer {
        position: fixed; inset: 0; pointer-events: none;
        z-index: 2147483647;
        font: 12px/1.5 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
        -webkit-font-smoothing: antialiased;
      }

      /* ---------------------------------------------------------- the bar */
      .bar {
        position: fixed; top: 0; left: 0; right: 0; height: ${BAR_H}px;
        display: flex; align-items: stretch; gap: 0;
        background: #0b1016; color: #e6edf3;
        border-bottom: 1px solid rgba(255,255,255,.10);
        pointer-events: none;
        transition: background .25s ease;
      }
      .bar.controlled { background: #3b2f07; border-bottom-color: ${WARN}; }

      .brand {
        display: flex; align-items: center; gap: 8px;
        padding: 0 14px; flex: none; border-right: 1px solid rgba(255,255,255,.08);
      }
      .dot { width: 8px; height: 8px; border-radius: 50%; background: ${ACCENT}; flex: none; }
      @keyframes chamber-pulse { 0%,100% { opacity: 1 } 50% { opacity: .2 } }
      .dot.busy { animation: chamber-pulse 1.05s ease-in-out infinite; }
      .dot.warn { background: ${WARN}; }
      .dot.err  { background: ${ERR}; }
      .dot.ok   { background: ${OK}; }
      .name { font-weight: 700; letter-spacing: .11em; font-size: 10px;
              text-transform: uppercase; color: ${ACCENT}; }
      .bar.controlled .name { color: ${WARN}; }
      .step { font-variant-numeric: tabular-nums; color: #7d8b99; font-size: 10.5px; }

      /* goal, then the scrolling feed */
      .middle { flex: 1; min-width: 0; display: flex; align-items: center;
                gap: 14px; padding: 0 14px; overflow: hidden; }
      .goal { flex: none; max-width: 26%; font-size: 11px; color: #7d8b99;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

      .feed { flex: 1; min-width: 0; height: 100%; position: relative; overflow: hidden; }
      .feed-inner {
        position: absolute; left: 0; right: 0; bottom: 0;
        display: flex; flex-direction: column; justify-content: flex-end;
        transition: transform .3s cubic-bezier(.2,.8,.2,1);
      }
      .line {
        height: 22px; line-height: 22px; font-size: 12px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        opacity: .35; transition: opacity .3s ease;
      }
      .line:last-child { opacity: 1; }
      .line .tag {
        display: inline-block; min-width: 62px; font-size: 9.5px;
        text-transform: uppercase; letter-spacing: .07em; color: #55636f;
      }
      .line.act .tag { color: ${OK}; }
      .line.err .tag { color: ${ERR}; }
      .line.think .tag { color: ${ACCENT}; }
      .line.plan .tag { color: #818cf8; }
      .line.eye .tag { color: #e879f9; }
      .line.err { color: #fca5a5; }

      /* --------------------------------------------------- take control */
      .control {
        flex: none; display: flex; align-items: center; padding: 0 14px;
        border-left: 1px solid rgba(255,255,255,.08);
        pointer-events: auto;   /* the one thing here that is clickable */
      }
      .btn {
        all: unset; cursor: pointer; user-select: none;
        display: flex; align-items: center; gap: 7px;
        padding: 7px 14px; border-radius: 8px;
        background: ${ACCENT}; color: #04252b;
        font-weight: 680; font-size: 12px; white-space: nowrap;
        transition: background .15s ease, transform .1s ease;
      }
      .btn:hover { background: #38bdf8; }
      .btn:active { transform: scale(.97); }
      .bar.controlled .btn { background: ${WARN}; color: #241a00; }
      .bar.controlled .btn:hover { background: #fcd34d; }

      /* -------------------------------------------------------- cursor */
      .cursor {
        position: fixed; left: 0; top: 0; width: 24px; height: 24px;
        will-change: transform; transform: translate(-4px, -4px);
        transition: opacity .18s ease;
      }
      .cursor svg { display: block; }
      .cursor.hidden { opacity: 0; }
      .halo {
        position: fixed; left: 0; top: 0; border: 2px solid ${ACCENT};
        border-radius: 6px; background: ${ACCENT}14;
        opacity: 0; transition: opacity .15s ease, transform .18s cubic-bezier(.2,.8,.2,1);
      }
      .halo.on { opacity: 1; }
      .halo-tag {
        position: absolute; left: -2px; top: -22px; padding: 1px 7px;
        background: ${ACCENT}; color: #fff; border-radius: 4px;
        font-weight: 650; font-size: 11px; white-space: nowrap;
        max-width: 320px; overflow: hidden; text-overflow: ellipsis;
      }
      .ripple {
        position: fixed; left: 0; top: 0; width: 42px; height: 42px; margin: -21px 0 0 -21px;
        border-radius: 50%; border: 2.5px solid ${ACCENT};
        background: ${ACCENT}2b; opacity: 0;
      }
      @keyframes chamber-ripple {
        0%   { transform: scale(.35); opacity: 1; }
        100% { transform: scale(2.3); opacity: 0; }
      }
      .ripple.go { animation: chamber-ripple .45s cubic-bezier(.2,.7,.3,1) forwards; }

      /* ------------------------------------------------- handing over */
      .banner {
        position: fixed; left: 0; right: 0; top: ${BAR_H}px; padding: 15px 22px;
        background: linear-gradient(180deg, ${WARN}, #f0a020);
        color: #221800; font-weight: 620; font-size: 14.5px;
        box-shadow: 0 6px 26px rgba(0,0,0,.4);
        display: flex; align-items: center; gap: 13px;
        /* Hidden with visibility, not by transform alone: a percentage translate
           is relative to the banner's own height, so a taller banner never fully
           clears the viewport and bleeds a stripe under the bar. Caught in a
           screenshot with ~24px of amber over the top bar and no banner showing.
           NB: this whole block is a JS template literal, so no backticks here. */
        visibility: hidden;
        transform: translateY(-100%); transition: transform .32s cubic-bezier(.2,.9,.3,1);
      }
      .banner.on { visibility: visible; transform: translateY(0); }
      .banner .big { font-size: 20px; }
      .banner .sub { font-weight: 450; opacity: .82; font-size: 12.5px; margin-top: 2px; }
      .edge {
        position: fixed; inset: ${BAR_H}px 0 0 0; border: 3px solid ${WARN};
        opacity: 0; transition: opacity .3s ease;
      }
      .edge.on { opacity: 1; }
    `;
  }

  // Lucide `mouse-pointer-2` (ISC). Filled with a thin white outline: the outline
  // is what keeps the pointer legible over a dark hero image or a blue button.
  // Its tip sits at ~(4,4) in the 24-unit box, hence the -4px offset.
  const CURSOR_SVG = `
    <svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"
            fill="${ACCENT}" stroke="#ffffff" stroke-width="1.4"
            stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;

  /**
   * Make room for the bar instead of covering the page with it.
   *
   * Re-applied on mount because SPA route changes and `document.write` can reset
   * inline styles on the root element.
   */
  function reserveSpace() {
    const root = document.documentElement;
    if (root.style.getPropertyValue("--chamber-bar") !== `${BAR_H}px`) {
      root.style.setProperty("--chamber-bar", `${BAR_H}px`);
      root.style.setProperty("padding-top", `${BAR_H}px`, "important");
      root.style.setProperty("box-sizing", "border-box", "important");
    }
  }

  function mount() {
    if (state.mounted || !document.body) return;

    host = document.createElement("div");
    host.setAttribute("data-chamber-overlay", "root");
    host.style.cssText = "all:initial;position:fixed;z-index:2147483647;";
    const root = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = css();
    root.appendChild(style);

    shade = document.createElement("div");
    shade.className = "layer";
    shade.innerHTML = `
      <div class="bar">
        <div class="brand">
          <div class="dot"></div>
          <div>
            <div class="name">chamber</div>
            <div class="step"></div>
          </div>
        </div>
        <div class="middle">
          <div class="goal"></div>
          <div class="feed"><div class="feed-inner"></div></div>
        </div>
        <div class="control">
          <button class="btn" type="button">
            <span class="btn-icon">&#9995;</span><span class="btn-label">Take control</span>
          </button>
        </div>
      </div>
      <div class="edge"></div>
      <div class="halo"><div class="halo-tag"></div></div>
      <div class="ripple"></div>
      <div class="cursor">${CURSOR_SVG}</div>
      <div class="banner">
        <div class="big">&#128075;</div>
        <div>
          <div class="head">Your turn</div>
          <div class="sub"></div>
        </div>
      </div>`;
    root.appendChild(shade);
    document.documentElement.appendChild(host);

    els = {
      bar: shade.querySelector(".bar"),
      dot: shade.querySelector(".dot"),
      step: shade.querySelector(".step"),
      goal: shade.querySelector(".goal"),
      feed: shade.querySelector(".feed-inner"),
      btn: shade.querySelector(".btn"),
      btnLabel: shade.querySelector(".btn-label"),
      btnIcon: shade.querySelector(".btn-icon"),
      cursor: shade.querySelector(".cursor"),
      ripple: shade.querySelector(".ripple"),
      halo: shade.querySelector(".halo"),
      haloTag: shade.querySelector(".halo-tag"),
      banner: shade.querySelector(".banner"),
      bannerHead: shade.querySelector(".banner .head"),
      bannerSub: shade.querySelector(".banner .sub"),
      edge: shade.querySelector(".edge"),
    };
    cursor = els.cursor;
    ripple = els.ripple;
    halo = els.halo;

    els.btn.addEventListener("click", () => api.toggleControl());

    reserveSpace();
    paintCursor();
    renderFeed();
    state.mounted = true;

    // A page that rewrites <body> (SPA route swaps, document.write) can detach us
    // and reset the root padding. Cheaper and more reliable than a MutationObserver
    // over the whole document.
    setInterval(() => {
      if (!document.documentElement.contains(host)) {
        try { document.documentElement.appendChild(host); } catch {}
      }
      reserveSpace();
    }, 1000);
  }

  function paintCursor() {
    if (cursor) cursor.style.transform = `translate(${state.x - 2}px, ${state.y - 2}px)`;
  }

  function renderFeed() {
    if (!els) return;
    els.feed.innerHTML = "";
    // Only the last few matter; the terminal view keeps the full transcript.
    for (const item of state.feed.slice(-4)) {
      const row = document.createElement("div");
      row.className = `line ${item.kind || ""}`;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item.tag || "";
      row.appendChild(tag);
      row.appendChild(document.createTextNode(item.text || ""));
      els.feed.appendChild(row);
    }
  }

  // --------------------------------------------------------------- public API

  const api = {
    /** Glide the dot to (x, y), easing like a hand would. */
    moveTo(x, y, ms = 340) {
      mount();
      if (state.anim) cancelAnimationFrame(state.anim);
      const x0 = state.x, y0 = state.y;
      const dx = x - x0, dy = y - y0;
      const dist = Math.hypot(dx, dy);
      if (dist < 2 || ms <= 0) { state.x = x; state.y = y; paintCursor(); return; }
      // Long hops should not take proportionally long; a human flicks.
      const dur = Math.min(ms, 180 + dist * 0.42);
      const t0 = performance.now();
      const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
      const tick = (now) => {
        const p = Math.min((now - t0) / dur, 1);
        const e = ease(p);
        state.x = x0 + dx * e;
        state.y = y0 + dy * e;
        paintCursor();
        state.anim = p < 1 ? requestAnimationFrame(tick) : null;
      };
      state.anim = requestAnimationFrame(tick);
    },

    click(x, y) {
      mount();
      if (x != null) { state.x = x; state.y = y; paintCursor(); }
      if (!ripple) return;
      ripple.style.left = state.x + "px";
      ripple.style.top = state.y + "px";
      ripple.classList.remove("go");
      void ripple.offsetWidth; // restart the animation
      ripple.classList.add("go");
    },

    highlight(box, label) {
      mount();
      if (!halo) return;
      if (!box) { halo.classList.remove("on"); return; }
      const [x, y, w, h] = box;
      Object.assign(halo.style, {
        transform: `translate(${x - 3}px, ${y - 3}px)`,
        width: w + 6 + "px",
        height: h + 6 + "px",
      });
      els.haloTag.textContent = label || "";
      els.haloTag.style.display = label ? "block" : "none";
      halo.classList.add("on");
    },

    clearHighlight() { if (halo) halo.classList.remove("on"); },

    /** Append a line to the bar's rolling feed. */
    say(tag, text, kind) {
      mount();
      if (!text) return;
      const last = state.feed[state.feed.length - 1];
      if (last && last.text === text && last.tag === tag) return;
      state.feed.push({ tag, text, kind });
      if (state.feed.length > 40) state.feed.shift();
      renderFeed();
    },

    /** Status dot, step counter and goal. Every field optional. */
    think(patch) {
      mount();
      if (!els) return;
      if (patch.goal !== undefined) els.goal.textContent = patch.goal || "";
      if (patch.step !== undefined) els.step.textContent = patch.step || "";
      if (patch.thought) api.say("thinking", patch.thought, "think");
      if (patch.action) api.say("action", patch.action, "act");
      if (patch.status !== undefined) {
        els.dot.className = "dot";
        if (patch.status === "busy") els.dot.classList.add("busy");
        else if (patch.status && patch.status !== "idle") els.dot.classList.add(patch.status);
      }
    },

    /**
     * Hand the browser to the person, or take it back.
     *
     * The agent is not merely paused visually — `controlled` is read by the Python
     * side before every step, so nothing moves until this is switched back. That
     * is the difference between a pause button and a real handover.
     */
    toggleControl(force) {
      mount();
      const next = force === undefined ? !state.controlled : !!force;
      if (next === state.controlled) return state.controlled;
      state.controlled = next;

      els.bar.classList.toggle("controlled", next);
      els.btnLabel.textContent = next ? "Give control back" : "Take control";
      els.btnIcon.innerHTML = next ? "&#9654;" : "&#9995;";
      els.cursor.classList.toggle("hidden", next);
      if (next) {
        api.clearHighlight();
        api.say("you", "You have control. The agent is stopped.", "plan");
        els.dot.className = "dot warn";
      } else {
        api.say("agent", "Control handed back. Resuming.", "act");
        els.dot.className = "dot busy";
      }

      // Tell Python, if the binding is installed. Absent in a bare page.
      try {
        if (window.__chamberOnControl) window.__chamberOnControl(next);
      } catch {}
      return state.controlled;
    },

    /** Read by the loop before every step. */
    isControlled() { return state.controlled; },

    /** Full-width takeover banner. `head=null` clears it. */
    banner(head, sub) {
      mount();
      if (!els) return;
      if (!head) {
        els.banner.classList.remove("on");
        els.edge.classList.remove("on");
        return;
      }
      els.bannerHead.textContent = head;
      els.bannerSub.textContent = sub || "";
      els.banner.classList.add("on");
      els.edge.classList.add("on");
    },

    state() {
      return {
        x: Math.round(state.x),
        y: Math.round(state.y),
        mounted: state.mounted,
        controlled: state.controlled,
      };
    },
  };

  window.__chamberOverlay = api;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
})();
