/* One snapshot/JPEG request at a time per observer; no history or control API. */
(() => {
  "use strict";
  const zh = navigator.language.toLowerCase().startsWith("zh");
  const words = {
    connected: ["Connected", "已连接"], lost: ["Connection lost · last observation", "连接中断 · 最后观察"],
    stale: ["Publication stale · last observation", "发布已过期 · 最后观察"],
    waiting: ["Waiting for publication", "等待发布"], missing: ["No inspection image", "暂无检测图像"],
    expired: ["Publication changed · waiting for fresh snapshot", "发布已更新 · 等待新快照"],
    empty: ["No configured cameras", "暂无已配置相机"], pending: ["Pending image / evidence", "等待图像 / 结果"],
    scope: ["Read-only · Best-effort observations · Cameras need not share one cycle", "只读 · 尽力观察 · 多路图像不一定属于同一周期"],
    state: ["Application (last observed)", "应用（最后观察）"], cycle: ["Latest completed cycle", "最近完成周期"],
    time: ["Publication time", "发布时间"],
  };
  const t = key => words[key][zh ? 1 : 0];
  const element = id => document.getElementById(id);
  document.documentElement.lang = zh ? "zh" : "en";
  for (const [id, key] of [["scope", "scope"], ["state-label", "state"], ["cycle-label", "cycle"], ["time-label", "time"]]) {
    element(id).textContent = t(key);
  }
  const cards = new Map();
  let latest = null, stopped = false, busy = false, epoch = 0, next = null, controller = null;
  const current = token => !stopped && epoch === token;
  const aged = snapshot => !Number.isFinite(Date.parse(snapshot.published_at)) || Date.now() - Date.parse(snapshot.published_at) > 6000;

  function connection(key) {
    element("connection").textContent = t(key);
    element("connection").dataset.tone = key === "connected" ? "normal" : "warning";
  }
  function clearImage(card) {
    if (card.url) URL.revokeObjectURL(card.url);
    card.url = null;
    card.loaded = null;
    card.image.removeAttribute("src");
    card.image.hidden = true;
    card.placeholder.hidden = false;
    card.channels.replaceChildren();
  }
  function makeCard(id) {
    const root = document.createElement("section");
    root.className = "camera";
    const title = document.createElement("h2");
    title.textContent = id;
    const picture = document.createElement("div");
    picture.className = "picture";
    const image = document.createElement("img");
    image.alt = id;
    image.hidden = true;
    const placeholder = document.createElement("p");
    placeholder.className = "placeholder";
    placeholder.textContent = t("missing");
    picture.append(image, placeholder);
    const identity = document.createElement("p");
    identity.className = "identity";
    const observation = document.createElement("p");
    observation.className = "observation";
    const channels = document.createElement("div");
    channels.className = "channels";
    root.append(title, picture, identity, observation, channels);
    return { root, image, placeholder, identity, observation, channels, url: null, loaded: null };
  }
  function signature(snapshot, row) {
    const id = row.image_identity;
    return id ? JSON.stringify([snapshot.publisher_id, id.generation, id.cycle_id, id.camera_id, id.sequence, id.frame_revision, id.shape]) : null;
  }
  function status(card, row, key, stale) {
    card.observation.textContent = stale ? t("stale") : `${row.image_status} · ${row.result_status}`;
    card.channels.replaceChildren();
    if (!stale && card.loaded === key && row.result_status === "matched") {
      for (const channel of row.channels) {
        const label = document.createElement("span");
        const business = channel.business_status;
        label.className = `channel ${["OK", "NG", "EMPTY", "UNKNOWN"].includes(business) ? business.toLowerCase() : "unknown"}`;
        label.textContent = `CH${String(channel.channel_id).padStart(2, "0")} ${business}${channel.technical_status === "SUCCESS" ? "" : " · " + channel.technical_status}`;
        card.channels.append(label);
      }
    }
  }
  function markLost(key) {
    connection(key);
    for (const card of cards.values()) {
      card.observation.textContent = t(key);
      card.channels.replaceChildren();
    }
  }
  async function decodeImage(url, signal) {
    const decoded = new Image();
    let cancel;
    const aborted = new Promise((_, reject) => {
      cancel = () => { decoded.src = ""; reject(new Error("Aborted")); };
    });
    signal.addEventListener("abort", cancel, { once: true });
    try {
      decoded.src = url;
      if (signal.aborted) cancel();
      await Promise.race([decoded.decode(), aborted]);
    } finally {
      signal.removeEventListener("abort", cancel);
    }
  }
  async function show(snapshot, token, signal) {
    if (!current(token)) return;
    if (typeof snapshot.publisher_id !== "string" || !Number.isSafeInteger(snapshot.revision) ||
        !Array.isArray(snapshot.configured_camera_ids) || snapshot.configured_camera_ids.length > 6 ||
        new Set(snapshot.configured_camera_ids).size !== snapshot.configured_camera_ids.length || !Array.isArray(snapshot.cameras)) {
      throw new Error("Invalid snapshot metadata");
    }
    if (latest && latest.publisher_id === snapshot.publisher_id && snapshot.revision < latest.revision) return;
    latest = snapshot;
    element("state").textContent = snapshot.application_state;
    element("cycle").textContent = `G${snapshot.generation} · ${snapshot.latest_cycle_id ?? "—"}`;
    const date = new Date(snapshot.published_at);
    element("time").textContent = Number.isFinite(date.getTime()) ? date.toLocaleString() : "—";
    element("time").title = snapshot.published_at;
    const order = snapshot.configured_camera_ids;
    for (const [id, card] of cards) {
      if (!order.includes(id)) { clearImage(card); cards.delete(id); }
    }
    for (const id of order) if (!cards.has(id)) cards.set(id, makeCard(id));
    element("cameras").replaceChildren(...order.map(id => cards.get(id).root));
    element("empty").hidden = order.length !== 0;
    element("empty").textContent = t("empty");
    connection(aged(snapshot) ? "stale" : "connected");
    const pending = [];
    // Invalidate every changed/hidden frame before any network await.
    for (const id of order) {
      if (!current(token)) return;
      const card = cards.get(id);
      const row = snapshot.cameras.find(row => row.camera_id === id) ?? { image_status: "unavailable", result_status: "unavailable", channels: [] };
      const key = signature(snapshot, row);
      const identity = row.image_identity;
      card.identity.textContent = identity ? `G${identity.generation} · C${identity.cycle_id} · F${identity.sequence} · ${identity.shape[1]}×${identity.shape[0]}` : "—";
      if (!key || !row.image_url) {
        clearImage(card);
        card.placeholder.textContent = t("missing");
        status(card, row, key, aged(snapshot));
        continue;
      }
      if (card.loaded === key) { status(card, row, key, aged(snapshot)); continue; }
      clearImage(card); // Never present new evidence on a previous frame's pixels.
      card.placeholder.textContent = t("pending");
      card.observation.textContent = t("pending");
      pending.push({ card, row, key });
    }
    for (const { card, row, key } of pending) {
      if (!current(token)) return;
      const response = await fetch(row.image_url, { signal, cache: "no-store", credentials: "omit" });
      if (!current(token)) return;
      if (!response.ok) {
        card.placeholder.textContent = t(response.status === 410 ? "expired" : "missing");
        card.observation.textContent = t("pending");
        continue; // One attempt per poll; 410 waits for fresh metadata.
      }
      const blob = await response.blob();
      if (!current(token)) return;
      const url = URL.createObjectURL(blob);
      let retained = false;
      try {
        await decodeImage(url, signal);
        if (!current(token)) return;
        card.url = url;
        card.loaded = key;
        card.image.src = url;
        card.image.hidden = false;
        card.placeholder.hidden = true;
        retained = true;
        status(card, row, key, aged(snapshot));
      } catch {
        if (current(token)) card.placeholder.textContent = t("missing");
      } finally {
        if (!retained) URL.revokeObjectURL(url);
      }
    }
    if (current(token)) {
      if (aged(snapshot)) markLost("stale");
      else connection("connected");
    }
  }
  async function poll() {
    if (stopped || busy) return;
    busy = true;
    const started = Date.now(), token = ++epoch;
    const active = controller = new AbortController();
    const deadline = setTimeout(() => {
      if (current(token)) { ++epoch; markLost("lost"); active.abort(); }
    }, 5000);
    try {
      const response = await fetch("/api/snapshot", { signal: active.signal, cache: "no-store", credentials: "omit" });
      if (!current(token)) return;
      if (response.status === 503) markLost("waiting");
      else if (!response.ok) throw new Error(`Snapshot HTTP ${response.status}`);
      else await show(await response.json(), token, active.signal);
    } catch {
      if (current(token)) markLost("lost");
    } finally {
      clearTimeout(deadline);
      if (controller === active) controller = null;
      busy = false;
      if (!stopped) next = setTimeout(poll, Math.max(0, 2000 - (Date.now() - started)));
    }
  }
  window.addEventListener("pagehide", () => {
    stopped = true;
    ++epoch;
    clearTimeout(next);
    if (controller) controller.abort();
    for (const card of cards.values()) clearImage(card);
  });
  window.addEventListener("pageshow", () => { if (stopped) { stopped = false; poll(); } });
  poll();
})();
