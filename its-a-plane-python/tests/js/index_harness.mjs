// Runs web/templates/index.html's <script> under a minimal DOM stub so the
// tracking flow can be asserted without a browser.
//
// Exists because a review found two bugs here that no test could see: the
// lookup tested `found` before `multiple` (so a multi-leg result auto-saved and
// printed "select one" as a success), and saveCallsign dropped cached_route
// (so the chosen leg — which is carried ONLY by that field, since every leg of
// one flight number shares a callsign — never reached the server).
//
// Usage: node index_harness.mjs <path-to-index.html>   → prints JSON results.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(process.argv[2], "utf8");
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");

class El {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.className = "";
    this._text = ""; this.style = {}; this.value = ""; this._html = "";
    this._listeners = {};
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this.children.length
      ? this._text + this.children.map(c => c.textContent).join("")
      : this._text;
  }
  set innerHTML(v) { this._html = String(v); if (v === "") this.children = []; }
  get innerHTML() { return this._html; }
  get innerText() { return this.textContent; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(ev, fn) { (this._listeners[ev] ||= []).push(fn); }
  click() { (this._listeners.click || []).forEach(fn => fn({})); }
  querySelectorAll(sel) {
    const want = sel.trim().split(/\s+/).pop().replace(/^\./, "");
    return this.children.filter(c => c.className === want);
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const els = {};
const doc = {
  getElementById: id => (els[id] ||= new El()),
  createElement: tag => new El(tag),
  // Text nodes are how the picker avoids an innerHTML sink, so the stub has to
  // model them or the very code path under test throws.
  createTextNode: t => ({ textContent: String(t), className: "", children: [],
                          innerHTML: "" }),
  addEventListener: () => {},           // never fire DOMContentLoaded
  querySelector: () => new El(),
  querySelectorAll: sel => {
    const want = sel.trim().split(/\s+/).pop().replace(/^\./, "");
    return Object.values(els).flatMap(e => e.children.filter(c => c.className === want));
  },
};

const posted = [];
const responses = { lookup: null, legs: null };
const sandbox = {
  document: doc, console,
  setInterval: () => 0, setTimeout: (f, t) => setTimeout(f, t),
  fetch: async (url, opts) => {
    const u = String(url), body = opts && opts.body ? JSON.parse(opts.body) : null;
    posted.push({ url: u, body });
    if (u.includes("/tracked/lookup")) return { json: async () => responses.lookup };
    if (u.includes("/tracked/legs")) return { json: async () => responses.legs };
    if (u.includes("/tracked/set")) return { json: async () => ({ message: "ok" }) };
    return { json: async () => ({}) };
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(js, sandbox);

const api = vm.runInContext(
  "({lookupAndTrack, showLegs, pickLeg, loadOtherLegs, saveCallsign})", sandbox);

const setsOnly = () => posted.filter(p => p.url.includes("/tracked/set")).map(p => p.body);
const reset = () => { posted.length = 0; doc.getElementById("callsign-input").value = ""; };
const results = {};

// 1. multi-leg result must NOT auto-save; it must render a picker
reset();
responses.lookup = { found: true, multiple: true, callsign: "UAL1714", summary: "2 legs found — select one",
  flights: [
    { callsign:"UAL1714", origin:"LGA", destination:"DEN", dep_time:"07:29", status:"active",
      cached_route:{origin:"LGA",destination:"DEN"}, scheduled_departure: 1 },
    { callsign:"UAL1714", origin:"DEN", destination:"GJT", dep_time:"11:34", status:"scheduled",
      cached_route:{origin:"DEN",destination:"GJT"}, scheduled_departure: 2 },
  ]};
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndTrack();
results.multi_savedNothing = setsOnly().length === 0;
results.multi_buttons = doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn").length;

// 2. picking the SECOND leg sends that leg's route, not a bare callsign.
// Guarded: if step 1 rendered no picker this must still report, so the failure
// names the payload assertion rather than dying inside the harness.
reset();
const _btns = doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn");
if (_btns[1]) {
  _btns[1].click();
  await new Promise(r => setTimeout(r, 50));
}
results.pick_payload = setsOnly()[0] || null;

// 3. a live single result must forward cached_route too
reset();
responses.lookup = { found: true, callsign: "UAL1714", summary: "UA1714 LGA→DEN",
  origin:"LGA", destination:"DEN",
  cached_route: { origin:"LGA", destination:"DEN" }, scheduled_departure: 99 };
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndTrack();
results.single_payload = setsOnly()[0] || null;

// 4. `multiple` carrying exactly one leg must save THAT leg, not a bare callsign
reset();
responses.lookup = { found: true, multiple: true, callsign: "UAL1714", summary: "1 legs found",
  flights: [{ callsign:"UAL1714", origin:"DEN", destination:"GJT",
              cached_route:{origin:"DEN",destination:"GJT"}, scheduled_departure: 7 }]};
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndTrack();
results.oneLegMultiple_payload = setsOnly()[0] || null;

// 5. not-found must save nothing and clear any previous picker
reset();
api.showLegs([{callsign:"X", origin:"A", destination:"B"}]);
responses.lookup = { found: false };
doc.getElementById("callsign-input").value = "ZZ9999";
await api.lookupAndTrack();
results.notFound_savedNothing = setsOnly().length === 0;
results.notFound_legsCleared =
  doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn").length === 0;

// 6. markup in a server field must not become HTML
reset();
api.showLegs([{callsign:"X", origin:'<img src=x onerror="boom()">', destination:"DEN"}]);
const b = doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn")[0];
results.xss_textNotMarkup = b.textContent.includes("<img") && b.innerHTML === "";

// 7. Track pressed with an EMPTY box must still drop a stale picker.
// lookupAndTrack returns early on empty input, so the per-branch clears never
// run -- only the clear at the top of the function covers this, and without it
// the leftover buttons stay clickable under a status line about another flight.
reset();
api.showLegs([{callsign:"X", origin:"A", destination:"B"}]);
doc.getElementById("callsign-input").value = "   ";
await api.lookupAndTrack();
results.emptyInput_legsCleared =
  doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn").length === 0;
results.emptyInput_savedNothing = setsOnly().length === 0;

console.log(JSON.stringify(results, null, 2));
