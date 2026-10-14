/* Execute the actual browser script with deterministic DOM, fetch and clocks. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const script = fs.readFileSync("src/vimo_demo/web/static/observer.js", "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.hidden = false; this.value = ""; }
  set textContent(value) { this.value = String(value); this.children = []; }
  get textContent() { return this.value + this.children.map(child => child.textContent).join(" "); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.value = ""; this.children = children; }
  removeAttribute(name) { delete this[name]; }
}

function fixture() {
  const h = { now: 100000, calls: [], active: 0, maxActive: 0, urlCount: 0, urls: new Set(), timers: new Map(), events: {}, nodes: new Map() };
  const get = id => {
    if (!h.nodes.has(id)) h.nodes.set(id, new Element("div"));
    return h.nodes.get(id);
  };
  h.snapshot = (revision = 1) => ({
    publisher_id: "process-one", revision, generation: 1, application_state: "RUNNING", latest_cycle_id: 1,
    published_at: new Date(h.now).toISOString(), configured_camera_ids: ["CAM6", "CAM5", "CAM4", "CAM3", "CAM2", "CAM1"],
    cameras: [6, 5, 4, 3, 2, 1].map(id => ({
      camera_id: `CAM${id}`, image_status: "current", result_status: "matched",
      image_identity: { camera_id: `CAM${id}`, generation: 1, cycle_id: 1, sequence: 7, frame_revision: id, shape: [960, 1056, 3] },
      image_url: `/api/cameras/CAM${id}.jpg?publisher=process-one&revision=${revision}`,
      channels: [{ channel_id: id, business_status: "OK", technical_status: "SUCCESS" }],
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
    navigator: { language: "en" }, Date: Clock, Image, AbortController,
    document: { documentElement: {}, getElementById: get, createElement: tag => new Element(tag) },
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
  h.status = index => h.card(index).children[3].textContent;
  h.channels = index => h.card(index).children[4].textContent;
  h.jpegCalls = () => h.calls.filter(path => path.includes(".jpg"));
  h.stop = () => h.events.pagehide();
  return h;
}

test("configured order and authoritative channel statuses, without camera judgments", async () => {
  const h = fixture();
  h.value.cameras[0].channels[0] = { channel_id: 36, business_status: "NG", technical_status: "ERROR" };
  await h.start();
  assert.deepEqual(h.node("cameras").children.map(card => card.children[0].textContent), h.value.configured_camera_ids);
  assert.equal(h.channels(0), "CH36 NG · ERROR");
  assert.equal(h.node("state").textContent, "RUNNING");
  assert.equal(h.image(0).alt, "CAM6");
  assert.equal(h.maxActive, 1);
  assert.equal(h.urls.size, 6);
  h.stop(); assert.equal(h.urls.size, 0); assert.equal(h.timers.size, 0);
});

test("unchanged source images are reused while matching evidence updates", async () => {
  const h = fixture();
  h.value.cameras[0].result_status = "pending";
  await h.start(); assert.equal(h.channels(0), "");
  const first = h.image(0).src;
  h.value = h.snapshot(2);
  h.value.cameras[0].channels[0].business_status = "NG";
  await h.tick();
  assert.equal(h.channels(0), "CH06 NG");
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
  assert.equal(h.channels(0), ""); assert.equal(h.channels(1), ""); assert.equal(h.channels(2), "");
  assert.match(h.status(1), /stale/); assert.match(h.status(2), /pending/);
  h.value = h.snapshot(2); h.value.configured_camera_ids = []; h.value.cameras = [];
  await h.tick();
  assert.equal(h.node("cameras").children.length, 0); assert.equal(h.node("empty").hidden, false);
  assert.equal(h.urls.size, 0); h.stop();
});

test("410 is a normal race, one attempt then a fresh publication", async () => {
  const h = fixture();
  h.handler = path => h.response(path.includes("CAM6") ? 410 : 200);
  await h.start();
  assert.match(h.card(0).children[1].children[1].textContent, /Publication changed/);
  assert.equal(h.channels(0), ""); assert.equal(h.jpegCalls().length, 6);
  h.value = h.snapshot(2); h.handler = () => h.response();
  await h.tick();
  assert.equal(h.jpegCalls().length, 7); assert.equal(h.image(0).hidden, false);
  assert.equal(h.channels(0), "CH06 OK"); h.stop();
});

test("503, network failure and recovery remain distinct from application state", async () => {
  const h = fixture(); h.handler = () => h.response(503);
  await h.start(); assert.match(h.node("connection").textContent, /Waiting/);
  h.handler = () => h.response(); await h.tick();
  const old = h.image(0).src;
  h.handler = () => { throw new Error("offline"); }; await h.tick();
  assert.match(h.node("connection").textContent, /Connection lost/);
  assert.equal(h.node("state").textContent, "RUNNING"); assert.equal(h.channels(0), "");
  h.value = h.snapshot(2); h.handler = () => h.response(); await h.tick();
  assert.equal(h.image(0).src, old); assert.equal(h.jpegCalls().length, 6);
  assert.equal(h.channels(0), "CH06 OK"); h.stop();
});

test("stale publication and older revision cannot masquerade as current state", async () => {
  const h = fixture(); h.value = h.snapshot(5); await h.start();
  h.value = h.snapshot(4); h.value.application_state = "FAULT";
  await h.tick(); assert.equal(h.node("state").textContent, "RUNNING");
  assert.equal(h.jpegCalls().length, 6);
  h.value = h.snapshot(6); h.value.published_at = new Date(h.now - 20000).toISOString();
  await h.tick(); assert.match(h.node("connection").textContent, /Publication stale/);
  assert.equal(h.channels(0), ""); h.stop();
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
  assert.equal(h.active, 0); assert.match(h.node("connection").textContent, /Connection lost/);
  h.value = h.snapshot(2); h.handler = () => h.response(); await h.tick();
  assert.equal(h.maxActive, 1); assert.equal(h.channels(0), "CH06 OK"); h.stop();
});

test("frame replacement clears old pixels/evidence before delayed JPEG completes", async () => {
  const h = fixture(); await h.start(); const held = deferred();
  h.value = h.snapshot(2); h.value.cameras[0].image_identity.sequence = 8;
  h.value.cameras[1].image_identity.sequence = 9;
  h.handler = path => path.includes("CAM6") ? held.promise : h.response();
  await h.tick();
  assert.equal(h.image(0).hidden, true); assert.equal(h.channels(0), "");
  assert.match(h.card(0).children[2].textContent, /F8/);
  assert.equal(h.image(1).hidden, true); assert.equal(h.channels(1), "");
  held.resolve(h.response()); await flush();
  assert.equal(h.image(0).hidden, false); assert.equal(h.channels(0), "CH06 OK");
  h.stop();
});

test("late image decode after page disposal cannot restore pixels/results", async () => {
  const h = fixture(); const held = deferred(); h.decode = () => held.promise;
  await h.start(); assert.equal(h.urls.size, 1);
  h.stop(); held.resolve(); await flush();
  assert.equal(h.image(0).hidden, true); assert.equal(h.channels(0), "");
  assert.equal(h.urls.size, 0); assert.equal(h.timers.size, 0);
});

test("image decode timeout cancels ownership and permits the next poll", async () => {
  const h = fixture(); const held = deferred(); h.decode = () => held.promise;
  await h.start(); await h.tick(5000);
  assert.equal(h.urls.size, 0); assert.equal(h.image(0).hidden, true);
  h.decode = () => Promise.resolve(); h.value = h.snapshot(2);
  await h.tick(); assert.equal(h.channels(0), "CH06 OK");
  const newest = h.image(0).src;
  held.resolve(); await flush(); assert.equal(h.image(0).src, newest);
  h.stop();
});
