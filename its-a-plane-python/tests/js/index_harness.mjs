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
// This regex only matches a BARE <script>. Add `type=`, `defer`, or a
// `</script>` inside a JS string and it silently extracts nothing or too
// little, and the run dies as `ReferenceError: lookupAndTrack is not defined`
// -- which reads like a page bug rather than a harness bug. Say so instead.
if (!/\blookupAndTrack\b/.test(js)) {
  throw new Error(
    "harness could not extract the page script: matched " +
    `${js.length} chars with no lookupAndTrack. If index.html now puts ` +
    "attributes on <script>, widen the regex in this file.");
}

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
  // Counted, not just recorded: asserting `btn.innerHTML === ""` only proves
  // nobody used the sink on THAT element, and the markup that actually bit us
  // would go through a child div. Clearing (v === "") is not a sink.
  set innerHTML(v) {
    this._html = String(v);
    if (v === "") this.children = []; else htmlSinkHits++;
  }
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

let htmlSinkHits = 0;
// Seeded from the page's own id="..." attributes. A lazily-created element for
// ANY id would let a typo'd getElementById keep working here while returning
// null in a browser -- the harness would prove the opposite of what it claims.
const pageIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
const els = {};
const doc = {
  getElementById: id => {
    if (!pageIds.has(id)) return null;
    return (els[id] ||= new El());
  },
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
const responses = { lookup: null, legs: null, current: null, queue: [] };
const sandbox = {
  document: doc,
  // Results are parsed from stdout, so anything the PAGE logs would be mixed
  // into the JSON. Send it to stderr, where it is still visible on failure.
  console: { ...console, log: (...a) => console.error("[page]", ...a) },
  setInterval: () => 0, setTimeout: (f, t) => setTimeout(f, t),
  fetch: async (url, opts) => {
    const u = String(url), body = opts && opts.body ? JSON.parse(opts.body) : null;
    posted.push({ url: u, body });
    if (u.includes("/tracked/lookup")) return { json: async () => responses.lookup };
    if (u.includes("/tracked/legs")) return { json: async () => responses.legs };
    if (u.includes("/tracked/queue")) return { json: async () => ({ message: "ok", queue: responses.queue || [] }) };
    if (u.includes("/tracked/json")) return { json: async () => (responses.current || { callsign: "", queue: [] }) };
    if (u.includes("/tracked/set")) return { json: async () => ({ message: "ok" }) };
    return { json: async () => ({}) };
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(js, sandbox);

const api = vm.runInContext(
  "({lookupAndTrack, lookupAndQueue, showLegs, showQueue, pickLeg, " +
  "loadOtherLegs, saveCallsign, removeQueued})", sandbox);

const setsOnly = () => posted.filter(p => p.url.includes("/tracked/set")).map(p => p.body);
const reset = () => { posted.length = 0; doc.getElementById("callsign-input").value = ""; };
const results = {};

// 1. multi-leg result must NOT auto-save; it must render a picker
reset();
responses.lookup = { found: true, multiple: true, callsign: "UAL1714", summary: "2 legs found — select one",
  flights: [
    { callsign:"UAL1714", origin:"LGA", destination:"DEN", dep_time:"07:29", status:"active",
      airline_name:"United Express",
      cached_route:{origin:"LGA",destination:"DEN"}, scheduled_departure: 1 },
    { callsign:"UAL1714", origin:"DEN", destination:"GJT", dep_time:"11:34", status:"scheduled",
      airline_name:"United Express",
      cached_route:{origin:"DEN",destination:"GJT"}, scheduled_departure: 2 },
  ]};
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndTrack();
results.multi_savedNothing = setsOnly().length === 0;
results.multi_buttons = doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn").length;
// Two legs of a codeshare share a callsign and can share a route line; the
// operator is what tells them apart, so it has to actually render.
results.multi_showsAirline = doc.getElementById("leg-picker").children
  .filter(c => c.className === "leg-btn")
  .every(c => c.textContent.includes("United Express"));

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

// 6. markup in ANY server-supplied field must not reach an HTML sink.
// Every one of these five is relayed from AirLabs, and the sub-line fields
// (dep_time, status) are rendered by a different element than the label, so a
// leg carrying only an `origin` never even builds that element.
reset();
const XSS = '<img src=x onerror="boom()">';
htmlSinkHits = 0;
api.showLegs([{ callsign: XSS, origin: XSS, destination: XSS,
                dep_time: XSS, status: XSS }]);
results.xss_noHtmlSinkUsed = htmlSinkHits === 0;
const _b = doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn")[0];
// and the markup must still be VISIBLE as text, not silently dropped
results.xss_markupRenderedAsText = !!_b && _b.textContent.split(XSS).length - 1 >= 4;

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

// 8-10. The on-demand "other legs" link. A live match never consulted AirLabs,
// so a continuation is invisible until asked for -- and asking costs a credit,
// which is why it is a link and not automatic. None of this had any coverage.
reset();
responses.lookup = { found: true, callsign: "UAL1714", summary: "UA1714 LGA→DEN",
  origin: "LGA", destination: "DEN",
  cached_route: { origin: "LGA", destination: "DEN" }, scheduled_departure: 99 };
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndTrack();
const _link = doc.getElementById("other-legs").children[0];
results.otherLegs_linkOffered = !!_link && _link.textContent.includes("UAL1714");
// Nothing is spent until the link is clicked.
results.otherLegs_noCreditBeforeClick =
  posted.filter(p => p.url.includes("/tracked/legs")).length === 0;
// A live single match legitimately saves at lookup time; what must NOT happen
// is the link changing the tracked flight merely by being consulted.
const _savesBeforeClick = setsOnly().length;

// Clicking asks the server, and the leg we already matched is not offered back.
responses.legs = { legs: [
  { callsign:"UAL1714", origin:"LGA", destination:"DEN",
    cached_route:{origin:"LGA",destination:"DEN"}, scheduled_departure: 99 },
  { callsign:"UAL1714", origin:"DEN", destination:"GJT",
    cached_route:{origin:"DEN",destination:"GJT"}, scheduled_departure: 2 },
]};
_link.click();
await new Promise(r => setTimeout(r, 50));
results.otherLegs_asked =
  posted.filter(p => p.url.includes("/tracked/legs"))
        .map(p => p.body && p.body.callsign)[0] || null;
const _offered = doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn");
results.otherLegs_excludesMatchedLeg =
  _offered.length === 1 && _offered[0].textContent.includes("GJT")
  && !_offered[0].textContent.includes("LGA");
// Offering other legs is not switching to one: only a click on a LEG does that.
results.otherLegs_askingChangesNothing = setsOnly().length === _savesBeforeClick;

// A flight with no continuation must say so, not render an empty picker.
reset();
responses.lookup = { found: true, callsign: "UAL1714", summary: "UA1714 LGA→DEN",
  origin: "LGA", destination: "DEN",
  cached_route: { origin: "LGA", destination: "DEN" }, scheduled_departure: 99 };
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndTrack();
responses.legs = { legs: [
  { callsign:"UAL1714", origin:"LGA", destination:"DEN",
    cached_route:{origin:"LGA",destination:"DEN"}, scheduled_departure: 99 },
]};
doc.getElementById("other-legs").children[0].click();
await new Promise(r => setTimeout(r, 50));
results.otherLegs_noneSaysSo =
  doc.getElementById("other-legs").textContent.includes("No other legs")
  && doc.getElementById("leg-picker").children
       .filter(c => c.className === "leg-btn").length === 0;

// 11-13. Queueing a connection. Same lookup path as tracking, so the picker
// and every other branch work for a queued leg too — only the endpoint differs.
const queuedOnly = () => posted.filter(p => p.url.includes("/tracked/queue")
                                         && !p.url.includes("remove")).map(p => p.body);
reset();
responses.lookup = { found: true, callsign: "UAL1714", summary: "UA1714 DEN→GJT",
  origin: "DEN", destination: "GJT",
  cached_route: { origin: "DEN", destination: "GJT" }, scheduled_departure: 7 };
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndQueue();
results.queue_wentToQueueEndpoint = queuedOnly().length === 1 && setsOnly().length === 0;
results.queue_payload = queuedOnly()[0] || null;

// Queue mode must not stick: the next plain Track has to track, not queue.
reset();
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndTrack();
results.queue_modeDidNotStick = setsOnly().length === 1 && queuedOnly().length === 0;

// A multi-leg lookup while queueing still shows the picker, and picking a leg
// queues THAT leg rather than tracking it.
reset();
responses.lookup = { found: true, multiple: true, callsign: "UAL1714", summary: "2 legs",
  flights: [
    { callsign:"UAL1714", origin:"LGA", destination:"DEN",
      cached_route:{origin:"LGA",destination:"DEN"}, scheduled_departure: 1 },
    { callsign:"UAL1714", origin:"DEN", destination:"GJT",
      cached_route:{origin:"DEN",destination:"GJT"}, scheduled_departure: 2 },
  ]};
doc.getElementById("callsign-input").value = "UA1714";
await api.lookupAndQueue();
const _qbtns = doc.getElementById("leg-picker").children.filter(c => c.className === "leg-btn");
results.queue_pickerStillWorks = _qbtns.length === 2;

// The queue is listed for the user, with what each leg is.
reset();
api.showQueue([
  { callsign: "UAL1714", cached_route: { origin: "DEN", destination: "GJT" } },
  { callsign: "UAL22", cached_route: { origin: "GJT", destination: "LAX" } },
]);
const _rows = doc.getElementById("queue-list").children.filter(c => c.className === "queue-item");
results.queue_listed = _rows.length === 2
  && _rows[0].textContent.includes("DEN") && _rows[0].textContent.includes("GJT");

// Removing a queued leg asks the server for that position.
_rows[1].children.filter(c => c.className === "queue-rm")[0].click();
await new Promise(r => setTimeout(r, 50));
results.queue_removeIndex = (posted.filter(p => p.url.includes("/tracked/queue/remove"))
                                   .map(p => p.body && p.body.index)[0]);

console.log(JSON.stringify(results, null, 2));
