/**
 * chamber — page extraction.
 *
 * Runs in the page and returns everything one agent step needs to reason about,
 * in one round trip. Called as an IIFE by snapshot.py with an options object.
 *
 * Three ideas drive the design:
 *
 *  1. An element is worth showing only if a human could act on it *right now*.
 *     Not "is in the DOM" — laid out, painted, on screen or reachable by
 *     scrolling, and not buried under a modal. The occlusion check is what
 *     removes the single largest category of agent failure: confidently clicking
 *     an element that a cookie banner is sitting on top of.
 *
 *  2. Refs must survive a re-render. Every element gets both a live handle (fast)
 *     and a re-locatable path with shadow-DOM hops (durable). A framework that
 *     swaps nodes underneath us costs one re-resolve, not a failed step.
 *
 *  3. Text and controls are different products. The model needs a readable digest
 *     to *understand* the page and an indexed control list to *act* on it. Mixing
 *     them into one flat a11y dump is what makes those dumps so long and so hard
 *     to act on.
 */
(function chamberExtract(rawOptions) {
  "use strict";

  const opt = Object.assign(
    {
      maxElements: 220,
      maxTextChars: 6000,
      viewportOnly: false,     // true = only what is currently on screen
      occlusionCheck: true,
      includeIframes: true,
      textDigest: true,
      stampRefs: true,
    },
    rawOptions || {}
  );

  const CHAMBER_ATTR = "data-chamber-ref";
  const OVERLAY_ATTR = "data-chamber-overlay";

  // Registry lives on the page so Python can address elements by ref between
  // calls without re-running extraction.
  const reg = (window.__chamber = window.__chamber || {});
  reg.elements = new Map();
  reg.seq = 0;

  // ---------------------------------------------------------------- utilities

  const trim = (s, n) => {
    if (!s) return "";
    // Zero-width and bidi control characters are invisible on the page but arrive
    // in labels as literal escape sequences, which is noise the model pays for and
    // can misread as part of the text. Written as escapes on purpose: the literal
    // characters would be unreviewable in this source file.
    s = String(s).replace(/[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]/g, "");
    s = s.replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  };

  /**
   * Is this node part of chamber's own overlay?
   *
   * `closest()` does not cross shadow boundaries, and the overlay deliberately
   * lives inside one — so walking up through hosts is required, not defensive.
   * Getting this wrong would let the agent see and click its own HUD.
   */
  function isOurs(el) {
    let node = el;
    let guard = 0;
    while (node && guard++ < 40) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        if (node.hasAttribute?.(OVERLAY_ATTR)) return true;
        const hit = node.closest?.(`[${OVERLAY_ATTR}]`);
        if (hit) return true;
      }
      const root = node.getRootNode?.();
      node = root instanceof ShadowRoot ? root.host : null;
    }
    return false;
  }

  /** Deepest element at a point, following open shadow roots. */
  function deepElementFromPoint(x, y, root) {
    root = root || document;
    let el = root.elementFromPoint(x, y);
    if (!el) return null;
    while (el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  // ------------------------------------------------------------- visibility

  const HIDDEN_INPUT = new Set(["hidden"]);

  function layoutBox(el) {
    // getClientRects handles inline elements that wrap across lines, where
    // getBoundingClientRect returns a union box covering empty space.
    const rects = el.getClientRects();
    if (rects.length === 0) return null;
    let best = rects[0];
    for (const r of rects) if (r.width * r.height > best.width * best.height) best = r;
    return best.width > 0 && best.height > 0 ? best : null;
  }

  // Smaller than this in both dimensions and no human could hit it. A real
  // checkbox is ~13x13, so this only catches the 1x1 and 2x2 inputs sites hide
  // behind styled labels for file pickers and custom controls. They are visible
  // by every CSS measure, which is exactly why they need their own rule — left
  // in, a model eventually types into one and the text goes nowhere.
  const MIN_HIT = 8;

  function visibility(el, style, box) {
    if (!box) return { visible: false, why: "no-box" };
    if (box.width < MIN_HIT && box.height < MIN_HIT)
      return { visible: false, why: "sub-pixel" };
    if (style.visibility === "hidden" || style.visibility === "collapse")
      return { visible: false, why: "visibility" };
    if (style.display === "none") return { visible: false, why: "display" };
    if (parseFloat(style.opacity) === 0) return { visible: false, why: "opacity" };
    if (el.tagName === "INPUT" && HIDDEN_INPUT.has(el.type))
      return { visible: false, why: "input-hidden" };
    // `content-visibility: hidden` and inert subtrees are unreachable even though
    // they lay out. `inert` is the modern way modals disable the page behind them.
    if (el.closest && el.closest("[inert]")) return { visible: false, why: "inert" };
    if (el.checkVisibility && !el.checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true }))
      return { visible: false, why: "checkVisibility" };
    return { visible: true, why: null };
  }

  /**
   * Is something painted on top of this element's click point?
   *
   * Tested at the centre first, then at four inset points, because a large
   * element can be centre-covered by a tooltip while still perfectly clickable
   * near its edge. Returns the point that works, so the executor clicks *there*
   * rather than at a covered centre.
   */
  function hitPoint(el, box, frameOffset) {
    const cx = box.left + box.width / 2;
    const cy = box.top + box.height / 2;
    if (!opt.occlusionCheck) {
      return { x: cx + frameOffset.x, y: cy + frameOffset.y, occluded: false, by: null };
    }

    const inset = Math.min(6, box.width / 4, box.height / 4);
    const candidates = [
      [cx, cy],
      [box.left + inset, box.top + inset],
      [box.right - inset, box.top + inset],
      [box.left + inset, box.bottom - inset],
      [box.right - inset, box.bottom - inset],
    ];

    const root = el.getRootNode();
    const searchRoot = root instanceof ShadowRoot ? root : document;
    let blocker = null;

    for (const [x, y] of candidates) {
      if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
      const top = deepElementFromPoint(x, y, searchRoot);
      if (!top) continue;
      if (isOurs(top)) continue; // chamber's own overlay never counts as a blocker
      if (top === el || el.contains(top) || top.contains(el)) {
        return { x: x + frameOffset.x, y: y + frameOffset.y, occluded: false, by: null };
      }
      if (!blocker) blocker = top;
    }

    return {
      x: cx + frameOffset.x,
      y: cy + frameOffset.y,
      occluded: true,
      by: blocker ? describeBlocker(blocker) : "offscreen",
    };
  }

  function describeBlocker(el) {
    const id = el.id ? "#" + el.id : "";
    const cls = typeof el.className === "string" && el.className
      ? "." + el.className.trim().split(/\s+/).slice(0, 2).join(".")
      : "";
    return trim(el.tagName.toLowerCase() + id + cls, 60);
  }

  // ------------------------------------------------------------ accessible name

  const LABELLABLE = new Set(["INPUT", "SELECT", "TEXTAREA", "BUTTON", "METER", "OUTPUT", "PROGRESS"]);

  function ownText(el) {
    // Text the element itself contributes, ignoring nested interactive children
    // so a card-link does not absorb the label of the button inside it.
    let out = "";
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) out += node.nodeValue;
      else if (node.nodeType === Node.ELEMENT_NODE && !isInteractiveTag(node)) {
        out += " " + (node.innerText || node.textContent || "");
      }
    }
    return out;
  }

  function accessibleName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return trim(aria, 120);

    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const root = el.getRootNode();
      const parts = labelledby
        .split(/\s+/)
        .map((id) => root.getElementById && root.getElementById(id))
        .filter(Boolean)
        .map((n) => n.innerText || n.textContent || "");
      if (parts.length) return trim(parts.join(" "), 120);
    }

    if (LABELLABLE.has(el.tagName) && el.labels && el.labels.length) {
      const t = [...el.labels].map((l) => l.innerText || l.textContent).join(" ");
      if (t.trim()) return trim(t, 120);
    }

    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      const ph = el.getAttribute("placeholder");
      if (ph && ph.trim()) return trim(ph, 120);
      if (el.type === "submit" || el.type === "button") {
        if (el.value) return trim(el.value, 120);
      }
    }

    if (el.tagName === "IMG") {
      const alt = el.getAttribute("alt");
      if (alt && alt.trim()) return trim(alt, 120);
    }

    const text = trim(ownText(el), 120);
    if (text) return text;

    const title = el.getAttribute("title");
    if (title && title.trim()) return trim(title, 120);

    // Icon-only controls: the icon's own label is often the only signal.
    const img = el.querySelector("img[alt], svg title, [aria-label]");
    if (img) {
      const t =
        img.getAttribute?.("alt") ||
        img.getAttribute?.("aria-label") ||
        img.textContent;
      if (t && t.trim()) return trim(t, 120);
    }

    const name = el.getAttribute("name");
    if (name) return trim(name, 120);

    return "";
  }

  // -------------------------------------------------------------- interactivity

  const INTERACTIVE_TAGS = new Set([
    "A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "SUMMARY", "DETAILS",
    "LABEL", "OPTION", "AUDIO", "VIDEO",
  ]);

  const INTERACTIVE_ROLES = new Set([
    "button", "link", "checkbox", "radio", "textbox", "searchbox", "combobox",
    "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab",
    "switch", "slider", "spinbutton", "treeitem", "gridcell", "menu", "menubar",
  ]);

  function isInteractiveTag(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
    if (INTERACTIVE_TAGS.has(el.tagName)) return true;
    const role = el.getAttribute && el.getAttribute("role");
    return !!(role && INTERACTIVE_ROLES.has(role.toLowerCase()));
  }

  function interactivity(el, style) {
    if (INTERACTIVE_TAGS.has(el.tagName)) {
      // A bare <a> with no href is a styling hook, not a link.
      if (el.tagName === "A" && !el.hasAttribute("href") && !el.hasAttribute("onclick"))
        return null;
      // <label> only matters when it drives a control.
      if (el.tagName === "LABEL" && !el.control) return null;
      return el.tagName.toLowerCase();
    }
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (INTERACTIVE_ROLES.has(role)) return role;
    if (el.isContentEditable) {
      // `isContentEditable` is inherited, so every node inside a rich-text editor
      // reports true — ChatGPT's composer is a contenteditable div wrapping a <p>,
      // and reporting both gives the model two refs for one box and a coin-flip
      // about which to type into. Only the outermost one is the field.
      const parent = el.parentElement;
      if (parent && parent.isContentEditable) return null;
      return "textbox";
    }
    if (el.hasAttribute("onclick")) return "button";
    const ti = el.getAttribute("tabindex");
    if (ti !== null && ti !== "-1") return "button";
    // `cursor: pointer` is the last resort and the noisiest signal — accept it
    // only on small elements that carry a label, which filters out the
    // pointer-cursor page wrappers some sites apply.
    if (style.cursor === "pointer") {
      const box = el.getBoundingClientRect();
      if (box.width < 500 && box.height < 200 && !el.querySelector("a,button,input")) {
        return "clickable";
      }
    }
    return null;
  }

  function implicitRole(el, kind) {
    if (el.tagName === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (t === "checkbox" || t === "radio") return t;
      if (t === "submit" || t === "button" || t === "reset") return "button";
      return "textbox";
    }
    if (el.tagName === "A") return "link";
    if (el.tagName === "TEXTAREA") return "textbox";
    if (el.tagName === "SELECT") return el.multiple ? "listbox" : "combobox";
    return kind;
  }

  // --------------------------------------------------------------- element path

  /**
   * A re-locatable path, as a list of hops. Each hop is a selector; a hop can
   * cross a shadow boundary or an iframe boundary, marked by its kind. Python
   * replays this when a live handle has gone stale.
   */
  function elementPath(el) {
    const hops = [];
    let node = el;
    let guard = 0;

    while (node && guard++ < 50) {
      const root = node.getRootNode();
      const scope = root instanceof ShadowRoot ? root : document;
      hops.unshift({ kind: "css", sel: uniqueSelectorWithin(node, scope) });
      if (root instanceof ShadowRoot) {
        node = root.host;
        hops.unshift({ kind: "shadow" });
      } else {
        break;
      }
    }
    return hops;
  }

  function uniqueSelectorWithin(el, scope) {
    // An id is only useful if it is actually unique and not a generated hash that
    // changes on every render — a digit-heavy id is a bad bet for durability.
    if (el.id && !/^\d|[0-9a-f]{8}/i.test(el.id)) {
      const esc = CSS.escape(el.id);
      if (scope.querySelectorAll("#" + esc).length === 1) return "#" + esc;
    }

    const parts = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(part);
        break;
      }
      const siblings = [...parent.children].filter((c) => c.tagName === node.tagName);
      if (siblings.length > 1) {
        part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      if (parts.length >= 6) break;
      node = parent;
      if (node.id && !/^\d|[0-9a-f]{8}/i.test(node.id)) {
        parts.unshift("#" + CSS.escape(node.id));
        break;
      }
    }
    return parts.join(" > ");
  }

  // ------------------------------------------------------------------- the walk

  const collected = [];
  const nodes = [];        // parallel to `collected`; DOM nodes cannot be serialized
  const scrollables = [];
  let scanned = 0;
  let closedShadow = 0;

  function collect(el, frameOffset, framePath, depth) {
    scanned++;
    if (isOurs(el)) return;

    let style;
    try {
      style = getComputedStyle(el);
    } catch {
      return;
    }

    const box = layoutBox(el);
    const vis = visibility(el, style, box);

    // Scroll containers are reported separately — the model needs to know that
    // "scroll down" means this panel, not the window.
    if (box && vis.visible && el.scrollHeight > el.clientHeight + 40 && /auto|scroll/.test(style.overflowY)) {
      scrollables.push({
        path: elementPath(el),
        box: [Math.round(box.left + frameOffset.x), Math.round(box.top + frameOffset.y), Math.round(box.width), Math.round(box.height)],
        scrollTop: Math.round(el.scrollTop),
        scrollHeight: Math.round(el.scrollHeight),
        clientHeight: Math.round(el.clientHeight),
        label: trim(accessibleName(el) || el.tagName.toLowerCase(), 40),
      });
    }

    // Collect this element only if it is itself visible...
    if (vis.visible && box) {
      const kind = interactivity(el, style);
      if (kind) {
        const absTop = box.top + frameOffset.y;
        const absLeft = box.left + frameOffset.x;
        const inViewport =
          absTop < innerHeight && absTop + box.height > 0 && absLeft < innerWidth && absLeft + box.width > 0;

        if (!opt.viewportOnly || inViewport) {
          // Occlusion is only meaningful for something on screen; an off-screen
          // element is "covered" by nothing, it is simply elsewhere.
          const hit = inViewport
            ? hitPoint(el, box, frameOffset)
            : { x: absLeft + box.width / 2, y: absTop + box.height / 2, occluded: false, by: null };

          collected.push(buildRecord(el, kind, box, frameOffset, inViewport, hit, framePath, depth));
          nodes.push(el);
        }
      }
    }

    /**
     * ...but keep descending regardless, unless the subtree genuinely is not
     * rendered.
     *
     * This distinction is load-bearing and getting it wrong is silent. An element
     * having no box does NOT mean its children have none: `display: contents`
     * generates no box of its own while rendering its children normally, and it is
     * everywhere in modern component frameworks. Treating "no box" as "stop" cost
     * us the entire ChatGPT composer — `#prompt-textarea` sits under two
     * `display: contents` wrappers, so the whole subtree was invisible to the
     * agent even though the element was 579x42 and perfectly clickable.
     *
     * Only two conditions actually remove a subtree from the page:
     *   - `display: none`
     *   - `content-visibility: hidden`
     * `visibility: hidden` is not one of them — it inherits, but a child can set
     * `visibility: visible` and reappear, so we must still walk it.
     */
    if (style.display === "none" || style.contentVisibility === "hidden") return;

    // Descend: light DOM, then open shadow roots. A custom element with no
    // reachable shadowRoot is either shadow-free or closed; a closed root is
    // genuinely unreachable from script, so it is counted and reported in stats
    // rather than silently swallowed — "the button isn't in the list" needs an
    // explanation the model can act on.
    for (const child of el.children) collect(child, frameOffset, framePath, depth + 1);
    if (el.shadowRoot) {
      for (const child of el.shadowRoot.children) collect(child, frameOffset, framePath, depth + 1);
    } else if (el.tagName.includes("-") && el.children.length === 0) {
      closedShadow++;
    }
  }

  function buildRecord(el, kind, box, frameOffset, inViewport, hit, framePath, depth) {
    const rec = {
      tag: el.tagName.toLowerCase(),
      role: implicitRole(el, kind),
      name: accessibleName(el),
      box: [
        Math.round(box.left + frameOffset.x),
        Math.round(box.top + frameOffset.y),
        Math.round(box.width),
        Math.round(box.height),
      ],
      point: [Math.round(hit.x), Math.round(hit.y)],
      inViewport,
      occluded: hit.occluded,
      occludedBy: hit.by,
      depth,
      frame: framePath || null,
      path: elementPath(el),
    };

    if (el.tagName === "A" && el.href) {
      rec.href = trim(el.getAttribute("href"), 200);
      rec.hrefAbs = trim(el.href, 300);
      if (el.target === "_blank") rec.newTab = true;
    }
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      rec.inputType = (el.type || "text").toLowerCase();
      if (el.type === "checkbox" || el.type === "radio") rec.checked = !!el.checked;
      else if (el.value) rec.value = trim(el.value, 80);
      if (el.placeholder) rec.placeholder = trim(el.placeholder, 80);
      if (el.required) rec.required = true;
      if (el.maxLength > 0 && el.maxLength < 1e6) rec.maxLength = el.maxLength;
    }
    if (el.tagName === "SELECT") {
      rec.options = [...el.options].slice(0, 25).map((o) => trim(o.textContent, 50));
      rec.selected = trim(el.selectedOptions?.[0]?.textContent ?? "", 50);
    }
    if (el.disabled || el.getAttribute("aria-disabled") === "true") rec.disabled = true;
    const expanded = el.getAttribute("aria-expanded");
    if (expanded !== null) rec.expanded = expanded === "true";
    const checkedAria = el.getAttribute("aria-checked");
    if (checkedAria !== null && rec.checked === undefined) rec.checked = checkedAria === "true";
    const selected = el.getAttribute("aria-selected");
    if (selected !== null) rec.selected = selected === "true";
    if (el === document.activeElement) rec.focused = true;

    return rec;
  }

  // ----------------------------------------------------------------- run the walk

  const t0 = performance.now();
  collect(document.documentElement, { x: 0, y: 0 }, null, 0);

  // ------------------------------------------------------------- ranking & refs

  /**
   * When there are more controls than budget, keep the ones a human would reach
   * for first: on screen, near the top, not occluded, actually labelled.
   */
  function score(rec) {
    let s = 0;
    if (rec.inViewport) s += 1000;
    if (!rec.occluded) s += 300;
    if (rec.name) s += 200;
    if (rec.focused) s += 500;
    if (rec.disabled) s -= 400;
    if (rec.role === "clickable") s -= 150; // weakest signal, lowest priority
    s -= Math.min(rec.box[1] / 4, 250);     // prefer higher on the page
    return s;
  }

  // Records carry their index into `nodes` through the sorts, so the ref → DOM
  // node binding survives reordering.
  collected.forEach((rec, i) => (rec._i = i));

  collected.sort((a, b) => score(b) - score(a));
  const kept = collected.slice(0, opt.maxElements);
  const dropped = collected.length - kept.length;

  // Document order reads far better than score order for a human or a model
  // scanning the list, so restore it after the budget is applied.
  kept.sort((a, b) => a.box[1] - b.box[1] || a.box[0] - b.box[0]);

  if (opt.stampRefs) {
    for (const el of document.querySelectorAll(`[${CHAMBER_ATTR}]`)) {
      el.removeAttribute(CHAMBER_ATTR);
    }
  }

  for (const rec of kept) {
    rec.ref = "e" + ++reg.seq;
    const node = nodes[rec._i];
    delete rec._i;
    if (node) {
      // The fast path for the next action: no path replay needed while the page
      // holds still. resolve() falls back to `rec.path` once it does not.
      reg.elements.set(rec.ref, node);
      if (opt.stampRefs) {
        try { node.setAttribute(CHAMBER_ATTR, rec.ref); } catch { /* svg, etc. */ }
      }
    }
  }
  for (const rec of collected) delete rec._i;

  // --------------------------------------------------------------- text digest

  const SKIP_TEXT = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "SVG", "CANVAS", "TEMPLATE", "IFRAME"]);

  function textDigest() {
    const main =
      document.querySelector("main, [role=main], article") || document.body;
    if (!main) return "";

    const out = [];
    let chars = 0;
    const walker = document.createTreeWalker(main, NodeFilter.SHOW_ELEMENT, {
      acceptNode(node) {
        if (SKIP_TEXT.has(node.tagName)) return NodeFilter.FILTER_REJECT;
        if (isOurs(node)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });

    const BLOCK = /^(H[1-6]|P|LI|TD|TH|DT|DD|BLOCKQUOTE|PRE|FIGCAPTION|CAPTION|SUMMARY)$/;
    let node = walker.currentNode;
    while (node && chars < opt.maxTextChars) {
      if (BLOCK.test(node.tagName)) {
        let style;
        try {
          style = getComputedStyle(node);
        } catch {
          node = walker.nextNode();
          continue;
        }
        if (style.display !== "none" && style.visibility !== "hidden") {
          const t = trim(node.innerText || node.textContent, 400);
          if (t && t.length > 1) {
            const prefix = /^H[1-6]$/.test(node.tagName)
              ? "#".repeat(Number(node.tagName[1])) + " "
              : node.tagName === "LI"
              ? "- "
              : "";
            const line = prefix + t;
            out.push(line);
            chars += line.length;
          }
        }
      }
      node = walker.nextNode();
    }

    // Consecutive duplicates are common in card grids and add nothing.
    const deduped = out.filter((line, i) => line !== out[i - 1]);
    return deduped.join("\n").slice(0, opt.maxTextChars);
  }

  // ------------------------------------------------------------------- assemble

  return {
    url: location.href,
    title: document.title,
    ts: Date.now(),
    readyState: document.readyState,
    viewport: {
      w: innerWidth,
      h: innerHeight,
      scrollX: Math.round(scrollX),
      scrollY: Math.round(scrollY),
      docW: Math.round(document.documentElement.scrollWidth),
      docH: Math.round(document.documentElement.scrollHeight),
    },
    elements: kept,
    scrollables: scrollables.slice(0, 8),
    text: opt.textDigest ? textDigest() : "",
    stats: {
      scanned,
      candidates: collected.length,
      kept: kept.length,
      dropped,
      closedShadowRoots: closedShadow,
      ms: Math.round(performance.now() - t0),
    },
  };
})
