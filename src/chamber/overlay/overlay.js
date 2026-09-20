/**
 * Chamber's on-page instrument panel.
 *
 * The panel never changes the host page's layout. Only its controls and resize
 * grip accept pointer input, so the rest of the card does not create a dead
 * rectangle over the page. The marked host is ignored by Chamber's extraction.
 */
(() => {
  "use strict";
  if (window.__chamberOverlay) return;

  const skipHosts = window.__chamberSkipHosts || [];
  const hostname = location.hostname || "";
  if (skipHosts.some((h) => hostname === h || hostname.endsWith("." + h))) return;

  const ACCENT = "#a78bfa";
  const WARN = "#f6c453";
  const ERR = "#fb7185";
  const OK = "#6ee7b7";
  const MIN_W = 260;
  const MIN_H = 188;
  // Chamber Desk owns the status read-out in window mode. Keep this overlay
  // mounted for the synthetic cursor, highlights, and handoff control API, but
  // hide the old panel until it is intentionally re-enabled with CHAMBER_DISPLAY=page.
  const panelEnabled = window.__chamberOverlayPanel !== false;

  const state = {
    x: window.innerWidth / 2,
    y: window.innerHeight / 2,
    mounted: false,
    anim: null,
    controlled: false,
    visible: true,
    expanded: false,
    corner: "bottom-right",
    view: "activity",
    width: 360,
    height: 230,
    status: "idle",
    step: "",
    goal: "",
    current: { tag: "ready", text: "Waiting for a task.", kind: "" },
    feed: [],
    attention: null,
  };

  let host, shade, cursor, ripple, halo, els;

  function css() {
    return `
      :host { all: initial; }
      * { box-sizing: border-box; }
      button, select { font: inherit; }
      .layer {
        position: fixed; inset: 0; z-index: 2147483647; pointer-events: none;
        color: #f4f1fb;
        font: 12px/1.45 "Segoe UI Variable Text", "Aptos", "Segoe UI", sans-serif;
        -webkit-font-smoothing: antialiased;
      }
      .panel {
        position: fixed; width: ${state.width}px; min-width: ${MIN_W}px;
        max-width: min(680px, calc(100vw - 24px));
        border: 1px solid rgba(255,255,255,.13); border-radius: 16px;
        background: rgba(18,16,24,.88);
        box-shadow: 0 18px 60px rgba(4,3,8,.38), 0 2px 12px rgba(4,3,8,.28);
        backdrop-filter: blur(18px) saturate(125%);
        -webkit-backdrop-filter: blur(18px) saturate(125%);
        overflow: hidden; pointer-events: none;
        transition: opacity .18s ease, transform .18s ease, border-color .18s ease;
      }
      .panel.bottom-right { right: 12px; bottom: 12px; }
      .panel.bottom-left { left: 12px; bottom: 12px; }
      .panel.top-right { right: 12px; top: 12px; }
      .panel.top-left { left: 12px; top: 12px; }
      .panel.hidden { opacity: 0; transform: translateY(8px); visibility: hidden; }
      .panel.controlled { border-color: color-mix(in srgb, ${WARN} 64%, transparent); }

      .compact {
        min-height: 68px; display: grid; grid-template-columns: minmax(0,1fr) auto;
        align-items: center; gap: 10px; padding: 12px 12px 12px 15px;
      }
      .message { min-width: 0; }
      .meta { display: flex; align-items: center; gap: 7px; min-height: 17px;
              color: #9992a8; font: 600 10px/1.2 "Cascadia Mono", "SFMono-Regular", monospace;
              letter-spacing: .04em; text-transform: uppercase; }
      .tag { color: #c4b5fd; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .step { color: #7f788c; white-space: nowrap; }
      .live-text {
        display: block; margin-top: 4px; max-width: 100%; min-height: 19px;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        color: #f6f2ff; font-size: 13px; font-weight: 540; letter-spacing: -.005em;
      }
      .panel.busy .live-text {
        color: transparent;
        background: linear-gradient(100deg,#a8a1b7 18%,#fff 42%,#d8caff 55%,#a8a1b7 78%);
        background-size: 230% 100%; background-position: 120% 0;
        -webkit-background-clip: text; background-clip: text;
        animation: chamber-shimmer 1.8s linear infinite;
      }
      .panel.warn .tag { color: ${WARN}; }
      .panel.err .tag { color: ${ERR}; }
      .panel.ok .tag { color: ${OK}; }
      @keyframes chamber-shimmer { to { background-position: -110% 0; } }

      .controls { display: flex; align-items: center; gap: 5px; pointer-events: auto; }
      .icon-btn, .control-btn, .view-btn, .corner-select {
        all: unset; box-sizing: border-box; cursor: pointer; user-select: none;
        border: 1px solid rgba(255,255,255,.11); background: rgba(255,255,255,.055);
        color: #d8d2e2; transition: background .14s ease, border-color .14s ease, transform .1s ease;
      }
      .icon-btn { width: 30px; height: 30px; display: grid; place-items: center; border-radius: 9px; }
      .icon-btn:hover, .view-btn:hover, .corner-select:hover { background: rgba(255,255,255,.11); }
      .icon-btn:active, .control-btn:active, .view-btn:active { transform: scale(.96); }
      .icon-btn:focus-visible, .control-btn:focus-visible, .view-btn:focus-visible,
      .corner-select:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }
      .chevron { transition: transform .18s ease; }
      .panel.expanded .chevron { transform: rotate(180deg); }

      .details {
        display: none; height: ${Math.max(state.height - 68, 120)}px; min-height: 120px;
        border-top: 1px solid rgba(255,255,255,.09); pointer-events: none;
      }
      .panel.expanded .details { display: flex; flex-direction: column; }
      .toolbar {
        display: flex; align-items: center; gap: 6px; padding: 9px 10px;
        border-bottom: 1px solid rgba(255,255,255,.07); pointer-events: auto;
      }
      .views { display: flex; gap: 4px; flex: 1; }
      .view-btn { padding: 5px 8px; border-radius: 8px; border-color: transparent;
                  color: #9992a8; font-size: 11px; }
      .view-btn.active { color: #f5f1fb; background: rgba(167,139,250,.14);
                         border-color: rgba(167,139,250,.2); }
      .corner-select { height: 28px; padding: 0 8px; border-radius: 8px; color: #b6afc1; font-size: 11px; }
      .corner-select option { background: #17141d; color: #f4f1fb; }
      .body { flex: 1; min-height: 0; overflow: auto; padding: 11px 12px 18px; pointer-events: auto;
              scrollbar-width: thin; scrollbar-color: #4c4558 transparent; }
      .section { display: none; }
      .section.active { display: block; }
      .label { color: #777080; margin-bottom: 5px; font: 600 9px/1.2 "Cascadia Mono", monospace;
               letter-spacing: .08em; text-transform: uppercase; }
      .goal { color: #e8e2ed; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
      .attention { display: none; margin-bottom: 11px; padding: 10px 11px; border-radius: 10px;
                   border: 1px solid rgba(246,196,83,.28); background: rgba(246,196,83,.1); }
      .attention.on { display: block; }
      .attention-head { color: #ffe7a6; font-weight: 680; }
      .attention-sub { margin-top: 3px; color: #c9b989; font-size: 11px; white-space: pre-wrap; }
      .history { display: grid; gap: 9px; }
      .history-row { display: grid; grid-template-columns: 66px minmax(0,1fr); gap: 9px; }
      .history-tag { color: #827b8e; font: 600 9px/1.5 "Cascadia Mono", monospace;
                     text-transform: uppercase; overflow: hidden; text-overflow: ellipsis; }
      .history-text { color: #d0cad7; overflow-wrap: anywhere; }
      .empty { color: #746d7d; }
      .control-row { display: flex; align-items: center; gap: 9px; }
      .control-btn { padding: 7px 10px; border-radius: 9px; color: #111015;
                     background: #e9e2f3; border-color: transparent; font-size: 11px; font-weight: 700; }
      .panel.controlled .control-btn { background: ${WARN}; color: #241b08; }
      .control-copy { color: #8c8597; font-size: 11px; }

      .resize { position: absolute; width: 20px; height: 20px; bottom: 0; cursor: nwse-resize;
                pointer-events: auto; opacity: .6; }
      .bottom-right .resize, .top-right .resize { right: 0; }
      .bottom-left .resize, .top-left .resize { left: 0; cursor: nesw-resize; }
      .resize::after { content: ""; position: absolute; right: 4px; bottom: 4px; width: 7px; height: 7px;
                       border-right: 1px solid #8c8499; border-bottom: 1px solid #8c8499; }
      .bottom-left .resize::after, .top-left .resize::after { right: auto; left: 4px; transform: rotate(90deg); }

      .cursor { position: fixed; left: 0; top: 0; width: 24px; height: 24px;
                will-change: transform; transition: opacity .18s ease; }
      .cursor.hidden { opacity: 0; }
      .cursor svg { display: block; }
      .halo { position: fixed; left: 0; top: 0; border: 2px solid ${ACCENT}; border-radius: 7px;
              background: color-mix(in srgb, ${ACCENT} 8%, transparent); opacity: 0;
              transition: opacity .15s ease, transform .18s cubic-bezier(.2,.8,.2,1); }
      .halo.on { opacity: 1; }
      .halo-tag { position: absolute; left: -2px; top: -23px; padding: 2px 7px; max-width: 320px;
                  border-radius: 6px; color: #17121f; background: #d8c8ff; font-weight: 680;
                  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .ripple { position: fixed; left: 0; top: 0; width: 42px; height: 42px; margin: -21px 0 0 -21px;
                border: 2px solid ${ACCENT}; border-radius: 50%; opacity: 0; }
      @keyframes chamber-ripple { from { transform: scale(.35); opacity: 1; }
                                  to { transform: scale(2.2); opacity: 0; } }
      .ripple.go { animation: chamber-ripple .45s cubic-bezier(.2,.7,.3,1) forwards; }
      .restore {
        position: fixed; right: 12px; bottom: 12px; display: none; width: 32px; height: 32px;
        place-items: center; pointer-events: auto; cursor: pointer; border-radius: 10px;
        border: 1px solid rgba(255,255,255,.13); background: rgba(18,16,24,.88); color: #ddd5e7;
        box-shadow: 0 8px 24px rgba(4,3,8,.3);
      }
      .panel.hidden + .restore { display: grid; }

      @media (max-width: 520px) {
        .panel { max-width: calc(100vw - 16px); }
        .panel.bottom-right, .panel.top-right { right: 8px; }
        .panel.bottom-left, .panel.top-left { left: 8px; }
        .panel.bottom-right, .panel.bottom-left { bottom: 8px; }
        .panel.top-right, .panel.top-left { top: 8px; }
        .live-text { font-size: 12px; }
      }
      @media (prefers-reduced-motion: reduce) {
        .panel, .chevron, .halo { transition: none; }
        .panel.busy .live-text { animation: none; color: #f6f2ff; background: none; }
        .ripple.go { animation: none; }
      }
    `;
  }

  const CURSOR_SVG = `
    <svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"
            fill="${ACCENT}" stroke="#fff" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
  const EXPAND_ICON = `<svg class="chevron" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>`;
  const EYE_ICON = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2.1 12a10.5 10.5 0 0 1 19.8 0 10.5 10.5 0 0 1-19.8 0Z"/><circle cx="12" cy="12" r="3"/></svg>`;

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
      <aside class="panel bottom-right" aria-label="Agent activity" aria-live="polite">
        <div class="compact">
          <div class="message"><div class="meta"><span class="tag"></span><span class="step"></span></div><span class="live-text"></span></div>
          <div class="controls">
            <button class="icon-btn visibility" type="button" aria-label="Hide activity panel" title="Hide panel">${EYE_ICON}</button>
            <button class="icon-btn expand" type="button" aria-label="Show details" aria-expanded="false" title="Show details">${EXPAND_ICON}</button>
          </div>
        </div>
        <div class="details">
          <div class="toolbar">
            <div class="views"><button class="view-btn active" type="button" data-view="activity">Activity</button><button class="view-btn" type="button" data-view="goal">Goal</button><button class="view-btn" type="button" data-view="history">History</button></div>
            <select class="corner-select" aria-label="Panel position" title="Panel position"><option value="bottom-right">Bottom right</option><option value="bottom-left">Bottom left</option><option value="top-right">Top right</option><option value="top-left">Top left</option></select>
          </div>
          <div class="body">
            <section class="section activity active">
              <div class="attention"><div class="attention-head"></div><div class="attention-sub"></div></div>
              <div class="label">Current</div><div class="current-detail"></div>
              <div class="control-row" style="margin-top:13px"><button class="control-btn" type="button">Take control</button><span class="control-copy">The agent pauses before its next step.</span></div>
            </section>
            <section class="section goal-section"><div class="label">Current goal</div><div class="goal"></div></section>
            <section class="section history-section"><div class="history"></div></section>
          </div>
        </div>
        <div class="resize" role="separator" aria-label="Resize activity panel"></div>
      </aside>
      <button class="restore" type="button" aria-label="Show activity panel" title="Show activity">${EYE_ICON}</button>
      <div class="halo"><div class="halo-tag"></div></div><div class="ripple"></div><div class="cursor">${CURSOR_SVG}</div>`;
    root.appendChild(shade);
    document.documentElement.appendChild(host);
    els = {
      panel: shade.querySelector(".panel"), tag: shade.querySelector(".tag"), step: shade.querySelector(".step"), liveText: shade.querySelector(".live-text"),
      expand: shade.querySelector(".expand"), visibility: shade.querySelector(".visibility"), restore: shade.querySelector(".restore"), details: shade.querySelector(".details"),
      viewButtons: [...shade.querySelectorAll(".view-btn")], sections: [...shade.querySelectorAll(".section")], corner: shade.querySelector(".corner-select"),
      goal: shade.querySelector(".goal"), history: shade.querySelector(".history"), current: shade.querySelector(".current-detail"), control: shade.querySelector(".control-btn"),
      controlCopy: shade.querySelector(".control-copy"), attention: shade.querySelector(".attention"), attentionHead: shade.querySelector(".attention-head"),
      attentionSub: shade.querySelector(".attention-sub"), resize: shade.querySelector(".resize"), cursor: shade.querySelector(".cursor"),
      ripple: shade.querySelector(".ripple"), halo: shade.querySelector(".halo"), haloTag: shade.querySelector(".halo-tag"),
    };
    cursor = els.cursor; ripple = els.ripple; halo = els.halo;
    if (!panelEnabled) {
      els.panel.style.display = "none";
      els.panel.hidden = true;
      els.panel.setAttribute("aria-hidden", "true");
      els.restore.style.display = "none";
      els.restore.hidden = true;
      els.restore.setAttribute("aria-hidden", "true");
    }
    els.expand.addEventListener("click", () => { setExpanded(!state.expanded); savePrefs(); });
    els.visibility.addEventListener("click", () => api.hud(false));
    els.restore.addEventListener("click", () => api.hud(true));
    els.control.addEventListener("click", () => api.toggleControl());
    els.corner.addEventListener("change", () => { setCorner(els.corner.value); savePrefs(); });
    for (const button of els.viewButtons) button.addEventListener("click", () => { setView(button.dataset.view); savePrefs(); });
    installResize(); render(); paintCursor(); state.mounted = true;
    try {
      if (window.__chamberOverlayPrefs) {
        Promise.resolve(window.__chamberOverlayPrefs()).then((prefs) => api.configure(prefs || {})).catch(() => {});
      }
    } catch {}
    setInterval(() => { if (!document.documentElement.contains(host)) { try { document.documentElement.appendChild(host); } catch {} } }, 1000);
  }

  function installResize() {
    els.resize.addEventListener("pointerdown", (event) => {
      event.preventDefault(); els.resize.setPointerCapture(event.pointerId);
      const startX = event.clientX, startY = event.clientY, startW = state.width, startH = state.height;
      const left = state.corner.endsWith("left"), top = state.corner.startsWith("top");
      const move = (e) => {
        const dx = (e.clientX - startX) * (left ? -1 : 1);
        const dy = (e.clientY - startY) * (top ? 1 : -1);
        state.width = Math.max(MIN_W, Math.min(window.innerWidth - 24, startW + dx));
        state.height = Math.max(MIN_H, Math.min(window.innerHeight - 24, startH + dy));
        els.panel.style.width = state.width + "px";
        els.details.style.height = Math.max(state.height - 68, 120) + "px";
      };
      const up = () => { els.resize.removeEventListener("pointermove", move); els.resize.removeEventListener("pointerup", up); savePrefs(); };
      els.resize.addEventListener("pointermove", move); els.resize.addEventListener("pointerup", up);
    });
  }

  function setExpanded(value) {
    state.expanded = !!value; els.panel.classList.toggle("expanded", state.expanded);
    els.expand.setAttribute("aria-expanded", String(state.expanded));
    els.expand.setAttribute("aria-label", state.expanded ? "Hide details" : "Show details");
  }
  function setView(view) {
    if (!["activity","goal","history"].includes(view)) return;
    state.view = view; els.viewButtons.forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    const target = view === "goal" ? "goal-section" : view === "history" ? "history-section" : "activity";
    els.sections.forEach((s) => s.classList.toggle("active", s.classList.contains(target)));
  }
  function setCorner(corner) {
    if (!["bottom-right","bottom-left","top-right","top-left"].includes(corner)) return;
    state.corner = corner; els.panel.classList.remove("bottom-right","bottom-left","top-right","top-left");
    els.panel.classList.add(corner); els.corner.value = corner;
  }
  function savePrefs() {
    try {
      if (window.__chamberOverlayPrefs) {
        window.__chamberOverlayPrefs({corner:state.corner,view:state.view,expanded:state.expanded,width:Math.round(state.width),height:Math.round(state.height),visible:state.visible});
      }
    } catch {}
  }
  function renderHistory() {
    els.history.innerHTML = "";
    if (!state.feed.length) { const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No activity yet."; els.history.appendChild(empty); return; }
    for (const item of [...state.feed].reverse()) {
      const row = document.createElement("div"); row.className = "history-row";
      const tag = document.createElement("div"); tag.className = "history-tag"; tag.textContent = item.tag || "event";
      const text = document.createElement("div"); text.className = "history-text"; text.textContent = item.text || "";
      row.append(tag,text); els.history.appendChild(row);
    }
  }
  function render() {
    if (!els) return;
    const item = state.current;
    els.tag.textContent = state.controlled ? "your control" : (item.tag || "activity");
    els.step.textContent = state.step ? `\u00b7 ${state.step}` : "";
    els.liveText.textContent = state.controlled ? "Agent paused. Use the browser normally." : (item.text || "Waiting.");
    els.current.textContent = item.text || "Waiting."; els.goal.textContent = state.goal || "No goal has been set yet.";
    els.panel.classList.remove("idle","busy","ok","warn","err");
    els.panel.classList.add(state.controlled ? "warn" : state.status || "idle");
    els.panel.classList.toggle("controlled", state.controlled); els.panel.classList.toggle("hidden", !state.visible);
    els.control.textContent = state.controlled ? "Give control back" : "Take control";
    els.controlCopy.textContent = state.controlled ? "Resume only when you are ready." : "The agent pauses before its next step.";
    els.attention.classList.toggle("on", !!state.attention); els.attentionHead.textContent = state.attention?.head || ""; els.attentionSub.textContent = state.attention?.sub || "";
    renderHistory();
  }
  function paintCursor() { if (cursor) cursor.style.transform = `translate(${state.x-2}px, ${state.y-2}px)`; }

  const api = {
    moveTo(x,y,ms=340) {
      mount(); if (state.anim) cancelAnimationFrame(state.anim);
      const x0=state.x,y0=state.y,dx=x-x0,dy=y-y0,dist=Math.hypot(dx,dy);
      if (dist<2 || ms<=0) { state.x=x; state.y=y; paintCursor(); return; }
      const dur=Math.min(ms,180+dist*.42),t0=performance.now();
      const ease=(t)=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
      const tick=(now)=>{ const p=Math.min((now-t0)/dur,1),e=ease(p); state.x=x0+dx*e; state.y=y0+dy*e; paintCursor(); state.anim=p<1?requestAnimationFrame(tick):null; };
      state.anim=requestAnimationFrame(tick);
    },
    click(x,y) { mount(); if (x!=null) { state.x=x; state.y=y; paintCursor(); } ripple.style.left=state.x+"px"; ripple.style.top=state.y+"px"; ripple.classList.remove("go"); void ripple.offsetWidth; ripple.classList.add("go"); },
    highlight(box,label) { mount(); if (!box) { halo.classList.remove("on"); return; } const [x,y,w,h]=box; Object.assign(halo.style,{transform:`translate(${x-3}px, ${y-3}px)`,width:w+6+"px",height:h+6+"px"}); els.haloTag.textContent=label||""; els.haloTag.style.display=label?"block":"none"; halo.classList.add("on"); },
    clearHighlight() { if (halo) halo.classList.remove("on"); },
    say(tag,text,kind) { mount(); if (!text) return; const next={tag:tag||"activity",text,kind:kind||""}; const last=state.feed[state.feed.length-1]; if (last&&last.text===next.text&&last.tag===next.tag) return; state.current=next; state.feed.push(next); if (state.feed.length>100) state.feed.shift(); render(); },
    think(patch) { mount(); if (patch.goal!==undefined) state.goal=patch.goal||""; if (patch.step!==undefined) state.step=patch.step||""; if (patch.status!==undefined) state.status=patch.status||"idle"; if (patch.thought) api.say("thinking",patch.thought,"think"); if (patch.action) api.say("action",patch.action,"act"); render(); },
    toggleControl(force) { mount(); const next=force===undefined?!state.controlled:!!force; if (next===state.controlled) return state.controlled; state.controlled=next; if (next) { api.clearHighlight(); setExpanded(true); setView("activity"); } state.status=next?"warn":"busy"; state.current=next?{tag:"your control",text:"Agent paused. Use the browser normally.",kind:"plan"}:{tag:"resuming",text:"Control returned. Looking at the page again.",kind:"act"}; state.feed.push(state.current); render(); try { if (window.__chamberOnControl) window.__chamberOnControl(next); } catch {} return state.controlled; },
    isControlled() { return state.controlled; },
    banner(head,sub) { mount(); state.attention=head?{head,sub:sub||""}:null; if (head) { state.status="warn"; state.current={tag:"needs you",text:head,kind:"plan"}; state.feed.push(state.current); setExpanded(true); setView("activity"); } render(); },
    hud(visible) { mount(); state.visible=!!visible; render(); savePrefs(); return state.visible; },
    configure(patch={}) { mount(); if (patch.corner) setCorner(patch.corner); if (patch.view) setView(patch.view); if (patch.expanded!==undefined) setExpanded(patch.expanded); if (Number.isFinite(patch.width)) { state.width=Math.max(MIN_W,Math.min(window.innerWidth-24,patch.width)); els.panel.style.width=state.width+"px"; } if (Number.isFinite(patch.height)) { state.height=Math.max(MIN_H,Math.min(window.innerHeight-24,patch.height)); els.details.style.height=Math.max(state.height-68,120)+"px"; } if (patch.visible!==undefined) state.visible=!!patch.visible; render(); },
    state() { return {x:Math.round(state.x),y:Math.round(state.y),mounted:state.mounted,controlled:state.controlled,visible:state.visible,expanded:state.expanded,corner:state.corner,view:state.view,width:Math.round(state.width),height:Math.round(state.height)}; },
  };
  window.__chamberOverlay=api;
  if (document.readyState==="loading") document.addEventListener("DOMContentLoaded",mount,{once:true}); else mount();
})();
