/* Execute the actual browser script with deterministic DOM, fetch and clocks. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const script = fs.readFileSync("src/capsule_vision/web/static/observer.js", "utf8");
const html = fs.readFileSync("src/capsule_vision/web/static/observer.html", "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

class Element {
  constructor(tag) {
    this.writes = 0; this.tag = tag; this.children = []; this.attributes = {}; this.style = {}; this.hidden = false; this.value = ""; this.remounts = 0;
    this.dataset = new Proxy({}, { set: (data, name, value) => { this.writes++; data[name] = value; return true; }, deleteProperty: (data, name) => { if (name in data) this.writes++; return delete data[name]; } });
  }
  set hidden(value) { this.writes++; this._hidden = value; }
  get hidden() { return this._hidden; }
  set className(value) { this.writes++; this._className = value; }
  get className() { return this._className; }
  set title(value) { this.writes++; this._title = value; }
  get title() { return this._title; }
  set textContent(value) { this.writes++; this.value = String(value); this.children = []; }
  get textContent() { return this.value + this.children.map(child => child.textContent).join(" "); }
  append(...children) { this.writes++; this.children.push(...children); }
  replaceChildren(...children) { this.writes++; this.value = ""; this.children = children; this.remounts++; }
  replaceChild(child, previous) { const index = this.children.indexOf(previous); assert.notEqual(index, -1); this.children[index] = child; }
  setAttribute(name, value) { this.writes++; this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { this.writes++; if (name === "title") delete this._title; else delete this[name]; delete this.attributes[name]; }
}

function fixture(language = "en") {
  const h = { now: 100000, calls: [], active: 0, maxActive: 0, urlCount: 0, urls: new Set(), maxUrls: 0, timers: new Map(), events: {}, nodes: new Map() };
  for (const match of html.matchAll(/id="([^"]+)"/g)) h.nodes.set(match[1], new Element("div"));
  const get = id => h.nodes.get(id) ?? null;
  get("cameras").clientWidth = 1912; get("cameras").clientHeight = 1072;
  const imageUrl = (snapshot, row) => {
    const id=row.image_identity;
    return `/api/cameras/${encodeURIComponent(row.camera_id)}.jpg?publisher=${encodeURIComponent(snapshot.publisher_id)}&image=${[id.image_epoch,id.generation,id.cycle_id,id.sequence,id.frame_revision,...id.shape.slice(0,2)].join("-")}`;
  };
  h.snapshot = (revision = 1) => {
    const snapshot = {
    publisher_id: "process-one", revision, generation: 1, application_state: "RUNNING", latest_cycle_id: 1, refresh_interval_ms: 2000,
    published_at: new Date(h.now).toISOString(), configured_camera_ids: ["CAM6", "CAM5", "CAM4", "CAM3", "CAM2", "CAM1"],
    cameras: [6, 5, 4, 3, 2, 1].map(id => ({
      camera_id: `CAM${id}`, image_status: "current", result_status: "matched",
      image_identity: { image_epoch:id, camera_id: `CAM${id}`, generation: 1, cycle_id: 1, sequence: 7, frame_revision: id, shape: [960, 1056, 3] },
      jpeg_dimensions: { width: 640, height: 582 },
      channels: Array.from({ length: 6 }, (_, slot) => ({ channel_id: (id - 1) * 6 + slot + 1, business_status: "OK", technical_status: "SUCCESS" })),
    })),
    };
    for(const row of snapshot.cameras)row.image_url=imageUrl(snapshot,row);
    return snapshot;
  };
  h.value = h.snapshot();
  h.updateUrls = () => { for(const row of h.value.cameras) if(row.image_url && row.image_identity)row.image_url=imageUrl(h.value,row); };
  h.response = (status = 200, value = h.value) => ({
    status, ok: status >= 200 && status < 300,
    json: async () => structuredClone(value), blob: async () => ({ type: "image/jpeg" }),
  });
  h.handler = path => h.response(200);
  h.decode = () => Promise.resolve();
  class Clock extends Date { static now() { return h.now; } }
  class Image extends Element {
    constructor() { super("img"); this.naturalWidth = 640; this.naturalHeight = 582; }
    decode() { return h.decode(this); }
  }
  let timerId = 0;
  h.context = vm.createContext({
    navigator: { language }, Date: Clock, Image, AbortController,
    document: { documentElement: {}, getElementById: get, createElement: tag => new Element(tag), createElementNS: (_namespace, tag) => new Element(tag) },
    window: { addEventListener: (name, callback) => { h.events[name] = callback; } },
    setTimeout: (callback, delay) => { const id = ++timerId; h.timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => h.timers.delete(id),
    URL: {
      createObjectURL: () => { const url = `blob:${++h.urlCount}`; h.urls.add(url); h.maxUrls = Math.max(h.maxUrls, h.urls.size); return url; },
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
  h.presentationWrites = index => [h.card(index), ...h.card(index).children, ...h.card(index).children[0].children, ...h.card(index).children[1].children, ...h.indicators(index)].map(node => node.writes);
  h.resize = (width, height) => { get("cameras").clientWidth = width - 8; get("cameras").clientHeight = height - 8; h.events.resize(); };
  h.stop = () => h.events.pagehide();
  return h;
}

test("configured order and authoritative channel statuses, without camera judgments", async () => {
  const h = fixture();
  h.value.cameras[0].channels[0] = { channel_id: 36, business_status: "NG", technical_status: "ERROR" };
  h.value.cameras[0].channels[5].channel_id = 31;
  await h.start();
  assert.deepEqual(h.node("cameras").children.map(card => card.children[0].children[0].textContent), h.value.configured_camera_ids);
  assert.equal(h.channel(0).attributes["aria-label"], "CH36 · NG · Technical ERROR");
  assert.equal(h.channel(0).dataset.kind, "ng");
  assert.match(h.channel(0).className, /error/);
  assert.equal(h.connection(), "");
  assert.equal(h.node("fault").hidden, true);
  assert.equal(h.image(0).alt, "CAM6");
  assert.equal(h.maxActive, 1);
  assert.equal(h.urls.size, 6);
  h.stop(); assert.equal(h.urls.size, 0); assert.equal(h.timers.size, 0);
});

test("initial incomplete image stays neutral; matched pixels are reused for later evidence", async () => {
  const h = fixture();
  h.value.cameras[0].result_status = "pending";
  await h.start(); h.neutral(0, "unavailable");
  assert.equal(h.image(0).hidden, true); assert.equal(h.jpegCalls().length, 5);
  h.value = h.snapshot(2);
  h.value.cameras[0].channels[0].business_status = "NG";
  await h.tick();
  assert.equal(h.channel(0).attributes["aria-label"], "CH31 · NG · Technical SUCCESS");
  assert.equal(h.channel(0).dataset.kind, "ng");
  const first = h.image(0).src;
  assert.equal(h.jpegCalls().length, 6);
  h.value = h.snapshot(3); h.value.cameras[0].channels[0].business_status = "EMPTY";
  await h.tick(); assert.equal(h.image(0).src, first); assert.equal(h.jpegCalls().length, 6);
  assert.equal(h.channel(0).dataset.kind, "empty");
  h.stop();
});

for (const field of ["image_epoch", "generation", "cycle_id", "sequence", "frame_revision", "shape"]) {
  test(`full image identity changes: ${field}`, async () => {
    const h = fixture(); await h.start();
    const old = h.image(0).src;
    h.value = h.snapshot(2);
    h.value.cameras[0].image_identity[field] = field === "shape" ? [961, 1056, 3] : 99;
    if (field === "generation") h.value.generation = 99;
    h.updateUrls();
    await h.tick();
    assert.notEqual(h.image(0).src, old);
    assert.equal(h.jpegCalls().length, 7);
    assert.equal(h.channel(0).dataset.kind, "ok");
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
  h.value.cameras[0].image_identity = null; h.value.cameras[0].result_status = "unavailable"; h.value.cameras[0].channels = [];
  h.value.cameras[1].image_url = null; h.value.cameras[1].image_status = "stale"; h.value.cameras[1].result_status = "stale";
  h.value.cameras[2].result_status = "pending";
  await h.start();
  assert.equal(h.jpegCalls().length, 3);
  assert.ok(h.image(0).hidden); assert.ok(h.image(1).hidden);
  h.neutral(0, "unavailable"); h.neutral(1, "unavailable"); h.neutral(2, "unavailable");
  assert.ok(h.image(2).hidden);
  assert.equal(h.observation(0).hidden, true);
  assert.equal(h.observation(1).hidden, false); assert.match(h.observation(1).attributes["aria-label"], /Stale/);
  h.value = h.snapshot(2); h.value.configured_camera_ids = []; h.value.cameras = [];
  await h.tick();
  assert.equal(h.node("cameras").children.length, 0); assert.equal(h.node("empty").hidden, false);
  assert.equal(h.urls.size, 0); h.stop();
});

test("410 is a normal race, one attempt then a fresh publication", async () => {
  const h = fixture();
  h.handler = path => h.response(path.includes("CAM6") ? 410 : 200);
  await h.start();
  assert.equal(h.card(0).children[1].children[1].attributes["aria-label"], "Image unavailable");
  h.neutral(0, "unavailable"); assert.equal(h.jpegCalls().length, 6);
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
  await h.tick(); await h.tick(); await h.tick();
  assert.equal(h.connection(), "Stale observation"); h.neutral(0, "unavailable");
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

test("delayed JPEG keeps complete observations quiet until each replacement is ready", async () => {
  const h = fixture(); await h.start(); const held = deferred();
  const original = h.image(0), other = h.image(1);
  h.value = h.snapshot(2); h.value.cameras[0].image_identity.sequence = 8;
  h.value.cameras[1].image_identity.sequence = 9;
  h.updateUrls();
  h.handler = path => path.includes("CAM6") ? held.promise : h.response();
  const writes = h.presentationWrites(0);
  await h.tick();
  assert.equal(h.image(0), original); assert.equal(original.hidden, false); assert.equal(h.channel(0).dataset.kind, "ok");
  assert.equal(h.image(1), other); assert.equal(other.hidden, false); assert.equal(h.channel(1).dataset.kind, "ok");
  assert.equal(h.observation(0).hidden, true); assert.equal(h.observation(1).hidden, true);
  assert.deepEqual(h.presentationWrites(0), writes);
  assert.ok(h.urls.has(original.src));
  held.resolve(h.response()); await flush();
  assert.equal(h.image(0).hidden, false); assert.equal(h.channel(0).dataset.kind, "ok");
  assert.notEqual(h.image(0), original); assert.ok(!h.urls.has(original.src));
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
  assert.doesNotMatch(html, /<h1|<dl>|id="(?:scope|state|cycle|time)(?:-label)?"|<button|<dialog/);
  const h = fixture(); await h.start();
  assert.equal(h.connection(), "");
  assert.match(h.node("connection").attributes["aria-label"], /Observation connected/);
  assert.equal(h.node("fault").hidden, true);
  for (const id of ["state", "cycle", "time", "scope"]) assert.equal(h.node(id), null);
  for (let camera = 0; camera < 6; camera++) {
    assert.equal(h.card(camera).textContent.replace(/\s+/g, " ").trim(), h.value.configured_camera_ids[camera]);
    assert.equal(h.indicators(camera).length, 6);
    assert.ok(h.observation(camera).hidden);
    assert.equal(h.observation(camera).className, "observation");
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
    assert.equal(label.attributes["aria-label"], `CH${ids[slot]} · ${states[slot][0]} · Technical ${states[slot][1]}`);
    assert.equal(label.title, `CH${ids[slot]}`);
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
  assert.match(h.channel(0, 1).attributes["aria-label"], /DISABLED/);
  h.stop();
});

test("initial pending work is neutral and never invents ChannelIds or rebuilds its symbols", async () => {
  const h = fixture();
  h.value.cameras[0].result_status = "pending"; h.value.cameras[0].channels = [];
  await h.start(); h.neutral(0, "unavailable");
  const neutralIcon = h.channel(0).children[0];
  for (let slot = 0; slot < 6; slot++) {
    assert.equal(h.channel(0, slot).attributes["aria-label"], `CAM6 · Slot ${slot + 1} · Unavailable`);
    assert.doesNotMatch(h.channel(0, slot).attributes["aria-label"], /CH\d/);
  }
  h.value = h.snapshot(2); h.value.cameras[0].image_url = null; h.value.cameras[0].result_status = "unavailable";
  await h.tick(); h.neutral(0, "unavailable");
  assert.equal(h.channel(0).children[0], neutralIcon);
  assert.ok(h.image(0).hidden);
  h.stop();
});

test("retained and older-generation pixels have subtle indicators, not fresh judgments", async () => {
  const h = fixture(); h.value.application_state = "READY";
  for (const row of h.value.cameras) row.image_status = "retained";
  await h.start();
  const original = h.image(0).src;
  assert.equal(h.channel(0).dataset.kind, "ok");
  assert.equal(h.observation(0).hidden, false); assert.equal(h.observation(0).attributes["aria-label"], "Retained image");
  h.node("connection").title = "stale telemetry";
  h.observation(0).title = "stale runtime detail";
  h.value = h.snapshot(2); h.value.application_state = "READY"; h.value.generation = 2;
  for (const row of h.value.cameras) { row.image_status = "retained"; row.result_status = "stale"; row.channels = []; }
  await h.tick();
  assert.equal(h.image(0).src, original); assert.equal(h.jpegCalls().length, 6);
  assert.equal(h.observation(0).attributes["aria-label"], "Stale observation");
  assert.equal(h.observation(0).title, undefined);
  assert.equal(h.node("connection").title, undefined);
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

test("localized feedback and icon states remain accessible without runtime titles", async () => {
  const h = fixture("zh-CN"); await h.start();
  assert.equal(h.connection(), ""); assert.match(h.node("connection").attributes["aria-label"], /观察已连接/);
  assert.equal(h.channel(0).attributes["aria-label"], "CH31 · OK · 技术状态 SUCCESS");
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
    h.updateUrls();
    await h.tick();
    assert.equal(h.jpegCalls().length, 7);
    assert.equal(h.maxActive, 1);
    assert.deepEqual([...h.timers.values()].map(timer => timer.delay), [interval]);
    h.stop(); assert.equal(h.urls.size, 0); assert.equal(h.timers.size, 0);
  });
}

test("stable camera, picture and channel nodes across repeated publications and decoded replacements", async () => {
  const h = fixture(); await h.start();
  const wall = h.node("cameras"), roots = [...wall.children], pictures = roots.map(root => root.children[1]);
  const labels = h.indicators(0).slice();
  for (let revision = 2; revision <= 5; revision++) {
    h.value = h.snapshot(revision); h.value.cameras[0].image_identity.sequence = revision + 10;
    h.updateUrls();
    await h.tick();
    wall.children.forEach((root, index) => assert.equal(root, roots[index]));
    roots.forEach((root, index) => assert.equal(root.children[1], pictures[index]));
    h.indicators(0).forEach((label, index) => assert.equal(label, labels[index]));
    assert.equal(h.image(0).hidden, false);
    assert.equal(h.urls.size, 6);
  }
  assert.equal(wall.remounts, 1);
  assert.equal(h.maxUrls, 7); h.stop(); assert.equal(h.urls.size, 0);
});

test("old image remains visible through slow decoding; only decoded matching evidence is committed", async () => {
  const h = fixture(); await h.start(); const original = h.image(0), held = deferred();
  h.value = h.snapshot(2); h.value.cameras[0].image_identity.sequence++;
  h.value.cameras[0].channels[0].business_status = "NG";
  h.updateUrls();
  h.decode = () => held.promise;
  await h.tick();
  assert.equal(h.image(0), original); assert.equal(original.hidden, false);
  assert.equal(h.channel(0).dataset.kind, "ok"); assert.equal(h.urls.size, 7);
  assert.equal(h.observation(0).hidden, true);
  held.resolve(); await flush();
  assert.notEqual(h.image(0), original); assert.equal(h.channel(0).dataset.kind, "ng");
  assert.equal(h.urls.size, 6); assert.ok(!h.urls.has(original.src)); h.stop();
});

for (const failure of ["410", "decode", "dimensions", "body"]) {
  test(`replacement ${failure} preserves compatible pixels without attaching new judgments`, async () => {
    const h = fixture(); await h.start(); const original = h.image(0);
    h.value = h.snapshot(2); h.value.cameras[0].image_identity.sequence++;
    h.value.cameras[0].channels[0].business_status = "NG";
    h.updateUrls();
    if (failure === "410") h.handler = path => h.response(path.includes("CAM6") ? 410 : 200);
    if (failure === "decode") h.decode = () => Promise.reject(new Error("bad JPEG"));
    if (failure === "dimensions") h.decode = image => { image.naturalWidth = 639; return Promise.resolve(); };
    if (failure === "body") h.handler = () => ({ ...h.response(), blob: async () => { throw new Error("body interrupted"); } });
    await h.tick();
    assert.equal(h.image(0), original); assert.equal(original.hidden, false);
    if (failure === "410") { assert.equal(h.channel(0).dataset.kind, "ok"); assert.equal(h.observation(0).hidden, true); }
    else h.neutral(0, "unavailable");
    assert.equal(h.urls.size, 6); assert.equal(h.jpegCalls().length, 7);
    h.value.revision++;
    h.handler = () => h.response(); h.decode = () => Promise.resolve();
    await h.tick();
    assert.notEqual(h.image(0), original); assert.equal(h.channel(0).dataset.kind, "ng");
    assert.equal(h.maxActive, 1); assert.equal(h.urls.size, 6); h.stop();
  });
}

test("network loss and stale observations keep pixels explicitly stale, then recover matching results", async () => {
  const h = fixture(); await h.start(); const image = h.image(0), url = image.src;
  h.handler = () => { throw new Error("offline"); }; await h.tick();
  assert.equal(h.image(0), image); assert.equal(image.src, url); assert.equal(image.hidden, false);
  h.neutral(0, "unavailable"); assert.equal(h.observation(0).attributes["aria-label"], "Stale observation");
  assert.equal(h.channel(0).attributes["aria-label"], "CH31 · Unavailable");
  h.handler = () => h.response(); h.value = h.snapshot(2);
  h.value.published_at = new Date(h.now - 10000).toISOString(); await h.tick();
  assert.equal(h.connection(), "Stale observation"); assert.equal(h.image(0).src, url); h.neutral(0, "unavailable");
  h.value = h.snapshot(3); await h.tick();
  assert.equal(h.channel(0).dataset.kind, "ok"); assert.equal(h.jpegCalls().length, 6); h.stop();
});

for (const boundary of ["publisher", "generation", "shape", "reference", "source_epoch"]) {
  test(`incompatible ${boundary} clears old pixels before waiting on a replacement`, async () => {
    const h = fixture();
    if (boundary === "reference") for (const row of h.value.cameras) { row.image_identity.generation = 0; row.image_status = "reference"; row.result_status = "unavailable"; }
    h.updateUrls();
    await h.start(); const original = h.image(0), originalUrl = original.src, held = deferred();
    h.value = h.snapshot(2); h.value.cameras[0].image_identity.sequence++;
    if (boundary === "publisher") {
      h.value.publisher_id = "replacement";
      for (const row of h.value.cameras) row.image_url = row.image_url.replace("process-one", "replacement");
    }
    if (boundary === "generation") h.value.cameras[0].image_identity.generation++;
    if (boundary === "shape") h.value.cameras[0].image_identity.shape[0]++;
    if (boundary === "source_epoch") h.value.cameras[0].image_identity.image_epoch++;
    if (boundary === "reference") { h.value.cameras[0].image_identity.generation = 0; h.value.cameras[0].image_status = "reference"; }
    h.updateUrls();
    h.handler = path => path.includes("CAM6") ? held.promise : h.response();
    await h.tick();
    assert.equal(original.hidden, true); assert.equal(original.src, undefined);
    assert.ok(!h.urls.has(originalUrl)); h.neutral(0, "unavailable");
    h.stop(); held.resolve(h.response()); await flush();
    assert.equal(h.urls.size, 0); assert.equal(h.image(0).hidden, true);
  });
}

test("invalid frame identity clears its image; invalid channel evidence never becomes a successful check", async () => {
  const h = fixture(); await h.start();
  h.value = h.snapshot(2); h.value.cameras[0].image_identity.camera_id = "CAM1";
  await h.tick(); assert.equal(h.image(0).hidden, true); h.neutral(0, "unavailable");
  h.value = h.snapshot(3); h.value.cameras[0].channels[1].channel_id = h.value.cameras[0].channels[0].channel_id;
  await h.tick(); assert.equal(h.image(0).hidden, true); h.neutral(0, "unavailable");
  h.value = h.snapshot(4); h.value.cameras[0].image_url = h.value.cameras[1].image_url;
  await h.tick(); assert.equal(h.image(0).hidden, true); h.neutral(0, "unavailable");
  h.value = h.snapshot(5); h.value.configured_camera_ids.push("CAM6");
  await h.tick(); assert.equal(h.connection(), "Disconnected");
  assert.equal(h.urls.size, 0); h.stop();
});

test("stable image URLs ignore metadata revisions but reject another frame key from the same camera", async()=>{
  const h=fixture();await h.start();const image=h.image(0),url=h.value.cameras[0].image_url;
  h.value=h.snapshot(100);assert.equal(h.value.cameras[0].image_url,url);await h.tick();
  assert.equal(h.image(0),image);assert.equal(h.jpegCalls().length,6);
  h.value=h.snapshot(101);h.value.cameras[0].image_identity.sequence++;
  // An old image URL with newer metadata must never relabel its pixels.
  await h.tick();assert.equal(h.image(0).hidden,true);h.neutral(0,"unavailable");
  h.value=h.snapshot(102);h.value.cameras[0].image_identity.sequence++;h.updateUrls();await h.tick();
  assert.equal(h.channel(0).dataset.kind,"ok");h.stop();assert.equal(h.urls.size,0);
});

test("superseded JPEG batches rotate serial opportunity without moving cards or retrying", async()=>{
  const h=fixture();await h.start();const roots=[...h.node("cameras").children];
  h.value=h.snapshot(2);for(const row of h.value.cameras)row.image_identity.sequence++;h.updateUrls();
  h.handler=path=>h.response(path.includes(".jpg?")?410:200);await h.tick();
  assert.match(h.jpegCalls()[6],/CAM6\.jpg/);assert.equal(h.jpegCalls().length,12);
  h.value.revision++;h.value.published_at=new Date(h.now).toISOString();await h.tick();
  assert.match(h.jpegCalls()[12],/CAM5\.jpg/);assert.equal(h.jpegCalls().length,18);
  roots.forEach((node,index)=>assert.equal(h.card(index),node));
  assert.equal(h.maxActive,1);assert.equal(h.timers.size,1);assert.equal(h.urls.size,6);
  h.handler=()=>h.response();h.value.revision++;h.value.published_at=new Date(h.now).toISOString();await h.tick();
  assert.equal(h.channel(0).dataset.kind,"ok");assert.equal(h.observation(0).hidden,true);h.stop();
});

test("five/six camera removal and reordering preserve configured identities, nodes and bounded ownership", async () => {
  const h = fixture(); await h.start(); const roots = [...h.node("cameras").children], removed = h.image(0).src;
  h.value = h.snapshot(2); h.value.configured_camera_ids = h.value.configured_camera_ids.slice(1);
  h.value.cameras = h.value.cameras.slice(1); await h.tick();
  assert.deepEqual(h.node("cameras").children, roots.slice(1)); assert.equal(h.urls.size, 5); assert.ok(!h.urls.has(removed));
  assert.equal(h.channel(0).title, "CH25"); assert.equal(h.indicators(4).length, 6);
  h.value.configured_camera_ids.reverse(); await h.tick();
  assert.deepEqual(h.node("cameras").children, roots.slice(1).reverse()); assert.equal(h.jpegCalls().length, 6);
  h.value = h.snapshot(3); await h.tick();
  assert.equal(h.node("cameras").children.length, 6); assert.equal(h.urls.size, 6);
  assert.equal(h.jpegCalls().length, 7); h.stop();
});

for (const count of [5, 6]) {
  test(`${count} camera contain-area layout reflows without remounts or downloads`, async () => {
    const h = fixture(); h.value.configured_camera_ids = h.value.configured_camera_ids.slice(-count); h.value.cameras = h.value.cameras.slice(-count);
    await h.start(); const roots = [...h.node("cameras").children], urls = roots.map((_, index) => h.image(index).src);
    for (const [w, height, columns] of [[1920, 1080, 3], [1080, 1920, 2], [1366, 768, 3], [390, 844, 2], [1080, 1080, 2], [5000, 900, count], [1920, 1080, 3]]) {
      h.resize(w, height);
      assert.equal(h.node("cameras").style.gridTemplateColumns, `repeat(${columns}, minmax(0, 1fr))`, `${w}x${height}`);
      h.node("cameras").children.forEach((root, index) => assert.equal(root, roots[index]));
      assert.deepEqual(roots.map((_, index) => h.image(index).src), urls);
    }
    assert.equal(h.node("cameras").remounts, 1); assert.equal(h.jpegCalls().length, count); h.stop();
  });
}

test("image aspect ratios, not device breakpoints, select the useful-area grid", async () => {
  const h = fixture();
  for (const row of h.value.cameras) { row.image_identity.shape = [1600, 640, 3]; row.jpeg_dimensions = { width: 256, height: 640 }; }
  h.updateUrls();
  h.decode = image => { image.naturalWidth = 256; image.naturalHeight = 640; return Promise.resolve(); };
  await h.start(); h.resize(1080, 1920);
  assert.equal(h.node("cameras").style.gridTemplateColumns, "repeat(3, minmax(0, 1fr))"); h.stop();
});

test("timeout of replacement decoding retains old pixels, fences late work and permits bfcache recovery", async () => {
  const h = fixture(); await h.start(); const original = h.image(0), held = deferred();
  h.value = h.snapshot(2); h.value.cameras[0].image_identity.sequence++;
  h.updateUrls();
  h.decode = () => held.promise; await h.tick(); await h.tick(5000);
  assert.equal(h.image(0), original); assert.equal(original.hidden, false); assert.equal(h.urls.size, 6);
  h.neutral(0, "unavailable"); assert.equal(h.connection(), "Disconnected");
  h.stop(); assert.equal(h.urls.size, 0);
  h.value.revision++; h.value.published_at = new Date(h.now).toISOString();
  h.decode = () => Promise.resolve(); h.events.pageshow(); held.resolve(); await flush();
  assert.equal(h.urls.size, 6); assert.equal(h.channel(0).dataset.kind, "ok");
  assert.equal(h.maxActive, 1); h.stop(); assert.equal(h.timers.size, 0);
});

for (const count of [5, 6]) {
  test(`${count} cameras: identical captions with advancing frames have no indicator or observation DOM writes`, async () => {
    const h = fixture(); h.value.refresh_interval_ms = 500;
    h.value.configured_camera_ids = h.value.configured_camera_ids.slice(-count); h.value.cameras = h.value.cameras.slice(-count);
    await h.start();
    const labels = Array.from({length:count},(_,i)=>h.indicators(i).slice());
    const icons = labels.map(row=>row.map(label=>label.children[0]));
    const writes = labels.map(row=>row.map(label=>label.writes));
    const markers = Array.from({length:count},(_,i)=>h.observation(i).writes);
    for (let revision=2;revision<=10;revision++) {
      h.value.revision=revision; h.value.published_at=new Date(h.now).toISOString();
      for (const row of h.value.cameras) row.image_identity.sequence++;
      h.updateUrls();
      await h.tick();
      for(let camera=0;camera<count;camera++) {
        assert.equal(h.observation(camera).hidden,true); assert.equal(h.observation(camera).writes,markers[camera]);
        h.indicators(camera).forEach((label,slot)=>{assert.equal(label,labels[camera][slot]);assert.equal(label.children[0],icons[camera][slot]);assert.equal(label.writes,writes[camera][slot]);});
      }
    }
    assert.equal(h.maxUrls,count+1); h.stop(); assert.equal(h.urls.size,0);
  });
}

test("unchanged complete observations renew freshness without any repeated DOM writes", async () => {
  const h=fixture(); await h.start(); const writes=h.presentationWrites(0), globals=["connection","connection-text","fault","signals","empty"].map(id=>h.node(id).writes);
  for(let revision=2;revision<=7;revision++) {h.value=h.snapshot(revision);await h.tick();}
  assert.deepEqual(h.presentationWrites(0),writes);
  assert.deepEqual(["connection","connection-text","fault","signals","empty"].map(id=>h.node(id).writes),globals);
  assert.equal(h.channel(0).dataset.kind,"ok");assert.equal(h.observation(0).hidden,true);assert.equal(h.jpegCalls().length,6);h.stop();
});

test("new incomplete results retain the previous NG/ERROR bundle, then commit matching pixels and all six judgments", async () => {
  const h=fixture();h.value.cameras[0].channels[0]={channel_id:31,business_status:"NG",technical_status:"ERROR"};await h.start();
  const original=h.image(0), labels=h.indicators(0).map(n=>n.attributes["aria-label"]),writes=h.presentationWrites(0);
  for(let revision=2;revision<=3;revision++) {
    h.value=h.snapshot(revision);h.value.cameras[0].image_identity.sequence=8;h.value.cameras[0].result_status="pending";
    h.updateUrls();
    await h.tick();assert.equal(h.image(0),original);assert.deepEqual(h.indicators(0).map(n=>n.attributes["aria-label"]),labels);assert.deepEqual(h.presentationWrites(0),writes);
    assert.equal(h.jpegCalls().length,6);assert.equal(h.observation(0).hidden,true);
  }
  h.value=h.snapshot(4);h.value.cameras[0].image_identity.sequence=8;h.value.cameras[0].channels[0]={channel_id:31,business_status:"EMPTY",technical_status:"SUCCESS"};
  h.updateUrls();
  const held=deferred();h.decode=()=>held.promise;await h.tick();
  assert.equal(h.image(0),original);assert.equal(h.channel(0).dataset.kind,"ng");assert.match(h.channel(0).className,/error/);
  held.resolve();await flush();assert.notEqual(h.image(0),original);assert.equal(h.channel(0).dataset.kind,"empty");assert.doesNotMatch(h.channel(0).className,/error/);
  assert.equal(h.jpegCalls().length,7);assert.equal(h.urls.size,6);h.stop();
});

for(const missing of ["pending","410"]) {
  test(`fresh metadata with prolonged ${missing} cannot renew old complete-result freshness`, async()=>{
    const h=fixture();await h.start();const original=h.image(0);
    for(let revision=2;revision<=5;revision++) {
      h.value=h.snapshot(revision);h.value.cameras[0].image_identity.sequence=8;
      h.updateUrls();
      if(missing==="pending")h.value.cameras[0].result_status="pending";
      else h.handler=path=>h.response(path.includes("CAM6")?410:200);
      await h.tick();
      assert.equal(h.image(0),original);
      if(revision<5){assert.equal(h.channel(0).dataset.kind,"ok");assert.equal(h.observation(0).hidden,true);}
    }
    h.neutral(0,"unavailable");assert.equal(h.observation(0).attributes["aria-label"],"Stale observation");assert.equal(h.channel(1).dataset.kind,"ok");
    assert.equal(h.timers.size,1);h.value=h.snapshot(6);h.value.cameras[0].image_identity.sequence=8;h.updateUrls();h.handler=()=>h.response();await h.tick();
    assert.notEqual(h.image(0),original);assert.equal(h.channel(0).dataset.kind,"ok");assert.equal(h.observation(0).hidden,true);h.stop();
  });
}

test("authoritative invalidation and disconnect do not resurrect a quiet old result during pending recovery", async()=>{
  const h=fixture();await h.start();const image=h.image(0);
  h.value=h.snapshot(2);h.value.generation=2;h.value.application_state="FAULT";
  await h.tick();h.neutral(0,"unavailable");assert.equal(h.image(0),image);assert.equal(h.node("fault").hidden,false);
  h.value=h.snapshot(3);h.value.cameras[0].result_status="unavailable";await h.tick();h.neutral(0,"unavailable");
  h.value=h.snapshot(4);await h.tick();assert.equal(h.channel(0).dataset.kind,"ok");
  h.handler=()=>{throw new Error("offline")};await h.tick();h.neutral(0,"unavailable");
  h.value=h.snapshot(5);h.value.cameras[0].result_status="pending";h.handler=()=>h.response();await h.tick();
  h.neutral(0,"unavailable");assert.equal(h.image(0),image);assert.equal(h.observation(0).attributes["aria-label"],"Stale observation");h.stop();
});
