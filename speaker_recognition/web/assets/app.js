"use strict";

const basePath = document.querySelector('meta[name="ingress-base"]').content;
const apiUrl = (path) => `${basePath}${path.replace(/^\//, "")}`;
const state = { speakers: [], samples: [], testSample: null, recording: null, satellites: [], persons: [], satelliteSession: null, previewUrls: [], speechExamples: [], speechExampleIndex: -1 };

const elements = {
  dialog: document.querySelector("#enroll-dialog"),
  form: document.querySelector("#enroll-form"),
  name: document.querySelector("#speaker-name"),
  person: document.querySelector("#speaker-person"),
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
  testDialog: document.querySelector("#test-dialog"),
  testForm: document.querySelector("#test-form"),
  testFile: document.querySelector("#test-audio-file"),
  testSample: document.querySelector("#test-sample"),
  testError: document.querySelector("#test-form-error"),
  testRecord: document.querySelector("#test-record-button"),
  testRecognize: document.querySelector("#recognize-sample"),
  testResult: document.querySelector("#test-result"),
  testSatellite: document.querySelector("#test-voice-satellite"),
  testVoiceRecord: document.querySelector("#test-voice-record-button"),
  satellite: document.querySelector("#voice-satellite"),
  voiceRecord: document.querySelector("#voice-record-button"),
  speechExample: document.querySelector("#speech-example-text"),
  newSpeechExample: document.querySelector("#new-speech-example"),
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
    if (speaker.person_entity_id) {
      const person = document.createElement("span"); person.textContent = `Persoon: ${speaker.person_entity_id}`;
      meta.append(person);
    }
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
  revokePreviewUrls();
  state.samples = [];
  elements.form.reset();
  elements.error.hidden = true;
  renderSamples();
  elements.dialog.showModal();
  elements.name.focus();
  await Promise.all([loadSatellites(), loadPersons(), loadSpeechExamples()]);
}

async function openTest() {
  revokePreviewUrls();
  state.testSample = null;
  elements.testForm.reset();
  elements.testError.hidden = true;
  renderTestSample();
  elements.testDialog.showModal();
  await loadTestSatellites();
}

async function loadSpeechExamples() {
  if (!state.speechExamples.length) {
    try { state.speechExamples = await request("assets/speech-prompts.json"); }
    catch (_) { return; }
  }
  selectSpeechExample();
}

function selectSpeechExample() {
  if (!state.speechExamples.length) return;
  let nextIndex = Math.floor(Math.random() * state.speechExamples.length);
  if (nextIndex === state.speechExampleIndex && state.speechExamples.length > 1) {
    nextIndex = (nextIndex + 1 + Math.floor(Math.random() * (state.speechExamples.length - 1))) % state.speechExamples.length;
  }
  state.speechExampleIndex = nextIndex;
  elements.speechExample.textContent = state.speechExamples[nextIndex];
}

async function loadPersons() {
  elements.person.replaceChildren(new Option("Personen laden…", ""));
  try {
    state.persons = await request("api/home-assistant-persons");
    elements.person.replaceChildren(
      new Option("Niet gekoppeld", ""),
      ...state.persons.map((item) => new Option(item.name, item.entity_id)),
    );
  } catch (error) {
    elements.person.replaceChildren(new Option("Personen niet bereikbaar", ""));
    showToast(error.message);
  }
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

async function loadTestSatellites() {
  elements.testSatellite.replaceChildren(new Option("Voice-apparaten laden…", ""));
  elements.testVoiceRecord.disabled = true;
  try {
    state.satellites = await request("api/assist-satellites");
    const available = state.satellites.filter((item) => item.state === "idle");
    elements.testSatellite.replaceChildren(
      new Option(available.length ? "Kies een Voice-apparaat" : "Geen beschikbaar Voice-apparaat", ""),
      ...available.map((item) => new Option(item.name, item.entity_id)),
    );
  } catch (error) {
    elements.testSatellite.replaceChildren(new Option("Voice-apparaten niet bereikbaar", ""));
    setTestError(error.message);
  }
}

async function captureFromSatellite(mode = "enroll") {
  const testing = mode === "test";
  const satelliteSelect = testing ? elements.testSatellite : elements.satellite;
  const recordButton = testing ? elements.testVoiceRecord : elements.voiceRecord;
  const activeDialog = testing ? elements.testDialog : elements.dialog;
  const entityId = satelliteSelect.value;
  if (!entityId) return;
  let sessionId = null;
  if (testing) elements.testError.hidden = true; else elements.error.hidden = true;
  recordButton.disabled = true;
  recordButton.textContent = "Luisteren…";
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
    const sample = {
      ...session.audio,
      duration: bytes / 2 / session.audio.sample_rate,
      label: state.satellites.find((item) => item.entity_id === entityId)?.name || "Home Assistant Voice",
      source: "voice",
    };
    if (testing) {
      state.testSample = sample;
      renderTestSample();
    } else {
      state.samples.push(sample);
      renderSamples();
      selectSpeechExample();
    }
    showToast("Voice-fragment ontvangen");
  } catch (error) {
    if (activeDialog.open) (testing ? setTestError : setFormError)(error.message);
  } finally {
    if (state.satelliteSession === sessionId) state.satelliteSession = null;
    recordButton.textContent = "Opnemen via Voice";
    recordButton.disabled = !satelliteSelect.value;
  }
}

function renderSamples() {
  revokePreviewUrls();
  elements.samples.replaceChildren(...state.samples.map((sample, index) => {
    const row = document.createElement("div"); row.className = "sample-row";
    const icon = document.createElement("span"); icon.textContent = sample.source === "microfoon" ? "●" : sample.source === "voice" ? "◉" : "♪";
    const description = document.createElement("span"); description.textContent = `${sample.label} · ${sample.duration.toFixed(1)} sec`;
    const preview = document.createElement("audio"); preview.controls = true; preview.preload = "metadata"; preview.ariaLabel = `${sample.label} afspelen`;
    preview.src = samplePreviewUrl(sample); state.previewUrls.push(preview.src);
    const remove = document.createElement("button"); remove.type = "button"; remove.ariaLabel = "Sample verwijderen"; remove.textContent = "×";
    remove.addEventListener("click", () => { state.samples.splice(index, 1); renderSamples(); });
    row.append(icon, description, preview, remove); return row;
  }));
  updateSaveState();
}

function renderTestSample() {
  revokePreviewUrls();
  elements.testSample.replaceChildren();
  if (state.testSample) {
    const sample = state.testSample;
    const row = document.createElement("div"); row.className = "sample-row";
    const icon = document.createElement("span"); icon.textContent = sample.source === "microfoon" ? "●" : sample.source === "voice" ? "◉" : "♪";
    const description = document.createElement("span"); description.textContent = `${sample.label} · ${sample.duration.toFixed(1)} sec`;
    const preview = document.createElement("audio"); preview.controls = true; preview.preload = "metadata"; preview.ariaLabel = `${sample.label} afspelen`;
    preview.src = samplePreviewUrl(sample); state.previewUrls.push(preview.src);
    const remove = document.createElement("button"); remove.type = "button"; remove.ariaLabel = "Testfragment verwijderen"; remove.textContent = "×";
    remove.addEventListener("click", () => { state.testSample = null; renderTestSample(); });
    row.append(icon, description, preview, remove);
    elements.testSample.append(row);
  }
  elements.testRecognize.disabled = !state.testSample;
}

function updateSaveState() {
  elements.save.disabled = state.samples.length === 0 || !elements.name.value.trim();
}

function revokePreviewUrls() {
  state.previewUrls.forEach((url) => URL.revokeObjectURL(url));
  state.previewUrls = [];
}

function samplePreviewUrl(sample) {
  const binary = atob(sample.audio_data);
  const pcm = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) pcm[index] = binary.charCodeAt(index);
  const wav = new ArrayBuffer(44 + pcm.length);
  const view = new DataView(wav);
  const write = (offset, value) => [...value].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0)));
  write(0, "RIFF"); view.setUint32(4, 36 + pcm.length, true); write(8, "WAVE"); write(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sample.sample_rate, true); view.setUint32(28, sample.sample_rate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, "data"); view.setUint32(40, pcm.length, true);
  new Uint8Array(wav, 44).set(pcm);
  return URL.createObjectURL(new Blob([wav], { type: "audio/wav" }));
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

async function toggleRecording(mode = "enroll") {
  if (state.recording) { stopRecording(); return; }
  const testing = mode === "test";
  const recordButton = testing ? elements.testRecord : elements.record;
  if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext) {
    (testing ? setTestError : setFormError)("Microfoonopname is hier niet beschikbaar. Open Home Assistant via HTTPS of upload een audiofragment."); return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    processor.onaudioprocess = (event) => chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
    source.connect(processor); processor.connect(context.destination);
    state.recording = { stream, context, source, processor, chunks, started: performance.now(), mode };
    recordButton.classList.add("recording"); recordButton.querySelector("span").textContent = "Opname stoppen";
  } catch (_) { (testing ? setTestError : setFormError)("Geen toegang tot de microfoon. Controleer de browsertoestemming of gebruik upload."); }
}

function stopRecording() {
  const recording = state.recording; if (!recording) return;
  const testing = recording.mode === "test";
  const recordButton = testing ? elements.testRecord : elements.record;
  recording.processor.disconnect(); recording.source.disconnect(); recording.stream.getTracks().forEach((track) => track.stop());
  const length = recording.chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const merged = new Float32Array(length); let offset = 0;
  recording.chunks.forEach((chunk) => { merged.set(chunk, offset); offset += chunk.length; });
  const sampleRate = recording.context.sampleRate; recording.context.close(); state.recording = null;
  recordButton.classList.remove("recording"); recordButton.querySelector("span").textContent = "Opnemen";
  try {
    const sample = makeSample(merged, sampleRate, "Microfoonopname", "microfoon");
    if (testing) { state.testSample = sample; renderTestSample(); }
    else { state.samples.push(sample); renderSamples(); selectSpeechExample(); }
  } catch (error) { (testing ? setTestError : setFormError)(error.message); }
}

function discardRecording() {
  const recording = state.recording; if (!recording) return;
  const recordButton = recording.mode === "test" ? elements.testRecord : elements.record;
  recording.processor.disconnect(); recording.source.disconnect(); recording.stream.getTracks().forEach((track) => track.stop());
  recording.context.close(); state.recording = null;
  recordButton.classList.remove("recording"); recordButton.querySelector("span").textContent = "Opnemen";
}

function closeEnroll() {
  if (state.recording) discardRecording();
  const sessionId = state.satelliteSession;
  state.satelliteSession = null;
  revokePreviewUrls();
  state.samples = [];
  if (elements.dialog.open) elements.dialog.close("cancel");
  if (sessionId) request(`api/satellite-enrollment/${sessionId}`, { method: "DELETE" }).catch(() => {});
}

function closeTest() {
  if (state.recording?.mode === "test") discardRecording();
  const sessionId = state.satelliteSession;
  state.satelliteSession = null;
  revokePreviewUrls();
  state.testSample = null;
  if (elements.testDialog.open) elements.testDialog.close("cancel");
  if (sessionId) request(`api/satellite-enrollment/${sessionId}`, { method: "DELETE" }).catch(() => {});
}

async function saveSpeaker() {
  const name = elements.name.value.trim(); if (!name || !state.samples.length) return;
  elements.save.disabled = true; elements.save.textContent = "Embedding maken…"; elements.error.hidden = true;
  try {
    await request("api/enroll", { method: "POST", body: JSON.stringify({ speaker_name: name, person_entity_id: elements.person.value || null, replace: document.querySelector("#replace-profile").checked, samples: state.samples.map((item) => ({ audio: { audio_data: item.audio_data, sample_rate: item.sample_rate } })) }) });
    elements.dialog.close(); showToast(`${name} is succesvol enrolled`); await refresh();
  } catch (error) { setFormError(error.message); }
  finally { elements.save.textContent = "Profiel opslaan"; renderSamples(); }
}

async function deleteSpeaker(speaker) {
  if (!confirm(`Stemprofiel “${speaker.name}” definitief verwijderen?`)) return;
  try { await request(`api/speakers/${encodeURIComponent(speaker.id)}`, { method: "DELETE" }); showToast(`${speaker.name} is verwijderd`); await refresh(); }
  catch (error) { showToast(error.message); }
}

async function testAudio() {
  if (!state.testSample) return;
  elements.testResult.hidden = false; elements.testResult.classList.remove("no-match"); elements.testResult.textContent = "Analyseren…";
  elements.testRecognize.disabled = true; elements.testRecognize.textContent = "Analyseren…";
  try {
    const sample = state.testSample;
    const result = await request("api/recognize", { method: "POST", body: JSON.stringify({ audio: { audio_data: sample.audio_data, sample_rate: sample.sample_rate } }) });
    elements.testResult.classList.toggle("no-match", !result.matched);
    elements.testResult.textContent = result.matched ? `${result.speaker.name} · ${(result.confidence * 100).toFixed(1)}%` : `Onbekende speaker · ${(result.confidence * 100).toFixed(1)}%`;
    closeTest();
  } catch (error) { elements.testResult.classList.add("no-match"); elements.testResult.textContent = error.message; }
  finally { elements.testRecognize.textContent = "Fragment testen"; elements.testRecognize.disabled = !state.testSample; }
}

function setFormError(message) { elements.error.textContent = message; elements.error.hidden = false; }
function setTestError(message) { elements.testError.textContent = message; elements.testError.hidden = false; }
function showToast(message) { elements.toast.textContent = message; elements.toast.classList.add("show"); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => elements.toast.classList.remove("show"), 3200); }

document.querySelector("#open-enroll").addEventListener("click", openEnroll);
document.querySelector('[data-action="enroll"]').addEventListener("click", openEnroll);
document.querySelector("#open-test").addEventListener("click", openTest);
elements.name.addEventListener("input", updateSaveState);
elements.search.addEventListener("input", renderSpeakers);
elements.record.addEventListener("click", () => toggleRecording("enroll"));
elements.testRecord.addEventListener("click", () => toggleRecording("test"));
elements.satellite.addEventListener("change", () => { elements.voiceRecord.disabled = !elements.satellite.value; });
elements.testSatellite.addEventListener("change", () => { elements.testVoiceRecord.disabled = !elements.testSatellite.value; });
elements.voiceRecord.addEventListener("click", () => captureFromSatellite("enroll"));
elements.testVoiceRecord.addEventListener("click", () => captureFromSatellite("test"));
elements.newSpeechExample.addEventListener("click", selectSpeechExample);
elements.save.addEventListener("click", saveSpeaker);
elements.audioFiles.addEventListener("change", async () => {
  elements.error.hidden = true;
  for (const file of elements.audioFiles.files) {
    try { state.samples.push(await decodeFile(file)); } catch (error) { setFormError(error.message); }
  }
  elements.audioFiles.value = ""; renderSamples();
});
elements.testFile.addEventListener("change", async () => {
  elements.testError.hidden = true;
  if (elements.testFile.files[0]) {
    try { state.testSample = await decodeFile(elements.testFile.files[0]); renderTestSample(); }
    catch (error) { setTestError(error.message); }
  }
  elements.testFile.value = "";
});
elements.testRecognize.addEventListener("click", testAudio);
document.querySelectorAll('[data-action="close-enroll"]').forEach((button) => button.addEventListener("click", closeEnroll));
document.querySelectorAll('[data-action="close-test"]').forEach((button) => button.addEventListener("click", closeTest));
elements.dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeEnroll(); });
elements.testDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeTest(); });

refresh();
