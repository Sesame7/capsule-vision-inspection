/* One snapshot/JPEG request at a time per observer; no history or control API. */
(() => {
  "use strict";
  const zh = navigator.language.toLowerCase().startsWith("zh");
  const words = {
    connected: ["Observation connected", "观察已连接"], lost: ["Disconnected", "连接中断"],
    stale: ["Stale observation", "观察已过期"], unavailable: ["Unavailable", "暂无观察"],
    waiting: ["Waiting", "等待中"], missing: ["Image unavailable", "暂无图像"],
    empty: ["No configured cameras", "暂无已配置相机"],
    retained: ["Retained image", "保留图像"], fault: ["Application fault", "应用故障"],
    last: ["last observed", "最后观察"],
    slot: ["Slot", "槽位"], technical: ["Technical", "技术状态"],
  };
  const t = key => words[key][zh ? 1 : 0];
  const element = id => document.getElementById(id);
  document.documentElement.lang = zh ? "zh" : "en";
  const symbols = {
    ok: "M5 12l4 4L19 6", ng: "M6 6l12 12M18 6L6 18", empty: "M6 6h12v12H6z",
    unknown: "M12 3L22 21H2zM12 9v5M12 17v.1",
    unavailable: "M6 12h12",
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
    if (node.getAttribute("role") !== "img") node.setAttribute("role", "img");
    if (node.getAttribute("aria-label") !== description) node.setAttribute("aria-label", description);
  }
  const cards = new Map();
  let latest = null, stopped = false, busy = false, epoch = 0, next = null, controller = null;
  let cameraOrder = [], imageStart = 0;
  let refreshIntervalMs = 500;
  const current = token => !stopped && epoch === token;
  const expired = timestamp => !Number.isFinite(timestamp) || Date.now() - timestamp > 6000;
  const aged = snapshot => expired(Date.parse(snapshot.published_at));

  function connection(key) {
    const display = key === "connected" && ![...cards.values()].some(card => card.loaded) ? "unavailable" : key;
    const caption = display === "connected" ? "" : t(display), label = element("connection");
    if (element("connection-text").textContent !== caption) element("connection-text").textContent = caption;
    if (label.title) label.removeAttribute("title");
    if (label.getAttribute("aria-label") !== t(display)) label.setAttribute("aria-label", t(display));
    const tone = display === "connected" ? "normal" : "warning";
    if (label.dataset.tone !== tone) label.dataset.tone = tone;
    const fault = element("fault");
    const hidden = latest?.application_state !== "FAULT";
    if (fault.hidden !== hidden) fault.hidden = hidden;
    const detail = `${t("fault")}${["lost", "stale", "waiting"].includes(key) ? ` · ${t("last")}` : ""}`;
    if (fault.textContent !== detail) fault.textContent = detail;
    const quiet = !caption && fault.hidden;
    if (element("signals").hidden !== quiet) element("signals").hidden = quiet;
  }
  function indicators(card, channels, neutral = null) {
    channels = Array.isArray(channels) ? channels : [];
    const identified = channels.length === 6 &&
      new Set(channels.map(channel => channel?.channel_id)).size === 6 &&
      channels.every(channel => Number.isSafeInteger(channel?.channel_id) && channel.channel_id > 0);
    for (let slot = 0; slot < 6; slot++) {
      const channel = channels[slot];
      const business = channel?.business_status;
      const kind = neutral ?? (["OK", "NG", "EMPTY", "UNKNOWN"].includes(business) ? business.toLowerCase() : "unavailable");
      const error = !neutral && channel?.technical_status === "ERROR";
      const label = card.channels.children[slot];
      if (identified && label.dataset.channelId !== String(channel.channel_id)) label.dataset.channelId = String(channel.channel_id);
      const className = `channel ${kind}${error ? " error" : ""}`;
      if (label.className !== className) label.className = className;
      if (label.dataset.kind !== kind) { label.replaceChildren(icon(kind)); label.dataset.kind = kind; }
      const identity = label.dataset.channelId ? `CH${label.dataset.channelId.padStart(2, "0")}` : `${card.id} · ${t("slot")} ${slot + 1}`;
      described(label, `${identity} · ${neutral || !channel ? t(neutral ?? "unavailable") : `${business} · ${t("technical")} ${channel.technical_status}`}`);
      if (label.title !== identity) label.title = identity;
    }
  }
  function observation(card, kind) {
    const node = card.observation, hidden = !["retained", "stale"].includes(kind);
    if (node.title) node.removeAttribute("title");
    if (node.hidden !== hidden) node.hidden = hidden;
    if (hidden) return;
    if (node.className !== `observation ${kind}`) {
      node.className = `observation ${kind}`;
      described(node, t(kind));
      node.replaceChildren(icon(kind));
    }
  }
  function neutralize(card, kind = "stale", channels = []) {
    card.matchedAt = null;
    indicators(card, channels, "unavailable");
    observation(card, kind);
  }
  function clearImage(card) {
    if (card.url) URL.revokeObjectURL(card.url);
    card.url = null;
    if (card.loaded) card.image.removeAttribute("src");
    card.loaded = card.scope = null;
    for (const label of card.channels.children) delete label.dataset.channelId;
    if (!card.image.hidden) card.image.hidden = true;
    if (card.placeholder.hidden) card.placeholder.hidden = false;
    neutralize(card, "connected");
  }
  function makeCard(id) {
    const root = document.createElement("section");
    root.className = "camera";
    const title = document.createElement("h2");
    title.textContent = id;
    const heading = document.createElement("div");
    heading.className = "camera-heading";
    const observation = document.createElement("span");
    observation.className = "observation";
    observation.hidden = true;
    heading.append(title, observation);
    const picture = document.createElement("div");
    picture.className = "picture";
    const image = document.createElement("img");
    image.alt = id;
    image.hidden = true;
    const empty = document.createElement("span");
    empty.className = "placeholder";
    described(empty, t("missing"));
    empty.append(icon("missing"));
    picture.append(image, empty);
    const channels = document.createElement("div");
    channels.className = "channels";
    for (let slot = 0; slot < 6; slot++) channels.append(document.createElement("span"));
    root.append(heading, picture, channels);
    const card = { id, root, picture, image, placeholder: empty, observation, channels, url: null, loaded: null, scope: null, matchedAt: null, aspect: 1 };
    indicators(card, [], "unavailable");
    return card;
  }
  function signature(snapshot, row) {
    const id = row.image_identity;
    const dimensions = row.jpeg_dimensions;
    return id && id.camera_id === row.camera_id &&
      Number.isSafeInteger(id.image_epoch) && id.image_epoch > 0 &&
      [id.generation, id.cycle_id, id.sequence, id.frame_revision].every(value => Number.isSafeInteger(value) && value >= 0) &&
      Array.isArray(id.shape) && id.shape.length === 3 && id.shape[2] === 3 &&
      id.shape.slice(0, 2).every(value => Number.isSafeInteger(value) && value > 0) &&
      dimensions && [dimensions.width, dimensions.height].every(value => Number.isSafeInteger(value) && value > 0 && value <= 640) &&
      ["current", "reference", "retained", "stale"].includes(row.image_status) &&
      row.image_url === `/api/cameras/${encodeURIComponent(row.camera_id)}.jpg?publisher=${encodeURIComponent(snapshot.publisher_id)}&image=${[id.image_epoch, id.generation, id.cycle_id, id.sequence, id.frame_revision, ...id.shape.slice(0, 2)].join("-")}`
      ? JSON.stringify([snapshot.publisher_id, id.image_epoch, id.generation, id.cycle_id, id.camera_id, id.sequence, id.frame_revision, id.shape]) : null;
  }
  function arrange() {
    const wall = element("cameras"), count = cameraOrder.length;
    if (!count || stopped) return;
    const width = wall.clientWidth, height = wall.clientHeight;
    let best = -1, columns = 1;
    // At most six uniform grids: score the actual contain-fitted image area.
    for (let candidate = 1; candidate <= count; candidate++) {
      const rows = Math.ceil(count / candidate);
      const w = (width - (candidate - 1) * 4) / candidate;
      const h = (height - (rows - 1) * 4) / rows - 22;
      if (candidate > 1 && w < 132) continue;
      const area = cameraOrder.reduce((sum, id) => {
        const ratio = cards.get(id).aspect;
        return sum + Math.min(w, Math.max(0, h) * ratio) * Math.min(Math.max(0, h), w / ratio);
      }, 0);
      if (area > best) { best = area; columns = candidate; }
    }
    const rows = Math.ceil(count / columns);
    wall.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
    wall.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
    wall.style.minHeight = `${rows * 96 + (rows - 1) * 4}px`;
  }
  function matching(row) {
    const channels = Array.isArray(row.channels) ? row.channels : [];
    return row.image_status !== "reference" && row.result_status === "matched" && channels.length === 6 && new Set(channels.map(channel => channel?.channel_id)).size === 6 &&
      channels.every(channel => Number.isSafeInteger(channel?.channel_id) && channel.channel_id > 0 &&
        ["OK", "NG", "EMPTY", "UNKNOWN"].includes(channel.business_status) && ["SUCCESS", "ERROR", "DISABLED"].includes(channel.technical_status));
  }
  function status(card, row, key, snapshot) {
    const stale = aged(snapshot) || row.result_status === "stale" || row.image_status === "stale" ||
      row.image_identity && row.image_identity.generation !== 0 && row.image_identity.generation !== snapshot.generation;
    const matched = matching(row);
    if (!stale && key && card.loaded === key && matched) {
      card.matchedAt = Date.parse(snapshot.published_at);
      indicators(card, row.channels);
      observation(card, row.image_status === "retained" ? "retained" : "connected");
      return;
    }
    const waiting = !stale && (row.result_status === "pending" || matched);
    // Retain one complete observation, not newer judgments on older pixels.
    // Only a matching observation renews its existing six-second freshness.
    if (waiting && card.matchedAt !== null && !expired(card.matchedAt)) {
      if (row.image_status === "retained") observation(card, "retained");
      return;
    }
    neutralize(card, stale || waiting && card.loaded || row.result_status === "matched" && !matched ? "stale" :
      ["reference", "retained"].includes(row.image_status) ? "retained" : "connected", row.channels);
  }
  function markLost(key) {
    connection(key);
    for (const card of cards.values()) {
      neutralize(card);
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
      return decoded;
    } finally {
      signal.removeEventListener("abort", cancel);
    }
  }
  async function show(snapshot, token, signal) {
    if (!current(token)) return;
    if (typeof snapshot.publisher_id !== "string" || !snapshot.publisher_id || !Number.isSafeInteger(snapshot.revision) || snapshot.revision < 1 ||
        !Number.isSafeInteger(snapshot.generation) || snapshot.generation < 0 ||
        !Array.isArray(snapshot.configured_camera_ids) || snapshot.configured_camera_ids.length > 6 ||
        !snapshot.configured_camera_ids.every(id => typeof id === "string" && id.length) ||
        new Set(snapshot.configured_camera_ids).size !== snapshot.configured_camera_ids.length || !Array.isArray(snapshot.cameras) ||
        snapshot.cameras.some(row => !row || typeof row.camera_id !== "string") ||
        new Set(snapshot.cameras.map(row => row.camera_id)).size !== snapshot.cameras.length) {
      for (const card of cards.values()) clearImage(card);
      throw new Error("Invalid snapshot metadata");
    }
    if (latest && latest.publisher_id === snapshot.publisher_id && snapshot.revision < latest.revision) {
      if (aged(latest)) markLost("stale");
      return;
    }
    if (Number.isFinite(snapshot.refresh_interval_ms) && snapshot.refresh_interval_ms > 0) {
      refreshIntervalMs = snapshot.refresh_interval_ms;
    }
    latest = snapshot;
    const order = snapshot.configured_camera_ids;
    for (const [id, card] of cards) {
      if (!order.includes(id)) { clearImage(card); cards.delete(id); }
    }
    for (const id of order) if (!cards.has(id)) cards.set(id, makeCard(id));
    let changed = order.length !== cameraOrder.length || order.some((id, index) => id !== cameraOrder[index]);
    if (changed) {
      element("cameras").replaceChildren(...order.map(id => cards.get(id).root));
      cameraOrder = [...order];
      imageStart = 0;
    }
    if (element("empty").hidden !== (order.length !== 0)) element("empty").hidden = order.length !== 0;
    if (element("empty").textContent !== t("empty")) element("empty").textContent = t("empty");
    connection(aged(snapshot) ? "stale" : "connected");
    const pending = [];
    // Prepare replacements silently while compatible complete observations stay.
    for (const id of order) {
      if (!current(token)) return;
      const card = cards.get(id);
      const row = snapshot.cameras.find(row => row.camera_id === id) ?? { image_status: "unavailable", result_status: "unavailable", channels: [] };
      const key = signature(snapshot, row);
      if (!key || !row.image_url) {
        clearImage(card);
        status(card, row, key, snapshot);
        continue;
      }
      if (card.loaded === key) { status(card, row, key, snapshot); continue; }
      const identity = row.image_identity;
      const scope = JSON.stringify([snapshot.publisher_id, identity.image_epoch, identity.generation, id, identity.shape]);
      // Generation-zero references can come from different setup sources;
      // they have no immutable Production session and must not bridge changes.
      if (card.scope !== scope || identity.generation === 0 && card.loaded) {
        clearImage(card);
        card.scope = scope;
      }
      const aspect = row.jpeg_dimensions.width / row.jpeg_dimensions.height;
      if (card.aspect !== aspect) { card.aspect = aspect; changed = true; }
      status(card, row, key, snapshot);
      if (!matching(row) && !["reference", "retained"].includes(row.image_status)) continue;
      pending.push({ card, row, key, scope });
    }
    if (changed) arrange();
    const start = imageStart % Math.max(1, pending.length);
    let superseded = false;
    // A superseded batch gives the next camera first chance on the same serial
    // queue, without changing card order, retries, or request ownership.
    for (const { card, row, key, scope } of [...pending.slice(start), ...pending.slice(0, start)]) {
      if (!current(token)) return;
      const response = await fetch(row.image_url, { signal, cache: "no-store", credentials: "omit" });
      if (!current(token)) return;
      if (!response.ok) {
        if (response.status === 410) { superseded = true; status(card, row, key, snapshot); }
        else neutralize(card);
        continue; // One attempt per poll; 410 waits for fresh metadata.
      }
      const blob = await response.blob();
      if (!current(token)) return;
      const url = URL.createObjectURL(blob);
      let retained = false;
      try {
        const decoded = await decodeImage(url, signal);
        if (!current(token)) return;
        if (decoded.naturalWidth !== row.jpeg_dimensions.width || decoded.naturalHeight !== row.jpeg_dimensions.height) throw new Error("Invalid JPEG dimensions");
        if (aged(snapshot)) { status(card, row, key, snapshot); continue; }
        const previous = card.url;
        decoded.alt = card.id;
        card.picture.replaceChild(decoded, card.image);
        card.image = decoded;
        card.url = url;
        card.loaded = key;
        card.scope = scope;
        if (!card.placeholder.hidden) card.placeholder.hidden = true;
        retained = true;
        if (previous) URL.revokeObjectURL(previous);
        status(card, row, key, snapshot);
      } catch {
        if (current(token)) {
          neutralize(card);
        }
      } finally {
        if (!retained) URL.revokeObjectURL(url);
      }
    }
    if (current(token)) {
      if (superseded) imageStart = (start + 1) % Math.max(1, pending.length);
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
  window.addEventListener("resize", arrange);
  poll();
})();
