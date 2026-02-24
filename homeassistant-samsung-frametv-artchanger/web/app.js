const state = {
  filter: "all",
  tvs: [],
  gallery: [],
  selectedTvIps: [],
  busyAssets: new Set(),
  deleteAsset: null,
  retryContext: null,
  settings: {
    tv_ips: [],
    refresh_interval_seconds: 30,
    snapshot_ttl_seconds: 20,
  },
  settingsDraftTvIps: [],
  timer: null,
};

const basePath = window.location.pathname.replace(/\/+$/, "");
const withBasePath = (path) => `${basePath}${path}`;

const els = {
  filterSelect: document.getElementById("filterSelect"),
  refreshBtn: document.getElementById("refreshBtn"),
  openUploadBtn: document.getElementById("openUploadBtn"),
  openSettingsBtn: document.getElementById("openSettingsBtn"),
  tvPanel: document.getElementById("tvPanel"),
  gallery: document.getElementById("gallery"),
  cardTemplate: document.getElementById("cardTemplate"),
  statusIndicator: document.getElementById("statusIndicator"),
  statusLabel: document.getElementById("statusLabel"),
  uploadDialog: document.getElementById("uploadDialog"),
  uploadForm: document.getElementById("uploadForm"),
  fileInput: document.getElementById("fileInput"),
  cropWrap: document.getElementById("cropWrap"),
  cropImage: document.getElementById("cropImage"),
  zoomInput: document.getElementById("zoomInput"),
  zoomValue: document.getElementById("zoomValue"),
  rotationInput: document.getElementById("rotationInput"),
  rotationValue: document.getElementById("rotationValue"),
  rotate90Btn: document.getElementById("rotate90Btn"),
  flipHorizontalBtn: document.getElementById("flipHorizontalBtn"),
  cancelUploadBtn: document.getElementById("cancelUploadBtn"),
  confirmUploadBtn: document.getElementById("confirmUploadBtn"),
  confirmUploadActivateBtn: document.getElementById("confirmUploadActivateBtn"),
  uploadTvList: document.getElementById("uploadTvList"),
  settingsDialog: document.getElementById("settingsDialog"),
  settingsForm: document.getElementById("settingsForm"),
  newTvIpInput: document.getElementById("newTvIpInput"),
  addTvIpBtn: document.getElementById("addTvIpBtn"),
  settingsTvList: document.getElementById("settingsTvList"),
  refreshIntervalInput: document.getElementById("refreshIntervalInput"),
  snapshotTtlInput: document.getElementById("snapshotTtlInput"),
  discoverSubnetInput: document.getElementById("discoverSubnetInput"),
  discoverBtn: document.getElementById("discoverBtn"),
  discoverResults: document.getElementById("discoverResults"),
  cancelSettingsBtn: document.getElementById("cancelSettingsBtn"),
  saveSettingsBtn: document.getElementById("saveSettingsBtn"),
  deleteDialog: document.getElementById("deleteDialog"),
  deleteForm: document.getElementById("deleteForm"),
  deleteMessage: document.getElementById("deleteMessage"),
  cancelDeleteBtn: document.getElementById("cancelDeleteBtn"),
  confirmDeleteBtn: document.getElementById("confirmDeleteBtn"),
  resultDialog: document.getElementById("resultDialog"),
  resultTitle: document.getElementById("resultTitle"),
  resultMessage: document.getElementById("resultMessage"),
  resultList: document.getElementById("resultList"),
  retryFailedBtn: document.getElementById("retryFailedBtn"),
  toastStack: document.getElementById("toastStack"),
};

const cropState = {
  sourceObjectUrl: null,
  originalImage: null,
  loaded: false,
  naturalW: 0,
  naturalH: 0,
  safeW: 0,
  safeH: 0,
  baseScale: 1,
  minZoom: 1,
  zoom: 1,
  rotation: 0,
  quarterTurns: 0,
  flipHorizontal: false,
  offsetX: 0,
  offsetY: 0,
  dragging: false,
  startX: 0,
  startY: 0,
  renderVersion: 0,
};

const thumbQueueState = {
  generation: 0,
  queue: [],
  active: 0,
  concurrency: 1,
};

function selectedTvIps() {
  return [...document.querySelectorAll(".tv-target:checked")].map((node) => node.value);
}

function selectedUploadTvIps() {
  return [...document.querySelectorAll(".upload-tv-target:checked")].map((node) => node.value);
}

function normalizeIp(value) {
  return String(value || "").trim();
}

function isLikelyIp(value) {
  const parts = value.split(".");
  if (parts.length !== 4) {
    return false;
  }
  return parts.every((part) => {
    if (!/^\d+$/.test(part)) {
      return false;
    }
    const number = Number(part);
    return number >= 0 && number <= 255;
  });
}

function createBadge(label, extra = "") {
  const span = document.createElement("span");
  span.className = `badge ${extra}`.trim();
  span.textContent = label;
  return span;
}

function setBusy(on) {
  els.refreshBtn.disabled = on;
}

function setCardBusy(assetId, busy) {
  if (busy) {
    state.busyAssets.add(assetId);
  } else {
    state.busyAssets.delete(assetId);
  }

  const card = els.gallery.querySelector(`.card[data-asset-id="${assetId}"]`);
  if (!card) {
    return;
  }

  card.classList.toggle("busy", busy);
  const progress = card.querySelector(".card-progress");
  if (progress) {
    progress.hidden = !busy;
  }

  card.querySelectorAll("button").forEach((button) => {
    button.disabled = busy;
  });
}

function toast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  els.toastStack.appendChild(node);

  setTimeout(() => {
    node.remove();
  }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(withBasePath(path), {
    headers: {
      ...(options.headers || {}),
    },
    ...options,
  });

  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const data = isJson ? await response.json() : null;

  if (!response.ok) {
    const detail = data?.detail || `${response.status} ${response.statusText}`;
    const error = new Error(detail);
    error.code = data?.error?.code || "UNKNOWN";
    error.retryable = Boolean(data?.error?.retryable);
    error.requestId = data?.error?.request_id || response.headers.get("X-Request-ID") || "";
    throw error;
  }

  return data ?? response;
}

function readData(payload, fallbackKey) {
  if (payload?.data) {
    if (fallbackKey && payload.data[fallbackKey] !== undefined) {
      return payload.data[fallbackKey];
    }
    return payload.data;
  }
  if (fallbackKey) {
    return payload?.[fallbackKey];
  }
  return payload;
}

function updateStatusBar(meta) {
  if (!meta) {
    els.statusIndicator.className = "status-indicator status-unknown";
    els.statusLabel.textContent = "Status onbekend";
    els.statusIndicator.title = "Nog geen status beschikbaar.";
    return;
  }

  const lastRefresh = meta.last_refresh ? new Date(meta.last_refresh).toLocaleString() : "nog niet";
  const isRefreshing = Boolean(meta.refresh_in_progress);
  const isStale = Boolean(meta.stale);

  if (isRefreshing) {
    els.statusIndicator.className = "status-indicator status-refreshing";
    els.statusLabel.textContent = "Refresh bezig";
  } else if (isStale) {
    els.statusIndicator.className = "status-indicator status-stale";
    els.statusLabel.textContent = "Data mogelijk oud";
  } else {
    els.statusIndicator.className = "status-indicator status-fresh";
    els.statusLabel.textContent = "Data up-to-date";
  }

  els.statusIndicator.title = `Synchronisatiestatus\nRefresh actief: ${isRefreshing ? "ja" : "nee"}\nData status: ${isStale ? "stale" : "fresh"}\nLaatste refresh: ${lastRefresh}`;
}

function renderTvPanel() {
  els.tvPanel.innerHTML = "";

  if (!state.tvs.length) {
    els.tvPanel.textContent = "Geen TV-configuratie gevonden.";
    return;
  }

  const selected = new Set(state.selectedTvIps.length ? state.selectedTvIps : state.tvs.map((tv) => tv.ip));

  state.tvs.forEach((tv) => {
    const chip = document.createElement("label");
    chip.className = `tv-chip ${tv.online ? "online" : "offline"}`;

    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "tv-target";
    box.value = tv.ip;
    box.checked = selected.has(tv.ip);

    const text = document.createElement("span");
    const status = tv.online ? "online" : "offline";
    text.textContent = `${tv.ip} (${status})`;

    chip.append(box, text);
    els.tvPanel.appendChild(chip);
  });
}

function renderGallery() {
  els.gallery.innerHTML = "";
  resetThumbnailQueue();

  if (!state.gallery.length) {
    const empty = document.createElement("div");
    empty.textContent = "Geen beelden gevonden voor dit filter.";
    empty.className = "card";
    empty.style.padding = "18px";
    els.gallery.appendChild(empty);
    return;
  }

  state.gallery.forEach((item) => {
    const fragment = els.cardTemplate.content.cloneNode(true);
    const root = fragment.querySelector(".card");
    const thumb = fragment.querySelector(".thumb");
    const flag = fragment.querySelector(".sync-flag");
    const name = fragment.querySelector(".name");
    const badges = fragment.querySelector(".badges");

    root.dataset.assetId = item.asset_id;
    thumb.removeAttribute("src");
    thumb.loading = "lazy";
    const thumbUrl = withBasePath(`/api/thumb/${encodeURIComponent(item.asset_id)}?v=${encodeURIComponent(item.updated_at || "")}`);

    name.textContent = item.filename;
    if (item.synced) {
      flag.hidden = false;
    }

    if (item.on_ha) {
      badges.appendChild(createBadge("HA"));
    }
    if (item.on_tv) {
      badges.appendChild(createBadge("TV"));
    }
    if (item.synced) {
      badges.appendChild(createBadge("SYNC", "synced"));
    }
    if (item.active) {
      badges.appendChild(createBadge("ACTIVE", "active"));
    }

    fragment.querySelector(".activate-btn").addEventListener("click", async () => {
      await activateItem(item, selectedTvIps());
    });

    fragment.querySelector(".delete-btn").addEventListener("click", () => {
      openDeleteDialog(item);
    });

    els.gallery.appendChild(fragment);
    queueThumbnailLoad(thumb, thumbUrl);
    if (state.busyAssets.has(item.asset_id)) {
      setCardBusy(item.asset_id, true);
    }
  });
}

function resetThumbnailQueue() {
  thumbQueueState.generation += 1;
  thumbQueueState.queue = [];
  thumbQueueState.active = 0;
}

function queueThumbnailLoad(imageEl, url) {
  thumbQueueState.queue.push({
    imageEl,
    url,
    generation: thumbQueueState.generation,
  });
  processThumbnailQueue();
}

function processThumbnailQueue() {
  while (thumbQueueState.active < thumbQueueState.concurrency && thumbQueueState.queue.length) {
    const job = thumbQueueState.queue.shift();
    if (!job || job.generation !== thumbQueueState.generation || !job.imageEl.isConnected) {
      continue;
    }

    thumbQueueState.active += 1;
    loadThumbnailJob(job)
      .catch(() => {})
      .finally(() => {
        thumbQueueState.active = Math.max(0, thumbQueueState.active - 1);
        processThumbnailQueue();
      });
  }
}

function loadThumbnailJob(job) {
  const { imageEl, url, generation } = job;
  return new Promise((resolve) => {
    const timeoutMs = 20000;
    let timeoutHandle = null;

    const done = () => {
      if (timeoutHandle) {
        clearTimeout(timeoutHandle);
        timeoutHandle = null;
      }
      imageEl.onload = null;
      imageEl.onerror = null;
      resolve();
    };

    imageEl.onload = () => {
      if (generation !== thumbQueueState.generation) {
        done();
        return;
      }
      done();
    };

    imageEl.onerror = () => {
      if (generation !== thumbQueueState.generation) {
        done();
        return;
      }
      imageEl.removeAttribute("src");
      done();
    };

    timeoutHandle = setTimeout(() => {
      if (generation !== thumbQueueState.generation) {
        done();
        return;
      }
      imageEl.removeAttribute("src");
      done();
    }, timeoutMs);

    imageEl.src = url;
  });
}

function buildResultEntries(result, actionType) {
  if (actionType === "activate") {
    return Object.entries(result.results || {}).map(([tvIp, detail]) => ({
      tvIp,
      ok: Boolean(detail.ok),
      code: detail.code,
      message: detail.error || (detail.activated ? "Activated" : "Uploaded"),
      retryable: Boolean(detail.retryable),
    }));
  }

  if (actionType === "delete") {
    return Object.entries(result.tv || {}).map(([tvIp, detail]) => ({
      tvIp,
      ok: Boolean(detail.ok),
      code: detail.code,
      message: detail.error || (detail.deleted ? "Deleted" : detail.reason || "No-op"),
      retryable: Boolean(detail.retryable),
    }));
  }

  return [];
}

function openResultDialog(title, message, entries, retryContext = null) {
  els.resultTitle.textContent = title;
  els.resultMessage.textContent = message;
  els.resultList.innerHTML = "";

  entries.forEach((entry) => {
    const li = document.createElement("li");
    li.className = entry.ok ? "ok" : "fail";
    li.textContent = `${entry.tvIp}: ${entry.ok ? "OK" : "FAIL"} - ${entry.message}${entry.code ? ` (${entry.code})` : ""}`;
    els.resultList.appendChild(li);
  });

  const retryableFailures = entries.filter((entry) => !entry.ok && entry.retryable).map((entry) => entry.tvIp);
  if (retryContext && retryableFailures.length) {
    state.retryContext = { ...retryContext, tvIps: retryableFailures };
    els.retryFailedBtn.hidden = false;
  } else {
    state.retryContext = null;
    els.retryFailedBtn.hidden = true;
  }

  els.resultDialog.showModal();
}

async function activateItem(item, tvIpsOverride) {
  const tvIps = tvIpsOverride?.length ? tvIpsOverride : selectedTvIps();
  setCardBusy(item.asset_id, true);

  try {
    const result = await api(`/api/items/${encodeURIComponent(item.asset_id)}/activate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tv_ips: tvIps,
        ensure_upload: true,
        activate: true,
      }),
    });

    const compat = result.results ? result : readData(result);
    const entries = buildResultEntries(compat, "activate");
    const hasFailure = entries.some((entry) => !entry.ok);

    if (hasFailure) {
      openResultDialog(
        "Activeren gedeeltelijk mislukt",
        `Niet alle TV-acties slaagden voor ${item.filename}.`,
        entries,
        { action: "activate", assetId: item.asset_id },
      );
    } else {
      toast(`Geactiveerd: ${item.filename}`, "success");
    }

    await reloadData();
  } catch (error) {
    toast(`Activeren mislukt: ${error.message} [${error.code || "UNKNOWN"}]`, "error");
  } finally {
    setCardBusy(item.asset_id, false);
  }
}

function openDeleteDialog(item) {
  state.deleteAsset = item;
  els.deleteMessage.textContent = `Verwijder '${item.filename}'. Geselecteerde TV targets: ${selectedTvIps().join(", ") || "alle"}.`;
  els.deleteDialog.showModal();
}

async function runDelete(item, target, tvIps) {
  setCardBusy(item.asset_id, true);

  try {
    const result = await api(`/api/items/${encodeURIComponent(item.asset_id)}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        targets: target,
        tv_ips: tvIps,
      }),
    });

    const compat = result.tv ? result : readData(result);
    const entries = buildResultEntries(compat, "delete");
    const hasFailure = entries.some((entry) => !entry.ok);

    if (hasFailure) {
      openResultDialog(
        "Verwijderen gedeeltelijk mislukt",
        `Niet alle delete-acties slaagden voor ${item.filename}.`,
        entries,
        { action: "delete", assetId: item.asset_id, target },
      );
    } else {
      toast(`Verwijderd: ${item.filename}`, "success");
    }

    await reloadData();
  } catch (error) {
    toast(`Verwijderen mislukt: ${error.message} [${error.code || "UNKNOWN"}]`, "error");
  } finally {
    setCardBusy(item.asset_id, false);
  }
}

async function retryFailed() {
  const ctx = state.retryContext;
  if (!ctx) {
    return;
  }

  const item = state.gallery.find((entry) => entry.asset_id === ctx.assetId);
  if (!item) {
    toast("Item niet gevonden voor retry.", "error");
    return;
  }

  els.retryFailedBtn.disabled = true;
  try {
    if (ctx.action === "activate") {
      await activateItem(item, ctx.tvIps);
    } else if (ctx.action === "delete") {
      await runDelete(item, ctx.target, ctx.tvIps);
    }
    els.resultDialog.close();
  } finally {
    els.retryFailedBtn.disabled = false;
  }
}

async function loadSettings() {
  const payload = await api("/api/settings");
  const data = readData(payload);

  state.settings = {
    tv_ips: [...(data.tv_ips || [])],
    refresh_interval_seconds: Number(data.refresh_interval_seconds || 30),
    snapshot_ttl_seconds: Number(data.snapshot_ttl_seconds || 20),
  };
  state.settingsDraftTvIps = [...state.settings.tv_ips];
  renderUploadTvList();
}

function renderUploadTvList() {
  els.uploadTvList.innerHTML = "";

  const configuredIps = [...(state.settings.tv_ips || [])];
  if (!configuredIps.length) {
    const empty = document.createElement("span");
    empty.className = "section-label upload-tv-list-empty";
    empty.textContent = "Geen TV's geconfigureerd.";
    els.uploadTvList.appendChild(empty);
    return;
  }

  const previousSelection = new Set(selectedUploadTvIps());
  const hasPreviousSelection = previousSelection.size > 0;
  const tvStatus = new Map((state.tvs || []).map((tv) => [tv.ip, Boolean(tv.online)]));

  configuredIps.forEach((ip) => {
    const chip = document.createElement("label");
    const online = tvStatus.get(ip);
    chip.className = `tv-chip ${online === true ? "online" : online === false ? "offline" : ""}`.trim();

    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "upload-tv-target";
    box.value = ip;
    box.checked = hasPreviousSelection ? previousSelection.has(ip) : true;

    const text = document.createElement("span");
    text.textContent = online === true ? `${ip} (online)` : online === false ? `${ip} (offline)` : ip;

    chip.append(box, text);
    els.uploadTvList.appendChild(chip);
  });
}

function renderSettingsTvList() {
  els.settingsTvList.innerHTML = "";

  if (!state.settingsDraftTvIps.length) {
    const empty = document.createElement("span");
    empty.textContent = "Nog geen TV's toegevoegd.";
    empty.className = "section-label";
    els.settingsTvList.appendChild(empty);
    return;
  }

  state.settingsDraftTvIps.forEach((ip) => {
    const item = document.createElement("span");
    item.className = "settings-tv-item";

    const text = document.createElement("span");
    text.textContent = ip;

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.textContent = "x";
    removeBtn.title = `Verwijder ${ip}`;
    removeBtn.addEventListener("click", () => {
      state.settingsDraftTvIps = state.settingsDraftTvIps.filter((value) => value !== ip);
      renderSettingsTvList();
    });

    item.append(text, removeBtn);
    els.settingsTvList.appendChild(item);
  });
}

function openSettingsDialog() {
  state.settingsDraftTvIps = [...state.settings.tv_ips];
  els.refreshIntervalInput.value = String(state.settings.refresh_interval_seconds);
  els.snapshotTtlInput.value = String(state.settings.snapshot_ttl_seconds);
  els.newTvIpInput.value = "";
  els.discoverSubnetInput.value = "";
  els.discoverResults.innerHTML = "";
  renderSettingsTvList();
  els.settingsDialog.showModal();
}

function addTvIpFromInput() {
  const ip = normalizeIp(els.newTvIpInput.value);
  if (!ip) {
    return;
  }

  if (!isLikelyIp(ip)) {
    toast("Ongeldig IP-adres formaat.", "error");
    return;
  }

  if (!state.settingsDraftTvIps.includes(ip)) {
    state.settingsDraftTvIps.push(ip);
    renderSettingsTvList();
  }

  els.newTvIpInput.value = "";
}

function renderDiscoverResults(foundItems) {
  els.discoverResults.innerHTML = "";

  if (!foundItems.length) {
    const li = document.createElement("li");
    li.textContent = "Geen ondersteunde TV's gevonden.";
    els.discoverResults.appendChild(li);
    return;
  }

  foundItems.forEach((item) => {
    const li = document.createElement("li");
    li.className = "ok";

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "btn tiny secondary";
    addBtn.textContent = "Toevoegen";
    addBtn.addEventListener("click", () => {
      if (!state.settingsDraftTvIps.includes(item.ip)) {
        state.settingsDraftTvIps.push(item.ip);
        renderSettingsTvList();
      }
    });

    const text = document.createElement("span");
    text.textContent = `${item.ip} (supported)`;

    li.append(text, addBtn);
    els.discoverResults.appendChild(li);
  });
}

async function discoverTvs() {
  const subnet = normalizeIp(els.discoverSubnetInput.value);

  els.discoverBtn.disabled = true;
  els.discoverResults.innerHTML = "";

  try {
    const payload = await api("/api/settings/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subnet: subnet || null }),
    });

    const data = readData(payload);
    const found = data.found || [];
    renderDiscoverResults(found);
    toast(`Scan klaar: ${found.length} ondersteunde TV(s) gevonden.`, "success");
  } catch (error) {
    toast(`Scan mislukt: ${error.message} [${error.code || "UNKNOWN"}]`, "error");
  } finally {
    els.discoverBtn.disabled = false;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  if (event.submitter?.id !== "saveSettingsBtn") {
    return;
  }

  const refreshInterval = Number(els.refreshIntervalInput.value || "30");
  const snapshotTtl = Number(els.snapshotTtlInput.value || "20");

  if (state.settingsDraftTvIps.length === 0) {
    toast("Voeg minimaal 1 TV IP toe.", "error");
    return;
  }

  els.saveSettingsBtn.disabled = true;
  try {
    const payload = await api("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tv_ips: state.settingsDraftTvIps,
        refresh_interval_seconds: refreshInterval,
        snapshot_ttl_seconds: snapshotTtl,
      }),
    });

    const data = readData(payload);
    state.settings = {
      tv_ips: [...(data.tv_ips || [])],
      refresh_interval_seconds: Number(data.refresh_interval_seconds || 30),
      snapshot_ttl_seconds: Number(data.snapshot_ttl_seconds || 20),
    };

    state.selectedTvIps = [...state.settings.tv_ips];
    renderUploadTvList();
    els.settingsDialog.close();
    toast("Instellingen opgeslagen.", "success");
    await reloadData();
  } catch (error) {
    toast(`Opslaan mislukt: ${error.message} [${error.code || "UNKNOWN"}]`, "error");
  } finally {
    els.saveSettingsBtn.disabled = false;
  }
}

async function reloadData() {
  setBusy(true);
  try {
    const previousSelection = selectedTvIps();
    if (previousSelection.length) {
      state.selectedTvIps = previousSelection;
    }

    const tvPayload = await api("/api/tvs");
    const tvData = readData(tvPayload, "tvs") || [];
    state.tvs = tvData;
    if (!state.selectedTvIps.length) {
      state.selectedTvIps = state.tvs.map((tv) => tv.ip);
    }
    renderTvPanel();
    renderUploadTvList();

    const tvSelection = selectedTvIps();
    const tvQuery = tvSelection.length === 1 ? `&tv_ip=${encodeURIComponent(tvSelection[0])}` : "";
    const galleryPayload = await api(`/api/gallery?filter=${encodeURIComponent(state.filter)}${tvQuery}`);

    const galleryData = readData(galleryPayload, "items") || [];
    state.gallery = galleryData;
    renderGallery();
    updateStatusBar(galleryPayload.meta || tvPayload.meta);
  } finally {
    setBusy(false);
  }
}

function resetCropState(resetFile = false) {
  cropState.loaded = false;
  cropState.naturalW = 0;
  cropState.naturalH = 0;
  cropState.safeW = 0;
  cropState.safeH = 0;
  cropState.baseScale = 1;
  cropState.minZoom = 1;
  cropState.zoom = 1;
  cropState.rotation = 0;
  cropState.quarterTurns = 0;
  cropState.flipHorizontal = false;
  cropState.offsetX = 0;
  cropState.offsetY = 0;
  cropState.dragging = false;
  cropState.renderVersion += 1;

  els.zoomInput.min = "1";
  els.zoomInput.max = "4";
  els.zoomInput.value = "1";
  if (els.zoomValue) {
    els.zoomValue.textContent = "1.00x";
  }
  els.rotationInput.value = "0";
  updateRotationLabel();
  updateFlipButtonUi();

  if (cropState.sourceObjectUrl) {
    URL.revokeObjectURL(cropState.sourceObjectUrl);
    cropState.sourceObjectUrl = null;
  }

  cropState.originalImage = null;

  if (resetFile) {
    els.fileInput.value = "";
  }

  els.cropImage.removeAttribute("src");
  els.cropImage.style.display = "none";
}

function getTotalRotationDegrees() {
  return cropState.rotation + cropState.quarterTurns * 90;
}

function normalizeDisplayAngle(angle) {
  let normalized = angle % 360;
  if (normalized > 180) {
    normalized -= 360;
  } else if (normalized <= -180) {
    normalized += 360;
  }
  return normalized;
}

function updateRotationLabel() {
  if (!els.rotationValue) {
    return;
  }
  const display = normalizeDisplayAngle(getTotalRotationDegrees());
  els.rotationValue.textContent = `${display}\u00B0`;
}

function updateFlipButtonUi() {
  if (!els.flipHorizontalBtn) {
    return;
  }
  els.flipHorizontalBtn.classList.toggle("toggle-active", cropState.flipHorizontal);
  els.flipHorizontalBtn.setAttribute("aria-pressed", cropState.flipHorizontal ? "true" : "false");
  els.flipHorizontalBtn.title = cropState.flipHorizontal ? "Spiegeling aan" : "Spiegel horizontaal";
}

function largestInscribedRect(width, height, angleRad) {
  const absCos = Math.abs(Math.cos(angleRad));
  const absSin = Math.abs(Math.sin(angleRad));
  const widthIsLonger = width >= height;
  const sideLong = widthIsLonger ? width : height;
  const sideShort = widthIsLonger ? height : width;
  const sin2 = 2 * absSin * absCos;

  let rectW;
  let rectH;

  if (sideShort <= sin2 * sideLong || Math.abs(absSin - absCos) < 1e-6) {
    const half = 0.5 * sideShort;
    if (widthIsLonger) {
      rectW = half / Math.max(absCos, 1e-6);
      rectH = half / Math.max(absSin, 1e-6);
    } else {
      rectW = half / Math.max(absSin, 1e-6);
      rectH = half / Math.max(absCos, 1e-6);
    }
  } else {
    const cos2 = absCos * absCos - absSin * absSin;
    rectW = (width * absCos - height * absSin) / Math.max(cos2, 1e-6);
    rectH = (height * absCos - width * absSin) / Math.max(cos2, 1e-6);
  }

  if (!Number.isFinite(rectW) || !Number.isFinite(rectH) || rectW <= 0 || rectH <= 0) {
    return { width: width, height: height };
  }

  return {
    width: Math.max(1, Math.min(width, rectW)),
    height: Math.max(1, Math.min(height, rectH)),
  };
}

function updateZoomUi() {
  const minZoom = Math.max(1, cropState.minZoom);
  const dynamicMax = Math.max(4, Math.ceil((minZoom + 0.6) * 100) / 100);
  const maxZoom = Math.max(minZoom, dynamicMax);

  els.zoomInput.min = minZoom.toFixed(2);
  els.zoomInput.max = maxZoom.toFixed(2);
  cropState.zoom = Math.max(minZoom, Math.min(maxZoom, cropState.zoom));
  els.zoomInput.value = cropState.zoom.toFixed(2);

  if (els.zoomValue) {
    els.zoomValue.textContent = `${cropState.zoom.toFixed(2)}x`;
  }
}

function getGeometry() {
  const wrapRect = els.cropWrap.getBoundingClientRect();
  const wrapW = wrapRect.width;
  const wrapH = wrapRect.height;

  if (!cropState.naturalW || !cropState.naturalH || !cropState.originalImage) {
    cropState.baseScale = 1;
    cropState.minZoom = 1;
    cropState.safeW = cropState.naturalW;
    cropState.safeH = cropState.naturalH;
    updateZoomUi();
    return {
      wrapW,
      wrapH,
      scale: cropState.zoom,
      drawW: cropState.naturalW,
      drawH: cropState.naturalH,
      maxX: 0,
      maxY: 0,
    };
  }

  cropState.baseScale = Math.max(wrapW / cropState.naturalW, wrapH / cropState.naturalH);
  const angleRad = (Math.abs(getTotalRotationDegrees()) * Math.PI) / 180;
  const sourceW = cropState.originalImage.naturalWidth;
  const sourceH = cropState.originalImage.naturalHeight;
  const safeRect = largestInscribedRect(sourceW, sourceH, angleRad);
  cropState.safeW = safeRect.width;
  cropState.safeH = safeRect.height;

  const requiredScale = Math.max(wrapW / Math.max(cropState.safeW, 1), wrapH / Math.max(cropState.safeH, 1));
  cropState.minZoom = Math.max(1, requiredScale / Math.max(cropState.baseScale, 1e-6));
  updateZoomUi();

  const scale = cropState.baseScale * cropState.zoom;
  const drawW = cropState.naturalW * scale;
  const drawH = cropState.naturalH * scale;
  const safeDrawW = cropState.safeW * scale;
  const safeDrawH = cropState.safeH * scale;
  const maxX = Math.max(0, (safeDrawW - wrapW) / 2);
  const maxY = Math.max(0, (safeDrawH - wrapH) / 2);

  return {
    wrapW,
    wrapH,
    scale,
    drawW,
    drawH,
    maxX,
    maxY,
  };
}

function clampOffsets() {
  if (!cropState.loaded) {
    return;
  }

  const geometry = getGeometry();
  cropState.offsetX = Math.max(-geometry.maxX, Math.min(geometry.maxX, cropState.offsetX));
  cropState.offsetY = Math.max(-geometry.maxY, Math.min(geometry.maxY, cropState.offsetY));
}

function updateCropTransform() {
  if (!cropState.loaded) {
    return;
  }

  clampOffsets();
  const geometry = getGeometry();

  const left = (geometry.wrapW - geometry.drawW) / 2 + cropState.offsetX;
  const top = (geometry.wrapH - geometry.drawH) / 2 + cropState.offsetY;

  els.cropImage.style.width = `${geometry.drawW}px`;
  els.cropImage.style.height = `${geometry.drawH}px`;
  els.cropImage.style.left = `${left}px`;
  els.cropImage.style.top = `${top}px`;
}

function cropPayloadFromState() {
  if (!cropState.loaded) {
    return null;
  }

  const geometry = getGeometry();
  const left = (geometry.wrapW - geometry.drawW) / 2 + cropState.offsetX;
  const top = (geometry.wrapH - geometry.drawH) / 2 + cropState.offsetY;

  let x = (0 - left) / geometry.scale;
  let y = (0 - top) / geometry.scale;
  let width = geometry.wrapW / geometry.scale;
  let height = geometry.wrapH / geometry.scale;

  x = Math.max(0, Math.min(x, cropState.naturalW - 1));
  y = Math.max(0, Math.min(y, cropState.naturalH - 1));
  width = Math.max(1, Math.min(width, cropState.naturalW - x));
  height = Math.max(1, Math.min(height, cropState.naturalH - y));

  return {
    x,
    y,
    width,
    height,
    rotation: cropState.rotation,
    quarter_turns: cropState.quarterTurns,
    flip_horizontal: cropState.flipHorizontal,
  };
}

function buildRotatedDataUrl() {
  const image = cropState.originalImage;
  const rad = (getTotalRotationDegrees() * Math.PI) / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);

  const sourceW = image.naturalWidth;
  const sourceH = image.naturalHeight;
  const targetW = Math.max(1, Math.ceil(Math.abs(sourceW * cos) + Math.abs(sourceH * sin)));
  const targetH = Math.max(1, Math.ceil(Math.abs(sourceW * sin) + Math.abs(sourceH * cos)));

  const canvas = document.createElement("canvas");
  canvas.width = targetW;
  canvas.height = targetH;

  const ctx = canvas.getContext("2d");
  ctx.translate(targetW / 2, targetH / 2);
  ctx.rotate(rad);
  if (cropState.flipHorizontal) {
    ctx.scale(-1, 1);
  }
  ctx.drawImage(image, -sourceW / 2, -sourceH / 2);

  return canvas.toDataURL("image/jpeg", 0.95);
}

function rebuildRotatedPreview(resetTransform) {
  if (!cropState.originalImage) {
    return;
  }

  const version = ++cropState.renderVersion;
  const dataUrl = buildRotatedDataUrl();
  els.cropImage.src = dataUrl;
  els.cropImage.style.display = "block";

  els.cropImage.onload = () => {
    if (version !== cropState.renderVersion) {
      return;
    }

    cropState.loaded = true;
    cropState.naturalW = els.cropImage.naturalWidth;
    cropState.naturalH = els.cropImage.naturalHeight;
    cropState.safeW = cropState.naturalW;
    cropState.safeH = cropState.naturalH;
    cropState.minZoom = 1;

    if (resetTransform) {
      cropState.zoom = 1;
      cropState.offsetX = 0;
      cropState.offsetY = 0;
    }

    updateCropTransform();
  };
}

function bindCropEvents() {
  els.cropWrap.addEventListener("pointerdown", (event) => {
    if (!cropState.loaded) {
      return;
    }

    cropState.dragging = true;
    cropState.startX = event.clientX;
    cropState.startY = event.clientY;
    els.cropWrap.setPointerCapture(event.pointerId);
  });

  els.cropWrap.addEventListener("pointermove", (event) => {
    if (!cropState.dragging) {
      return;
    }

    cropState.offsetX += event.clientX - cropState.startX;
    cropState.offsetY += event.clientY - cropState.startY;
    cropState.startX = event.clientX;
    cropState.startY = event.clientY;
    updateCropTransform();
  });

  const endDrag = () => {
    cropState.dragging = false;
  };

  els.cropWrap.addEventListener("pointerup", endDrag);
  els.cropWrap.addEventListener("pointercancel", endDrag);

  els.zoomInput.addEventListener("input", () => {
    cropState.zoom = Number(els.zoomInput.value);
    if (els.zoomValue) {
      els.zoomValue.textContent = `${cropState.zoom.toFixed(2)}x`;
    }
    updateCropTransform();
  });

  els.rotationInput.addEventListener("input", () => {
    cropState.rotation = Number(els.rotationInput.value);
    updateRotationLabel();
    rebuildRotatedPreview(false);
  });

  els.rotate90Btn.addEventListener("click", () => {
    cropState.quarterTurns = (cropState.quarterTurns + 1) % 4;
    updateRotationLabel();
    rebuildRotatedPreview(false);
  });

  els.flipHorizontalBtn.addEventListener("click", () => {
    cropState.flipHorizontal = !cropState.flipHorizontal;
    updateFlipButtonUi();
    rebuildRotatedPreview(false);
  });

  window.addEventListener("resize", updateCropTransform);
}

async function submitUpload(event) {
  event.preventDefault();
  const submitterId = event.submitter?.id;
  if (submitterId !== "confirmUploadBtn" && submitterId !== "confirmUploadActivateBtn") {
    return;
  }

  const file = els.fileInput.files[0];
  if (!file) {
    toast("Kies eerst een bestand.", "error");
    return;
  }

  if (!cropState.loaded) {
    toast("Crop preview is nog niet geladen.", "error");
    return;
  }

  const payload = new FormData();
  payload.append("file", file);
  payload.append("crop", JSON.stringify(cropPayloadFromState()));
  payload.append("activate", String(submitterId === "confirmUploadActivateBtn"));
  const uploadTvIps = selectedUploadTvIps();
  const finalTvIps = uploadTvIps.length ? uploadTvIps : [...(state.settings.tv_ips || [])];
  payload.append("tv_ips", finalTvIps.join(","));

  const submitButtons = [els.confirmUploadBtn, els.confirmUploadActivateBtn].filter(Boolean);
  submitButtons.forEach((button) => {
    button.disabled = true;
  });

  try {
    const response = await api("/api/upload", {
      method: "POST",
      body: payload,
    });

    const result = readData(response);
    if (result.duplicate) {
      toast("Duplicaat gedetecteerd: bestaand item behouden.", "info");
    } else {
      toast("Upload voltooid.", "success");
    }

    els.uploadDialog.close();
    resetCropState();
    els.uploadForm.reset();
    await reloadData();
  } catch (error) {
    toast(`Upload mislukt: ${error.message} [${error.code || "UNKNOWN"}]`, "error");
  } finally {
    submitButtons.forEach((button) => {
      button.disabled = false;
    });
  }
}

function initializeUploadDialog() {
  els.openUploadBtn.addEventListener("click", () => {
    renderUploadTvList();
    els.uploadDialog.showModal();
  });

  els.cancelUploadBtn.addEventListener("click", () => {
    els.uploadDialog.close();
  });

  els.fileInput.addEventListener("change", () => {
    const file = els.fileInput.files[0];
    if (!file) {
      resetCropState();
      return;
    }

    resetCropState();

    const objectUrl = URL.createObjectURL(file);
    cropState.sourceObjectUrl = objectUrl;

    const original = new Image();
    original.onload = () => {
      cropState.originalImage = original;
      cropState.rotation = Number(els.rotationInput.value);
      updateRotationLabel();
      updateFlipButtonUi();
      rebuildRotatedPreview(true);
    };

    original.src = objectUrl;
  });

  els.uploadDialog.addEventListener("close", () => {
    resetCropState();
    els.uploadForm.reset();
  });

  els.uploadForm.addEventListener("submit", submitUpload);
}

function initializeDeleteDialog() {
  els.cancelDeleteBtn.addEventListener("click", () => {
    state.deleteAsset = null;
    els.deleteDialog.close();
  });

  els.deleteForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.submitter?.id !== "confirmDeleteBtn") {
      return;
    }

    if (!state.deleteAsset) {
      els.deleteDialog.close();
      return;
    }

    const target = els.deleteForm.querySelector("input[name='deleteTarget']:checked").value;
    const tvIps = selectedTvIps();

    els.confirmDeleteBtn.disabled = true;
    await runDelete(state.deleteAsset, target, tvIps);
    els.confirmDeleteBtn.disabled = false;

    state.deleteAsset = null;
    els.deleteDialog.close();
  });

  els.deleteDialog.addEventListener("close", () => {
    state.deleteAsset = null;
  });
}

function initializeResultDialog() {
  els.retryFailedBtn.addEventListener("click", retryFailed);
  els.resultDialog.addEventListener("close", () => {
    state.retryContext = null;
    els.retryFailedBtn.hidden = true;
  });
}

function initializeSettingsDialog() {
  els.openSettingsBtn.addEventListener("click", () => {
    openSettingsDialog();
  });

  els.cancelSettingsBtn.addEventListener("click", () => {
    els.settingsDialog.close();
  });

  els.addTvIpBtn.addEventListener("click", addTvIpFromInput);

  els.newTvIpInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addTvIpFromInput();
    }
  });

  els.discoverBtn.addEventListener("click", discoverTvs);
  els.settingsForm.addEventListener("submit", saveSettings);
}

function initializeTopBar() {
  els.filterSelect.addEventListener("change", async () => {
    state.filter = els.filterSelect.value;
    await reloadData();
  });

  els.refreshBtn.addEventListener("click", async () => {
    try {
      await api("/api/refresh", { method: "POST" });
      await reloadData();
      toast("Refresh gestart.", "success");
    } catch (error) {
      toast(`Refresh mislukt: ${error.message} [${error.code || "UNKNOWN"}]`, "error");
    }
  });

  els.tvPanel.addEventListener("change", async () => {
    state.selectedTvIps = selectedTvIps();
    await reloadData();
  });
}

async function init() {
  bindCropEvents();
  initializeUploadDialog();
  initializeDeleteDialog();
  initializeResultDialog();
  initializeSettingsDialog();
  initializeTopBar();

  await loadSettings();
  state.selectedTvIps = [...state.settings.tv_ips];

  await reloadData();

  state.timer = setInterval(async () => {
    try {
      if (thumbQueueState.active > 0 || thumbQueueState.queue.length > 0) {
        return;
      }
      await reloadData();
    } catch (error) {
      console.warn(error);
    }
  }, 30000);
}

init().catch((error) => {
  toast(`Initialisatie mislukt: ${error.message}`, "error");
});
