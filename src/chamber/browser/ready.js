/**
 * chamber — readiness instrumentation.
 *
 * Installed as an init script so it is watching from the first byte. Answers one
 * question: has this page stopped changing?
 *
 * `load` fires when the initial document and its subresources finish, which on a
 * client-rendered app is *before* any content exists — waiting on it gets you an
 * empty shell. `networkidle` is closer but a page with an open websocket, an
 * analytics heartbeat or a polling widget is never idle, so it always times out.
 *
 * What actually correlates with "ready to read" is the DOM going quiet. This tracks
 * the last mutation that changed something visible — attribute churn on a spinner
 * and text nodes inside our own overlay are filtered out, because otherwise a
 * loading animation reads as continuous activity forever.
 */
(() => {
  "use strict";
  if (window.__chamberReady) return;

  const st = {
    lastMutation: performance.now(),
    mutations: 0,
    significant: 0,
    inflightFetch: 0,
    inflightXHR: 0,
    started: performance.now(),
  };

  const IGNORED_ATTRS = new Set([
    "style", "class", "aria-busy", "data-chamber-ref",
  ]);

  function significant(records) {
    for (const r of records) {
      const target = r.target;
      if (target && target.nodeType === Node.ELEMENT_NODE) {
        if (target.closest?.("[data-chamber-overlay]")) continue;
      }
      if (r.type === "attributes") {
        // Spinners animate by rewriting style/class thousands of times. That is
        // motion, not progress.
        if (IGNORED_ATTRS.has(r.attributeName)) continue;
        return true;
      }
      if (r.type === "childList") {
        for (const n of r.addedNodes) {
          if (n.nodeType === Node.ELEMENT_NODE || (n.textContent || "").trim().length > 0) {
            return true;
          }
        }
        if (r.removedNodes.length) return true;
      }
      if (r.type === "characterData") return true;
    }
    return false;
  }

  const observe = () => {
    const target = document.documentElement || document.body;
    if (!target) return;
    new MutationObserver((records) => {
      st.mutations += records.length;
      if (significant(records)) {
        st.significant++;
        st.lastMutation = performance.now();
      }
    }).observe(target, {
      childList: true,
      subtree: true,
      attributes: true,
      characterData: true,
    });
  };

  if (document.documentElement) observe();
  else document.addEventListener("readystatechange", observe, { once: true });

  // In-flight counters. Playwright's request events cover the network layer, but a
  // request that resolves from cache never hits the wire while still gating render,
  // so counting at the API level catches cases the network view misses.
  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (...args) {
      st.inflightFetch++;
      return origFetch.apply(this, args).finally(() => {
        st.inflightFetch = Math.max(0, st.inflightFetch - 1);
        st.lastMutation = performance.now();
      });
    };
  }

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (...args) {
    this.__chamberTracked = true;
    return origOpen.apply(this, args);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    if (this.__chamberTracked) {
      st.inflightXHR++;
      const done = () => {
        st.inflightXHR = Math.max(0, st.inflightXHR - 1);
        st.lastMutation = performance.now();
      };
      this.addEventListener("loadend", done, { once: true });
    }
    return origSend.apply(this, args);
  };

  window.__chamberReady = {
    /** Everything the Python side needs to decide whether to wait longer. */
    probe() {
      const now = performance.now();
      const body = document.body;
      return {
        readyState: document.readyState,
        quietMs: Math.round(now - st.lastMutation),
        mutations: st.mutations,
        significant: st.significant,
        inflight: st.inflightFetch + st.inflightXHR,
        elapsedMs: Math.round(now - st.started),
        // Content volume is the tiebreaker: a "complete", quiet page with 40
        // characters of text is a shell that has not rendered, not a finished page.
        textLength: body ? (body.innerText || "").trim().length : 0,
        nodeCount: document.getElementsByTagName("*").length,
        hasVisibleSpinner: !!document.querySelector(
          '[class*="spinner" i]:not([hidden]), [class*="loading" i]:not([hidden]), ' +
          '[role="progressbar"], [aria-busy="true"]'
        ),
      };
    },
  };
})();
