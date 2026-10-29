import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

// Renders the served console.js execution() view against a live overview
// snapshot (2026-10-29) with a minimal DOM double; no browser involved.
const source = await readFile(
  new URL("../src/resource_advisor/static/console.js", import.meta.url),
  "utf8",
);
const snapshot = JSON.parse(
  await readFile(
    new URL("./fixtures/overview-npu-live-20261029.json", import.meta.url),
    "utf8",
  ),
);

class Node {
  constructor(tag) {
    Object.assign(this, { tag, tagName: tag.toUpperCase(), children: [], className: "", attributes: {}, style: {}, dataset: {}, _text: "" });
    this.classList = { add: (c) => (this.className += " " + c), remove() {}, toggle() {}, contains: (c) => this.className.split(" ").includes(c) };
  }
  append(...nodes) { for (const n of nodes) this.children.push(typeof n === "string" ? Object.assign(new Node("#text"), { _text: n }) : n); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  addEventListener() {}
  focus() {}
  get childElementCount() { return this.children.length; }
  get firstElementChild() { return this.children[0] || null; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(" "); }
  findAll(cls, out = []) { if (this.className.split(" ").includes(cls)) out.push(this); this.children.forEach((c) => c.findAll(cls, out)); return out; }
}
const stub = new Proxy(function () {}, { get: (t, k) => (k === Symbol.toPrimitive ? () => "" : stub), set: () => true, apply: () => stub });
const context = vm.createContext({
  document: { createElement: (t) => new Node(t), createTextNode: (t) => Object.assign(new Node("#text"), { _text: t }), getElementById: () => stub, querySelector: () => null, querySelectorAll: () => [], activeElement: null, body: stub, hidden: false, addEventListener() {} },
  window: stub, location: { hash: "#execution", search: "", pathname: "/", href: "https://lab.test/#execution", origin: "https://lab.test", host: "lab.test" }, history: stub, sessionStorage: stub, localStorage: stub,
  fetch: () => new Promise(() => {}), setInterval: () => 0, setTimeout: () => 0, clearTimeout() {}, clearInterval() {}, requestAnimationFrame: () => 0, cancelAnimationFrame() {}, addEventListener() {}, matchMedia: () => ({ matches: false }),
  HTMLElement: Node, Node, performance, crypto, URL, URLSearchParams, AbortController, console, Intl, Date, Promise, Map, Set,
});
vm.runInContext(
  "const createWorkflowWorkspace=()=>new Proxy({},{get:()=>()=>{}}),createPlacementConsole=createWorkflowWorkspace;\n" +
    source.replace(/^import .*$/m, ""),
  context,
);
context.snapshot = snapshot;
vm.runInContext("data = snapshot; receivedAt = performance.now();", context);
const render = (search = "") => vm.runInContext(`resourceBackend = "all"; resourceSearch = ${JSON.stringify(search)}; execution()`, context);
const nodeRefs = (root) => root.findAll("node-card").map((c) => c.findAll("node-card-head")[0].textContent.match(/etri-\S+|ra-\S+|slurm-\S+/)[0]);

test("accelerator signal cards name each live NPU device, key and quantity", () => {
  const cards = Object.fromEntries(render().findAll("accelerator-signal").map((c) => [c.textContent.match(/etri-\S+/)?.[0], c.textContent]));
  for (const [node, words] of [
    ["etri-ser0004-cgnms0", ["NPU", "Mobilint", "ARIES2", "mobilint.com/npu"]],
    ["etri-dev0002-raspi5", ["NPU", "Hailo", "HAILO8", "hailo.ai/h8"]],
    ["etri-dev0006-raspi5", ["NPU", "Hailo", "HAILO8", "hailo.ai/h8"]],
    ["etri-ser0002-cgnmsb", ["NPU", "Intel", "Intel-NPU-3720", "npu.intel.com/accel"]],
    ["etri-dev0004-tedger", ["NPU", "Rockchip", "RK3399Pro", "rockchip.com/rk3399pro"]],
  ]) for (const w of words) assert.ok(cards[node]?.includes(w), `${node} card lacks ${w}: ${cards[node]}`);
  // Detection is stated separately from (missing) utilization and from node readiness.
  assert.match(cards["etri-dev0002-raspi5"], /장치 감지/);
  assert.match(cards["etri-dev0002-raspi5"], /사용률 미측정/);
  assert.match(cards["etri-dev0004-tedger"], /스케줄 불가/);
  assert.doesNotMatch(cards["etri-dev0004-tedger"], /Ready/);
  assert.match(cards["etri-dev0004-tedger"], /장치 보고 · 노드 스케줄 불가/);
  assert.doesNotMatch(cards["etri-dev0004-tedger"], /장치 감지/);
  // Busy Hailo stays listed with its reservation; registered model is not relabeled as measured.
  assert.match(cards["etri-dev0006-raspi5"], /1 예약 \/ 1/);
  assert.match(cards["etri-ser0004-cgnms0"], /등록 모델/);
});

test("model labels come only from the exact node and resource key", () => {
  const card = render().findAll("accelerator-signal").find((c) => c.textContent.includes("etri-ser0002-cgnmsb")).textContent;
  assert.ok(card.includes("Intel-NPU-3720") && card.includes("RTX 5080"));
  assert.ok(!card.includes("ARIES2") && !card.includes("HAILO8"));
});

test("resource search matches class, vendor and model, keeping busy/NotReady nodes", () => {
  assert.deepEqual(nodeRefs(render("NPU")).sort(), ["etri-dev0002-raspi5", "etri-dev0004-tedger", "etri-dev0006-raspi5", "etri-ser0002-cgnmsb", "etri-ser0004-cgnms0", "slurm-npu-lab"]);
  assert.deepEqual(nodeRefs(render("mobilint")), ["etri-ser0004-cgnms0"]);
  assert.deepEqual(nodeRefs(render("ARIES2")), ["etri-ser0004-cgnms0"]);
  assert.deepEqual(nodeRefs(render("모빌인트")), ["etri-ser0004-cgnms0"]);
  assert.deepEqual(nodeRefs(render("hailo")).sort(), ["etri-dev0002-raspi5", "etri-dev0006-raspi5"]);
  assert.deepEqual(nodeRefs(render("rockchip")), ["etri-dev0004-tedger"]);
  assert.deepEqual(nodeRefs(render("intel")), ["etri-ser0002-cgnmsb"]);
});
