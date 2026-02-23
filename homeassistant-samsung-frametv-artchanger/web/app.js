const state = {
  filter: "all",
  tvs: [],
  gallery: [],
  selectedTvIps: [],
  timer: null,
};

const basePath = window.location.pathname.replace(/\/+$/, "");
const withBasePath = (path) => `${basePath}${path}`;

const els = {
  filterSelect: document.getElementById("filterSelect"),
  refreshBtn: document.getElementById("refreshBtn"),
  openUploadBtn: document.getElementById("openUploadBtn"),
  tvPanel: document.getElementById("tvPanel"),
  gallery: document.getElementById("gallery"),
  cardTemplate: document.getElementById("cardTemplate"),
  uploadDialog: document.getElementById("uploadDialog"),
  uploadForm: document.getElementById("uploadForm"),
  fileInput: document.getElementById("fileInput"),
  cropWrap: document.getElementById("cropWrap"),
  cropImage: document.getElementById("cropImage"),
  zoomInput: document.getElementById("zoomInput"),
  activateInput: document.getElementById("activateInput"),
  uploadTvIps: document.getElementById("uploadTvIps"),
};

const cropState = {
  objectUrl: null,
  loaded: false,
  naturalW: 0,
  naturalH: 0,
  baseScale: 1,
  zoom: 1,
  offsetX: 0,
  offsetY: 0,
  dragging: false,
  startX: 0,
  startY: 0,
};

function selectedTvIps() {
  const checked = [...document.querySelectorAll(".tv-target:checked")].map((node) => node.value);
  return checked;
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

async function api(path, options = {}) {
  const response = await fetch(withBasePath(path), {
    headers: {
      ...(options.headers || {}),
    },
    ...options,
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (data.detail) {
        detail = data.detail;
      }
    } catch (_) {
      // Ignore JSON parse failures.
    }
    throw new Error(detail);
  }

  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response;
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

    thumb.src = withBasePath(
      `/api/thumb/${encodeURIComponent(item.asset_id)}?v=${encodeURIComponent(item.updated_at || "")}`,
    );
    thumb.loading = "lazy";

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
      const tvIps = selectedTvIps();
      try {
        await api(`/api/items/${encodeURIComponent(item.asset_id)}/activate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tv_ips: tvIps,
            ensure_upload: true,
            activate: true,
          }),
        });
        await reloadData();
      } catch (error) {
        alert(`Activeren mislukt: ${error.message}`);
      }
    });

    fragment.querySelector(".delete-btn").addEventListener("click", async () => {
      const target = (prompt("Verwijder target: tv | ha | both", "both") || "").trim().toLowerCase();
      if (!["tv", "ha", "both"].includes(target)) {
        alert("Ongeldige target. Kies tv, ha of both.");
        return;
      }

      if (!confirm(`Weet je zeker dat je '${item.filename}' wilt verwijderen op target: ${target}?`)) {
        return;
      }

      try {
        await api(`/api/items/${encodeURIComponent(item.asset_id)}`, {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            targets: target,
            tv_ips: selectedTvIps(),
          }),
        });
        await reloadData();
      } catch (error) {
        alert(`Verwijderen mislukt: ${error.message}`);
      }
    });

    root.dataset.assetId = item.asset_id;
    els.gallery.appendChild(fragment);
  });
}

async function reloadData() {
  setBusy(true);
  try {
    const previousSelection = selectedTvIps();
    if (previousSelection.length) {
      state.selectedTvIps = previousSelection;
    }

    const tvPayload = await api("/api/tvs");
    state.tvs = tvPayload.tvs || [];
    if (!state.selectedTvIps.length) {
      state.selectedTvIps = state.tvs.map((tv) => tv.ip);
    }
    renderTvPanel();

    const tvSelection = selectedTvIps();
    const tvQuery = tvSelection.length === 1 ? `&tv_ip=${encodeURIComponent(tvSelection[0])}` : "";
    const galleryPayload = await api(`/api/gallery?filter=${encodeURIComponent(state.filter)}${tvQuery}`);

    state.gallery = galleryPayload.items || [];
    renderGallery();
  } finally {
    setBusy(false);
  }
}

function resetCropState() {
  cropState.loaded = false;
  cropState.naturalW = 0;
  cropState.naturalH = 0;
  cropState.baseScale = 1;
  cropState.zoom = 1;
  cropState.offsetX = 0;
  cropState.offsetY = 0;
  cropState.dragging = false;
  els.zoomInput.value = "1";

  if (cropState.objectUrl) {
    URL.revokeObjectURL(cropState.objectUrl);
    cropState.objectUrl = null;
  }

  els.cropImage.removeAttribute("src");
  els.cropImage.style.display = "none";
}

function updateCropTransform() {
  if (!cropState.loaded) {
    return;
  }

  const wrapRect = els.cropWrap.getBoundingClientRect();
  const wrapW = wrapRect.width;
  const wrapH = wrapRect.height;

  cropState.baseScale = Math.max(wrapW / cropState.naturalW, wrapH / cropState.naturalH);
  const scale = cropState.baseScale * cropState.zoom;

  const drawW = cropState.naturalW * scale;
  const drawH = cropState.naturalH * scale;

  const left = (wrapW - drawW) / 2 + cropState.offsetX;
  const top = (wrapH - drawH) / 2 + cropState.offsetY;

  els.cropImage.style.width = `${drawW}px`;
  els.cropImage.style.height = `${drawH}px`;
  els.cropImage.style.left = `${left}px`;
  els.cropImage.style.top = `${top}px`;
}

function cropPayloadFromState() {
  if (!cropState.loaded) {
    return null;
  }

  const wrapRect = els.cropWrap.getBoundingClientRect();
  const wrapW = wrapRect.width;
  const wrapH = wrapRect.height;

  const scale = cropState.baseScale * cropState.zoom;
  const drawW = cropState.naturalW * scale;
  const drawH = cropState.naturalH * scale;
  const left = (wrapW - drawW) / 2 + cropState.offsetX;
  const top = (wrapH - drawH) / 2 + cropState.offsetY;

  let x = (0 - left) / scale;
  let y = (0 - top) / scale;
  let width = wrapW / scale;
  let height = wrapH / scale;

  x = Math.max(0, Math.min(x, cropState.naturalW - 1));
  y = Math.max(0, Math.min(y, cropState.naturalH - 1));
  width = Math.max(1, Math.min(width, cropState.naturalW - x));
  height = Math.max(1, Math.min(height, cropState.naturalH - y));

  return { x, y, width, height };
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
    updateCropTransform();
  });

  window.addEventListener("resize", updateCropTransform);
}

async function submitUpload(event) {
  event.preventDefault();

  const file = els.fileInput.files[0];
  if (!file) {
    alert("Kies eerst een bestand.");
    return;
  }

  if (!cropState.loaded) {
    alert("Crop preview is nog niet geladen.");
    return;
  }

  const payload = new FormData();
  payload.append("file", file);
  payload.append("crop", JSON.stringify(cropPayloadFromState()));
  payload.append("activate", String(els.activateInput.checked));
  payload.append("tv_ips", els.uploadTvIps.value.trim());

  try {
    await api("/api/upload", {
      method: "POST",
      body: payload,
    });

    els.uploadDialog.close();
    resetCropState();
    els.uploadForm.reset();
    await reloadData();
  } catch (error) {
    alert(`Upload mislukt: ${error.message}`);
  }
}

function initializeUploadDialog() {
  els.openUploadBtn.addEventListener("click", () => {
    els.uploadDialog.showModal();
  });

  els.fileInput.addEventListener("change", () => {
    const file = els.fileInput.files[0];
    if (!file) {
      resetCropState();
      return;
    }

    resetCropState();
    const url = URL.createObjectURL(file);
    cropState.objectUrl = url;
    els.cropImage.src = url;
    els.cropImage.style.display = "block";

    els.cropImage.onload = () => {
      cropState.loaded = true;
      cropState.naturalW = els.cropImage.naturalWidth;
      cropState.naturalH = els.cropImage.naturalHeight;
      cropState.zoom = 1;
      cropState.offsetX = 0;
      cropState.offsetY = 0;
      updateCropTransform();
    };
  });

  els.uploadDialog.addEventListener("close", () => {
    resetCropState();
    els.uploadForm.reset();
  });

  els.uploadForm.addEventListener("submit", submitUpload);
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
    } catch (error) {
      alert(`Refresh mislukt: ${error.message}`);
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
  initializeTopBar();

  await reloadData();

  state.timer = setInterval(async () => {
    try {
      await reloadData();
    } catch (error) {
      console.warn(error);
    }
  }, 30000);
}

init().catch((error) => {
  alert(`Initialisatie mislukt: ${error.message}`);
});
