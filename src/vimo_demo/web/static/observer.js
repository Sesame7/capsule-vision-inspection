/* One snapshot/JPEG request at a time per observer; no history or control API. */
(() => {
  "use strict";
  const zh = navigator.language.toLowerCase().startsWith("zh");
  const words = {
    connected: ["Observation connected", "观察已连接"], lost: ["Disconnected", "连接中断"],
    stale: ["Stale observation", "观察已过期"], unavailable: ["Unavailable", "暂无观察"],
    waiting: ["Waiting", "等待中"], missing: ["Image unavailable", "暂无图像"],
    empty: ["No configured cameras", "暂无已配置相机"], pending: ["Pending", "等待中"],
    retained: ["Retained image", "保留图像"], fault: ["Application fault", "应用故障"],
    last: ["last observed", "最后观察"], application: ["Application", "应用"],
    slot: ["Slot", "槽位"], technical: ["Technical", "技术状态"],
  };
  const t = key => words[key][zh ? 1 : 0];
  const element = id => document.getElementById(id);
  document.documentElement.lang = zh ? "zh" : "en";
  const symbols = {
    ok: "M5 12l4 4L19 6", ng: "M6 6l12 12M18 6L6 18", empty: "M6 6h12v12H6z",
    unknown: "M12 3L22 21H2zM12 9v5M12 17v.1",
    pending: "M12 4a8 8 0 1 0 0 16a8 8 0 1 0 0-16", unavailable: "M6 12h12",
    retained: "M7 5v14M17 5v14", stale: "M12 4a8 8 0 1 0 0 16a8 8 0 1 0 0-16M12 8v4l3 2",
    missing: "M4 5h16v14H4zM4 15l5-5 5 5 3-3 3 3M8 8h.1",
  };
  function icon(kind) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [name, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(name, value);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", symbols[kind]);
    svg.append(path);
    return svg;
  }
  function described(node, description) {
    node.title = description;
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", description);
  }
  const cards = new Map();
  let latest = null, stopped = false, busy = false, epoch = 0, next = null, controller = null;
  let refreshIntervalMs = 500;
  const current = token => !stopped && epoch === token;
  const aged = snapshot => !Number.isFinite(Date.parse(snapshot.published_at)) || Date.now() - Date.parse(snapshot.published_at) > 6000;

  function connection(key) {
    const display = key === "connected" && ![...cards.values()].some(card => card.loaded) ? "unavailable" : key;
    element("connection-text").textContent = display === "connected" ? "" : t(display);
    const description = `${t(display)}${latest ? ` · ${t("application")} ${latest.application_state} (${t("last")})` : ""}`;
    element("connection").title = description;
    element("connection").setAttribute("aria-label", description);
    element("connection").dataset.tone = display === "connected" ? "normal" : "warning";
    const fault = element("fault");
    fault.hidden = latest?.application_state !== "FAULT";
    fault.textContent = `${t("fault")}${["lost", "stale", "waiting"].includes(key) ? ` · ${t("last")}` : ""}`;
    fault.title = `${t("application")} FAULT (${t("last")})`;
  }
  function indicators(card, channels, neutral = null) {
    const labels = [];
    for (let slot = 0; slot < 6; slot++) {
      const channel = channels[slot];
      const business = channel?.business_status;
      const kind = neutral ?? (["OK", "NG", "EMPTY", "UNKNOWN"].includes(business) ? business.toLowerCase() : "unavailable");
      const error = !neutral && channel?.technical_status === "ERROR";
      const label = document.createElement("span");
      label.className = `channel ${kind}${error ? " error" : ""}`;
      label.dataset.kind = kind;
      const identity = channel ? `CH${String(channel.channel_id).padStart(2, "0")}` : `${card.id} · ${t("slot")} ${slot + 1}`;
      described(label, `${identity} · ${neutral || !channel ? t(neutral ?? "unavailable") : `${business} · ${t("technical")} ${channel.technical_status}`}`);
      label.append(icon(kind));
      labels.push(label);
    }
    card.channels.replaceChildren(...labels);
  }
  function observation(card, kind) {
    card.observation.className = `observation ${kind}`;
    card.observation.hidden = !["retained", "stale"].includes(kind);
    described(card.observation, t(kind));
    card.observation.replaceChildren(icon(kind === "retained" ? "retained" : "stale"));
  }
  function placeholder(card, kind) {
    described(card.placeholder, t(kind));
    card.placeholder.replaceChildren(icon(kind === "pending" ? "pending" : "missing"));
  }
  function clearImage(card) {
    if (card.url) URL.revokeObjectURL(card.url);
    card.url = null;
    card.loaded = null;
    card.image.removeAttribute("src");
    card.image.hidden = true;
    card.placeholder.hidden = false;
    indicators(card, [], "unavailable");
  }
  function makeCard(id) {
    const root = document.createElement("section");
    root.className = "camera";
    const title = document.createElement("h2");
    title.textContent = id;
    const heading = document.createElement("div");
    heading.className = "camera-heading";
    const observation = document.createElement("span");
    observation.hidden = true;
    heading.append(title, observation);
    const picture = document.createElement("div");
    picture.className = "picture";
    const image = document.createElement("img");
    image.alt = id;
    image.hidden = true;
    const empty = document.createElement("span");
    empty.className = "placeholder";
    picture.append(image, empty);
    const channels = document.createElement("div");
    channels.className = "channels";
    root.append(heading, picture, channels);
    const card = { id, root, image, placeholder: empty, observation, channels, url: null, loaded: null };
    placeholder(card, "missing");
    indicators(card, [], "unavailable");
    return card;
  }
  function signature(snapshot, row) {
    const id = row.image_identity;
    return id ? JSON.stringify([snapshot.publisher_id, id.generation, id.cycle_id, id.camera_id, id.sequence, id.frame_revision, id.shape]) : null;
  }
  function status(card, row, key, stale) {
    observation(card, stale || row.result_status === "stale" || row.image_status === "stale" ? "stale" : row.image_status === "retained" ? "retained" : "connected");
    const matched = !!key && !stale && card.loaded === key && row.result_status === "matched";
    indicators(card, row.channels, matched ? null : !stale && row.result_status === "pending" ? "pending" : "unavailable");
  }
  function markLost(key) {
    connection(key);
    for (const card of cards.values()) {
      observation(card, "stale");
      indicators(card, [], "unavailable");
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
    if (Number.isFinite(snapshot.refresh_interval_ms) && snapshot.refresh_interval_ms > 0) {
      refreshIntervalMs = snapshot.refresh_interval_ms;
    }
    latest = snapshot;
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
      if (!key || !row.image_url) {
        clearImage(card);
        placeholder(card, "missing");
        status(card, row, key, aged(snapshot));
        continue;
      }
      if (card.loaded === key) { status(card, row, key, aged(snapshot)); continue; }
      clearImage(card); // Never present new evidence on a previous frame's pixels.
      placeholder(card, "pending");
      observation(card, "connected");
      indicators(card, row.channels, "pending");
      pending.push({ card, row, key });
    }
    for (const { card, row, key } of pending) {
      if (!current(token)) return;
      const response = await fetch(row.image_url, { signal, cache: "no-store", credentials: "omit" });
      if (!current(token)) return;
      if (!response.ok) {
        placeholder(card, response.status === 410 ? "pending" : "missing");
        indicators(card, row.channels, response.status === 410 ? "pending" : "unavailable");
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
        if (current(token)) {
          placeholder(card, "missing");
          indicators(card, row.channels, "unavailable");
        }
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
      if (!stopped) next = setTimeout(poll, Math.max(0, refreshIntervalMs - (Date.now() - started)));
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
