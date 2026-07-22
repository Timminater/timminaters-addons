"use strict";

const basePath = document.querySelector('meta[name="ingress-base"]').content;
const apiUrl = (path) => `${basePath}${path.replace(/^\//, "")}`;
const state = { speakers: [], samples: [], recording: null, satellites: [], satelliteSession: null };

const elements = {
  dialog: document.querySelector("#enroll-dialog"),
  form: document.querySelector("#enroll-form"),
  name: document.querySelector("#speaker-name"),
  samples: document.querySelector("#samples"),
  save: document.querySelector("#save-speaker"),
  error: document.querySelector("#form-error"),
  record: document.querySelector("#record-button"),
  audioFiles: document.querySelector("#audio-files"),
  grid: document.querySelector("#speaker-grid"),
  empty: document.querySelector("#empty-state"),
  search: document.querySelector("#search"),
  status: document.querySelector("#engine-status"),
  toast: document.querySelector("#toast"),
  testFile: document.querySelector("#test-file"),
  testResult: document.querySelector("#test-result"),
  satellite: document.querySelector("#voice-satellite"),
  voiceRecord: document.querySelector("#voice-record-button"),
};

async function request(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `Verzoek mislukt (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) { /* no JSON */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function initials(name) {
  return name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
}

function renderSpeakers() {
  const query = elements.search.value.trim().toLocaleLowerCase();
  const speakers = state.speakers.filter((item) => item.name.toLocaleLowerCase().includes(query));
  elements.grid.replaceChildren(...speakers.map((speaker) => {
    const card = document.createElement("article");
    card.className = "speaker-card";
    const avatar = document.createElement("div"); avatar.className = "avatar"; avatar.textContent = initials(speaker.name);
    const meta = document.createElement("div"); meta.className = "speaker-meta";
    const name = document.createElement("strong"); name.textContent = speaker.name;
    const count = document.createElement("span"); count.textContent = `${speaker.sample_count} sample${speaker.sample_count === 1 ? "" : "s"}`;
    meta.append(name, count);
    const remove = document.createElement("button"); remove.className = "delete-button"; remove.type = "button"; remove.ariaLabel = `${speaker.name} verwijderen`; remove.textContent = "⌫";
    remove.addEventListener("click", () => deleteSpeaker(speaker));
    card.append(avatar, meta, remove);
    return card;
  }));
  elements.empty.hidden = state.speakers.length !== 0;
  document.querySelector("#speaker-count").textContent = state.speakers.length;
  document.querySelector("#sample-count").textContent = state.speakers.reduce((sum, item) => sum + item.sample_count, 0);
}

async function refresh() {
  try {
    const [health, speakers] = await Promise.all([request("health"), request("api/speakers")]);
    state.speakers = speakers;
    elements.status.classList.toggle("ready", health.ready);
    elements.status.querySelector("span").textContent = health.ready ? "Engine gereed" : "Engine start…";
    renderSpeakers();
  } catch (error) {
    elements.status.querySelector("span").textContent = "Niet bereikbaar";
    showToast(error.message);
  }
}

async function openEnroll() {
  state.samples = [];
  elements.form.reset();
  elements.error.hidden = true;
  renderSamples();
  elements.dialog.showModal();
  elements.name.focus();
  await loadSatellites();
}

async function loadSatellites() {
  elements.satellite.replaceChildren(new Option("Voice-apparaten laden…", ""));
  elements.voiceRecord.disabled = true;
  try {
    state.satellites = await request("api/assist-satellites");
    const available = state.satellites.filter((item) => item.state === "idle");
    elements.satellite.replaceChildren(
      new Option(available.length ? "Kies een Voice-apparaat" : "Geen beschikbaar Voice-apparaat", ""),
      ...available.map((item) => new Option(item.name, item.entity_id)),
    );
  } catch (error) {
    elements.satellite.replaceChildren(new Option("Voice-apparaten niet bereikbaar", ""));
    setFormError(error.message);
  }
}

async function captureFromSatellite() {
  const entityId = elements.satellite.value;
  if (!entityId) return;
  let sessionId = null;
  elements.error.hidden = true;
  elements.voiceRecord.disabled = true;
  elements.voiceRecord.textContent = "Luisteren…";
  try {
    let session = await request("api/satellite-enrollment", {
      method: "POST",
      body: JSON.stringify({ satellite_entity_id: entityId }),
    });
    sessionId = session.id;
    state.satelliteSession = sessionId;
    while (["armed", "capturing"].includes(session.status)) {
      await new Promise((resolve) => setTimeout(resolve, 600));
      if (state.satelliteSession !== session.id) return;
      session = await request(`api/satellite-enrollment/${session.id}`);
    }
    if (session.status !== "complete" || !session.audio) {
      throw new Error(session.error || "Geen stemfragment ontvangen. Controleer of deze Assist-pipeline de Speaker Recognition STT gebruikt.");
    }
    const bytes = atob(session.audio.audio_data).length;
    state.samples.push({
      ...session.audio,
      duration: bytes / 2 / session.audio.sample_rate,
      label: state.satellites.find((item) => item.entity_id === entityId)?.name || "Home Assistant Voice",
      source: "voice",
    });
    renderSamples();
    showToast("Voice-fragment ontvangen");
    await request(`api/satellite-enrollment/${session.id}`, { method: "DELETE" });
  } catch (error) {
    if (elements.dialog.open) setFormError(error.message);
  } finally {
    if (state.satelliteSession === sessionId) state.satelliteSession = null;
    elements.voiceRecord.textContent = "Opnemen via Voice";
    elements.voiceRecord.disabled = !elements.satellite.value;
  }
}

function renderSamples() {
  elements.samples.replaceChildren(...state.samples.map((sample, index) => {
    const row = document.createElement("div"); row.className = "sample-row";
    const icon = document.createElement("span"); icon.textContent = sample.source === "microfoon" ? "●" : sample.source === "voice" ? "◉" : "♪";
    const description = document.createElement("span"); description.textContent = `${sample.label} · ${sample.duration.toFixed(1)} sec`;
    const remove = document.createElement("button"); remove.type = "button"; remove.ariaLabel = "Sample verwijderen"; remove.textContent = "×";
    remove.addEventListener("click", () => { state.samples.splice(index, 1); renderSamples(); });
    row.append(icon, description, remove); return row;
  }));
  elements.save.disabled = state.samples.length === 0 || !elements.name.value.trim();
}

async function decodeFile(file) {
  if (file.size > 30 * 1024 * 1024) throw new Error(`${file.name} is groter dan 30 MB`);
  const context = new AudioContext();
  try {
    const buffer = await context.decodeAudioData(await file.arrayBuffer());
    return makeSample(buffer.getChannelData(0), buffer.sampleRate, file.name, "upload");
  } catch (error) {
    throw new Error(`${file.name} kan door deze browser niet worden gelezen`);
  } finally { await context.close(); }
}

function makeSample(floatSamples, sourceRate, label, source) {
  const targetRate = 16000;
  const duration = floatSamples.length / sourceRate;
  if (duration < 1) throw new Error("Een stemfragment moet minimaal 1 seconde lang zijn");
  if (duration > 120) throw new Error("Een stemfragment mag maximaal 120 seconden duren");
  const outputLength = Math.round(floatSamples.length * targetRate / sourceRate);
  const pcm = new Int16Array(outputLength);
  for (let index = 0; index < outputLength; index += 1) {
    const position = index * sourceRate / targetRate;
    const left = Math.floor(position);
    const right = Math.min(left + 1, floatSamples.length - 1);
    const fraction = position - left;
    const value = Math.max(-1, Math.min(1, floatSamples[left] * (1 - fraction) + floatSamples[right] * fraction));
    pcm[index] = value < 0 ? value * 32768 : value * 32767;
  }
  const bytes = new Uint8Array(pcm.buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return { audio_data: btoa(binary), sample_rate: targetRate, duration, label, source };
}

async function toggleRecording() {
  if (state.recording) { stopRecording(); return; }
  if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext) {
    setFormError("Microfoonopname is hier niet beschikbaar. Open Home Assistant via HTTPS of upload een audiofragment."); return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    processor.onaudioprocess = (event) => chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
    source.connect(processor); processor.connect(context.destination);
    state.recording = { stream, context, source, processor, chunks, started: performance.now() };
    elements.record.classList.add("recording"); elements.record.querySelector("span").textContent = "Opname stoppen";
  } catch (_) { setFormError("Geen toegang tot de microfoon. Controleer de browsertoestemming of gebruik upload."); }
}

function stopRecording() {
  const recording = state.recording; if (!recording) return;
  recording.processor.disconnect(); recording.source.disconnect(); recording.stream.getTracks().forEach((track) => track.stop());
  const length = recording.chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const merged = new Float32Array(length); let offset = 0;
  recording.chunks.forEach((chunk) => { merged.set(chunk, offset); offset += chunk.length; });
  const sampleRate = recording.context.sampleRate; recording.context.close(); state.recording = null;
  elements.record.classList.remove("recording"); elements.record.querySelector("span").textContent = "Opnemen";
  try { state.samples.push(makeSample(merged, sampleRate, "Microfoonopname", "microfoon")); renderSamples(); }
  catch (error) { setFormError(error.message); }
}

async function saveSpeaker() {
  const name = elements.name.value.trim(); if (!name || !state.samples.length) return;
  elements.save.disabled = true; elements.save.textContent = "Embedding maken…"; elements.error.hidden = true;
  try {
    await request("api/enroll", { method: "POST", body: JSON.stringify({ speaker_name: name, replace: document.querySelector("#replace-profile").checked, samples: state.samples.map((item) => ({ audio: { audio_data: item.audio_data, sample_rate: item.sample_rate } })) }) });
    elements.dialog.close(); showToast(`${name} is succesvol enrolled`); await refresh();
  } catch (error) { setFormError(error.message); }
  finally { elements.save.textContent = "Profiel opslaan"; renderSamples(); }
}

async function deleteSpeaker(speaker) {
  if (!confirm(`Stemprofiel “${speaker.name}” definitief verwijderen?`)) return;
  try { await request(`api/speakers/${encodeURIComponent(speaker.id)}`, { method: "DELETE" }); showToast(`${speaker.name} is verwijderd`); await refresh(); }
  catch (error) { showToast(error.message); }
}

async function testAudio(file) {
  elements.testResult.hidden = false; elements.testResult.classList.remove("no-match"); elements.testResult.textContent = "Analyseren…";
  try {
    const sample = await decodeFile(file);
    const result = await request("api/recognize", { method: "POST", body: JSON.stringify({ audio: { audio_data: sample.audio_data, sample_rate: sample.sample_rate } }) });
    elements.testResult.classList.toggle("no-match", !result.matched);
    elements.testResult.textContent = result.matched ? `${result.speaker.name} · ${(result.confidence * 100).toFixed(1)}%` : `Onbekende speaker · ${(result.confidence * 100).toFixed(1)}%`;
  } catch (error) { elements.testResult.classList.add("no-match"); elements.testResult.textContent = error.message; }
}

function setFormError(message) { elements.error.textContent = message; elements.error.hidden = false; }
function showToast(message) { elements.toast.textContent = message; elements.toast.classList.add("show"); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => elements.toast.classList.remove("show"), 3200); }

document.querySelector("#open-enroll").addEventListener("click", openEnroll);
document.querySelector('[data-action="enroll"]').addEventListener("click", openEnroll);
elements.name.addEventListener("input", renderSamples);
elements.search.addEventListener("input", renderSpeakers);
elements.record.addEventListener("click", toggleRecording);
elements.satellite.addEventListener("change", () => { elements.voiceRecord.disabled = !elements.satellite.value; });
elements.voiceRecord.addEventListener("click", captureFromSatellite);
elements.save.addEventListener("click", saveSpeaker);
elements.audioFiles.addEventListener("change", async () => {
  elements.error.hidden = true;
  for (const file of elements.audioFiles.files) {
    try { state.samples.push(await decodeFile(file)); } catch (error) { setFormError(error.message); }
  }
  elements.audioFiles.value = ""; renderSamples();
});
elements.testFile.addEventListener("change", () => { if (elements.testFile.files[0]) testAudio(elements.testFile.files[0]); elements.testFile.value = ""; });
elements.dialog.addEventListener("close", () => {
  if (state.recording) stopRecording();
  if (state.satelliteSession) {
    request(`api/satellite-enrollment/${state.satelliteSession}`, { method: "DELETE" }).catch(() => {});
    state.satelliteSession = null;
  }
});

refresh();
