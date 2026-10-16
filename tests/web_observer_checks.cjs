/* Execute the actual browser script with deterministic DOM, fetch and clocks. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const script = fs.readFileSync("src/vimo_demo/web/static/observer.js", "utf8");
const html = fs.readFileSync("src/vimo_demo/web/static/observer.html", "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.hidden = false; this.value = ""; }
  set textContent(value) { this.value = String(value); this.children = []; }
  get textContent() { return this.value + this.children.map(child => child.textContent).join(" "); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.value = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this[name]; }
}

function fixture(language = "en") {
  const h = { now: 100000, calls: [], active: 0, maxActive: 0, urlCount: 0, urls: new Set(), timers: new Map(), events: {}, nodes: new Map() };
  for (const match of html.matchAll(/id="([^"]+)"/g)) h.nodes.set(match[1], new Element("div"));
  const get = id => h.nodes.get(id) ?? null;
  h.snapshot = (revision = 1) => ({
    publisher_id: "process-one", revision, generation: 1, application_state: "RUNNING", latest_cycle_id: 1, refresh_interval_ms: 2000,
    published_at: new Date(h.now).toISOString(), configured_camera_ids: ["CAM6", "CAM5", "CAM4", "CAM3", "CAM2", "CAM1"],
    cameras: [6, 5, 4, 3, 2, 1].map(id => ({
      camera_id: `CAM${id}`, image_status: "current", result_status: "matched",
      image_identity: { camera_id: `CAM${id}`, generation: 1, cycle_id: 1, sequence: 7, frame_revision: id, shape: [960, 1056, 3] },
      image_url: `/api/cameras/CAM${id}.jpg?publisher=process-one&revision=${revision}`,
      channels: Array.from({ length: 6 }, (_, slot) => ({ channel_id: (id - 1) * 6 + slot + 1, business_status: "OK", technical_status: "SUCCESS" })),
    })),
  });
  h.value = h.snapshot();
  h.response = (status = 200, value = h.value) => ({
    status, ok: status >= 200 && status < 300,
    json: async () => structuredClone(value), blob: async () => ({ type: "image/jpeg" }),
  });
  h.handler = path => h.response(200);
  h.decode = () => Promise.resolve();
  class Clock extends Date { static now() { return h.now; } }
  class Image { decode() { return h.decode(); } }
  let timerId = 0;
  h.context = vm.createContext({
    navigator: { language }, Date: Clock, Image, AbortController,
    document: { documentElement: {}, getElementById: get, createElement: tag => new Element(tag), createElementNS: (_namespace, tag) => new Element(tag) },
    window: { addEventListener: (name, callback) => { h.events[name] = callback; } },
    setTimeout: (callback, delay) => { const id = ++timerId; h.timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => h.timers.delete(id),
    URL: {
      createObjectURL: () => { const url = `blob:${++h.urlCount}`; h.urls.add(url); return url; },
      revokeObjectURL: url => h.urls.delete(url),
    },
    fetch: async (path, options) => {
      assert.equal(options.cache, "no-store");
      h.calls.push(path); h.active++; h.maxActive = Math.max(h.maxActive, h.active);
      try { return await h.handler(path, options); } finally { h.active--; }
    },
  });
  h.start = async () => { vm.runInContext(script, h.context); await flush(); };
  h.tick = async (delay = 2000) => {
    const found = [...h.timers].find(([, value]) => delay === 5000 ? value.delay === delay : value.delay < 5000);
    assert.ok(found, "Expected one pending timer");
    const [id, value] = found;
    h.timers.delete(id); h.now += value.delay; value.callback(); await flush();
  };
  h.node = get;
  h.card = index => get("cameras").children[index];
  h.image = index => h.card(index).children[1].children[0];
  h.observation = index => h.card(index).children[0].children[1];
  h.indicators = index => h.card(index).children[2].children;
  h.channel = (index, slot = 0) => h.indicators(index)[slot];
  h.neutral = (index, kind) => assert.deepEqual(h.indicators(index).map(label => label.dataset.kind), Array(6).fill(kind));
  h.connection = () => h.node("connection-text").textContent;
  h.jpegCalls = () => h.calls.filter(path => path.includes(".jpg"));
  h.stop = () => h.events.pagehide();
  return h;
}

test("configured order and authoritative channel statuses, without camera judgments", async () => {
  const h = fixture();
  h.value.cameras[0].channels[0] = { channel_id: 36, business_status: "NG", technical_status: "ERROR" };
  await h.start();
  assert.deepEqual(h.node("cameras").children.map(card => card.children[0].children[0].textContent), h.value.configured_camera_ids);
  assert.equal(h.channel(0).title, "CH36 · NG · Technical ERROR");
  assert.equal(h.channel(0).dataset.kind, "ng");
  assert.match(h.channel(0).className, /error/);
  assert.equal(h.connection(), "");
  assert.equal(h.node("fault").hidden, true);
  assert.equal(h.image(0).alt, "CAM6");
  assert.equal(h.maxActive, 1);
  assert.equal(h.urls.size, 6);
  h.stop(); assert.equal(h.urls.size, 0); assert.equal(h.timers.size, 0);
});

test("unchanged source images are reused while matching evidence updates", async () => {
  const h = fixture();
  h.value.cameras[0].result_status = "pending";
  await h.start(); h.neutral(0, "pending");
  const first = h.image(0).src;
  h.value = h.snapshot(2);
  h.value.cameras[0].channels[0].business_status = "NG";
  await h.tick();
  assert.equal(h.channel(0).title, "CH31 · NG · Technical SUCCESS");
  assert.equal(h.channel(0).dataset.kind, "ng");
  assert.equal(h.image(0).src, first);
  assert.equal(h.jpegCalls().length, 6);
  h.stop();
});

for (const field of ["generation", "cycle_id", "sequence", "frame_revision", "shape"]) {
  test(`full image identity changes: ${field}`, async () => {
    const h = fixture(); await h.start();
    const old = h.image(0).src;
    h.value = h.snapshot(2);
    h.value.cameras[0].image_identity[field] = field === "shape" ? [961, 1056, 3] : 99;
    await h.tick();
    assert.notEqual(h.image(0).src, old);
    assert.equal(h.jpegCalls().length, 7);
    assert.ok(!h.urls.has(old));
    h.stop();
  });
}

test("new publisher prevents old process frame/revision aliasing", async () => {
  const h = fixture(); await h.start();
  const old = h.image(0).src;
  h.value = h.snapshot(1); h.value.publisher_id = "process-two";
  for (const row of h.value.cameras) row.image_url = row.image_url.replace("process-one", "process-two");
  await h.tick();
  assert.notEqual(h.image(0).src, old); assert.equal(h.jpegCalls().length, 12);
  h.stop();
});

test("missing, stale and pending observations; no valid configuration", async () => {
  const h = fixture();
  h.value.cameras[0].image_url = null; h.value.cameras[0].image_status = "unavailable";
  h.value.cameras[1].image_url = null; h.value.cameras[1].image_status = "stale"; h.value.cameras[1].result_status = "stale";
  h.value.cameras[2].result_status = "pending";
  await h.start();
  assert.equal(h.jpegCalls().length, 4);
  assert.ok(h.image(0).hidden); assert.ok(h.image(1).hidden);
  h.neutral(0, "unavailable"); h.neutral(1, "unavailable"); h.neutral(2, "pending");
  assert.equal(h.observation(1).hidden, false); assert.match(h.observation(1).title, /Stale/);
  h.value = h.snapshot(2); h.value.configured_camera_ids = []; h.value.cameras = [];
  await h.tick();
  assert.equal(h.node("cameras").children.length, 0); assert.equal(h.node("empty").hidden, false);
  assert.equal(h.urls.size, 0); h.stop();
});

test("410 is a normal race, one attempt then a fresh publication", async () => {
  const h = fixture();
  h.handler = path => h.response(path.includes("CAM6") ? 410 : 200);
  await h.start();
  assert.equal(h.card(0).children[1].children[1].title, "Pending");
  h.neutral(0, "pending"); assert.equal(h.jpegCalls().length, 6);
  h.value = h.snapshot(2); h.handler = () => h.response();
  await h.tick();
  assert.equal(h.jpegCalls().length, 7); assert.equal(h.image(0).hidden, false);
  assert.equal(h.channel(0).dataset.kind, "ok"); h.stop();
});

test("503, network failure and recovery remain distinct from application state", async () => {
  const h = fixture(); h.handler = () => h.response(503);
  await h.start(); assert.match(h.connection(), /Waiting/);
  h.handler = () => h.response(); await h.tick();
  const old = h.image(0).src;
  h.handler = () => { throw new Error("offline"); }; await h.tick();
  assert.equal(h.connection(), "Disconnected");
  assert.equal(h.node("fault").hidden, true); h.neutral(0, "unavailable");
  h.value = h.snapshot(2); h.handler = () => h.response(); await h.tick();
  assert.equal(h.image(0).src, old); assert.equal(h.jpegCalls().length, 6);
  assert.equal(h.channel(0).dataset.kind, "ok"); h.stop();
});

test("stale publication and older revision cannot masquerade as current state", async () => {
  const h = fixture(); h.value = h.snapshot(5); await h.start();
  h.value = h.snapshot(4); h.value.application_state = "FAULT";
  await h.tick(); assert.equal(h.node("fault").hidden, true);
  assert.equal(h.jpegCalls().length, 6);
  h.value = h.snapshot(6); h.value.published_at = new Date(h.now - 20000).toISOString();
  await h.tick(); assert.equal(h.connection(), "Stale observation");
  h.neutral(0, "unavailable"); h.stop();
});

test("slow request has no overlapping polls, aborts once and recovers", async () => {
  const h = fixture(); const held = deferred();
  h.handler = (_path, options) => {
    options.signal.addEventListener("abort", () => held.reject(new Error("aborted")), { once: true });
    return held.promise;
  };
  await h.start();
  assert.equal(h.calls.length, 1); assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].delay, 5000);
  await h.tick(5000);
  assert.equal(h.active, 0); assert.equal(h.connection(), "Disconnected");
  h.value = h.snapshot(2); h.handler = () => h.response(); await h.tick();
  assert.equal(h.maxActive, 1); assert.equal(h.channel(0).dataset.kind, "ok"); h.stop();
});

test("frame replacement clears old pixels/evidence before delayed JPEG completes", async () => {
  const h = fixture(); await h.start(); const held = deferred();
  h.value = h.snapshot(2); h.value.cameras[0].image_identity.sequence = 8;
  h.value.cameras[1].image_identity.sequence = 9;
  h.handler = path => path.includes("CAM6") ? held.promise : h.response();
  await h.tick();
  assert.equal(h.image(0).hidden, true); h.neutral(0, "pending");
  assert.equal(h.image(1).hidden, true); h.neutral(1, "pending");
  held.resolve(h.response()); await flush();
  assert.equal(h.image(0).hidden, false); assert.equal(h.channel(0).dataset.kind, "ok");
  h.stop();
});

test("late image decode after page disposal cannot restore pixels/results", async () => {
  const h = fixture(); const held = deferred(); h.decode = () => held.promise;
  await h.start(); assert.equal(h.urls.size, 1);
  h.stop(); held.resolve(); await flush();
  assert.equal(h.image(0).hidden, true); h.neutral(0, "unavailable");
  assert.equal(h.urls.size, 0); assert.equal(h.timers.size, 0);
});

test("image decode timeout cancels ownership and permits the next poll", async () => {
  const h = fixture(); const held = deferred(); h.decode = () => held.promise;
  await h.start(); await h.tick(5000);
  assert.equal(h.urls.size, 0); assert.equal(h.image(0).hidden, true);
  h.decode = () => Promise.resolve(); h.value = h.snapshot(2);
  await h.tick(); assert.equal(h.channel(0).dataset.kind, "ok");
  const newest = h.image(0).src;
  held.resolve(); await flush(); assert.equal(h.image(0).src, newest);
  h.stop();
});

test("normal header/cards are minimal; metadata and explanations remain API-only", async () => {
  assert.doesNotMatch(html, /<dl>|id="(?:scope|state|cycle|time)(?:-label)?"|<button|<dialog/);
  const h = fixture(); await h.start();
  assert.equal(h.connection(), "");
  assert.match(h.node("connection").title, /Observation connected/);
  assert.equal(h.node("fault").hidden, true);
  for (const id of ["state", "cycle", "time", "scope"]) assert.equal(h.node(id), null);
  for (let camera = 0; camera < 6; camera++) {
    assert.equal(h.card(camera).textContent.replace(/\s+/g, " ").trim(), h.value.configured_camera_ids[camera]);
    assert.equal(h.indicators(camera).length, 6);
    assert.ok(h.observation(camera).hidden);
  }
  h.stop();
});

test("six ordered SVG symbols preserve business state and independent ERROR", async () => {
  const h = fixture();
  const ids = [36, 31, 35, 32, 34, 33];
  const states = [["OK", "SUCCESS"], ["NG", "SUCCESS"], ["EMPTY", "SUCCESS"], ["UNKNOWN", "SUCCESS"], ["NG", "ERROR"], ["UNKNOWN", "ERROR"]];
  h.value.cameras[0].channels = states.map(([business_status, technical_status], index) => ({ channel_id: ids[index], business_status, technical_status }));
  await h.start();
  for (let slot = 0; slot < 6; slot++) {
    const label = h.channel(0, slot);
    assert.equal(label.dataset.kind, states[slot][0].toLowerCase());
    assert.equal(label.title, `CH${ids[slot]} · ${states[slot][0]} · Technical ${states[slot][1]}`);
    assert.equal(label.attributes["aria-label"], label.title);
    assert.equal(label.attributes.role, "img");
    assert.equal(label.className.includes(" error"), states[slot][1] === "ERROR");
    assert.equal(label.children[0].tag, "svg");
    assert.equal(label.children[0].attributes["aria-hidden"], "true");
    assert.equal(label.textContent.trim(), "");
  }
  assert.equal(new Set(h.indicators(0).slice(0, 4).map(label => label.children[0].children[0].attributes.d)).size, 4);
  assert.equal(h.node("fault").hidden, true); // Ordinary check ERROR is not application FAULT.
  h.value = h.snapshot(2);
  h.value.cameras[0].channels[0] = { channel_id: 31, business_status: "EMPTY", technical_status: "ERROR" };
  h.value.cameras[0].channels[1] = { channel_id: 32, business_status: "UNKNOWN", technical_status: "DISABLED" };
  await h.tick();
  assert.equal(h.channel(0).dataset.kind, "empty"); assert.match(h.channel(0).className, /error/);
  assert.equal(h.channel(0, 1).dataset.kind, "unknown"); assert.doesNotMatch(h.channel(0, 1).className, /error/);
  assert.match(h.channel(0, 1).title, /DISABLED/);
  h.stop();
});

test("pending/unavailable symbols differ and never invent ChannelIds", async () => {
  const h = fixture();
  h.value.cameras[0].result_status = "pending"; h.value.cameras[0].channels = [];
  await h.start(); h.neutral(0, "pending");
  const pendingPath = h.channel(0).children[0].children[0].attributes.d;
  for (let slot = 0; slot < 6; slot++) {
    assert.equal(h.channel(0, slot).title, `CAM6 · Slot ${slot + 1} · Pending`);
    assert.doesNotMatch(h.channel(0, slot).title, /CH\d/);
  }
  h.value = h.snapshot(2); h.value.cameras[0].image_url = null; h.value.cameras[0].result_status = "unavailable";
  await h.tick(); h.neutral(0, "unavailable");
  assert.notEqual(h.channel(0).children[0].children[0].attributes.d, pendingPath);
  assert.ok(h.image(0).hidden);
  h.stop();
});

test("retained and older-generation pixels have subtle indicators, not fresh judgments", async () => {
  const h = fixture(); h.value.application_state = "READY";
  for (const row of h.value.cameras) row.image_status = "retained";
  await h.start();
  const original = h.image(0).src;
  assert.equal(h.channel(0).dataset.kind, "ok");
  assert.equal(h.observation(0).hidden, false); assert.equal(h.observation(0).title, "Retained image");
  h.value = h.snapshot(2); h.value.application_state = "READY"; h.value.generation = 2;
  for (const row of h.value.cameras) { row.image_status = "retained"; row.result_status = "stale"; row.channels = []; }
  await h.tick();
  assert.equal(h.image(0).src, original); assert.equal(h.jpegCalls().length, 6);
  assert.equal(h.observation(0).title, "Stale observation");
  h.neutral(0, "unavailable"); h.stop();
});

test("authoritative FAULT stays separate from connection failure, including last-known fault", async () => {
  const h = fixture(); h.value.application_state = "FAULT";
  await h.start();
  assert.equal(h.connection(), ""); assert.equal(h.node("fault").hidden, false);
  assert.equal(h.node("fault").textContent, "Application fault");
  h.handler = () => { throw new Error("offline"); }; await h.tick();
  assert.equal(h.connection(), "Disconnected");
  assert.equal(h.node("fault").textContent, "Application fault · last observed");
  h.neutral(0, "unavailable");
  h.value = h.snapshot(2); h.value.application_state = "READY"; h.handler = () => h.response();
  await h.tick(); assert.equal(h.node("fault").hidden, true); h.stop();
});

test("Chinese feedback and English status tooltips remain accessible", async () => {
  const h = fixture("zh-CN"); await h.start();
  assert.equal(h.connection(), ""); assert.match(h.node("connection").title, /观察已连接/);
  assert.equal(h.channel(0).title, "CH31 · OK · 技术状态 SUCCESS");
  h.value = h.snapshot(2); h.value.application_state = "FAULT";
  await h.tick(); assert.equal(h.node("fault").textContent, "应用故障");
  h.handler = () => { throw new Error("offline"); }; await h.tick();
  assert.equal(h.connection(), "连接中断"); assert.match(h.node("fault").textContent, /最后观察/);
  h.stop();
});

test("no configured images is concise unavailability, never inferred FAULT", async () => {
  const h = fixture(); h.value.configured_camera_ids = []; h.value.cameras = []; h.value.application_state = "UNCONFIGURED";
  await h.start(); assert.equal(h.connection(), "Unavailable"); assert.equal(h.node("fault").hidden, true);
  assert.equal(h.node("empty").hidden, false); assert.equal(h.jpegCalls().length, 0); h.stop();
});

for (const interval of [500, 1000, 2000]) {
  test(`browser follows server refresh interval ${interval} ms without overlapping requests`, async () => {
    const h = fixture(); h.value.refresh_interval_ms = interval;
    await h.start();
    assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [interval]);
    h.value = h.snapshot(2); h.value.refresh_interval_ms = interval;
    h.value.cameras[0].image_identity.frame_revision++;
    await h.tick();
    assert.equal(h.jpegCalls().length, 7);
    assert.equal(h.maxActive, 1);
    assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [interval]);
    h.stop(); assert.equal(h.urls.size, 0); assert.equal(h.timers.size, 0);
  });
}
