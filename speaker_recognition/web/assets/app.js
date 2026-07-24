"use strict";

const basePath = document.querySelector('meta[name="ingress-base"]').content;
const apiUrl = (path) => `${basePath}${path.replace(/^\//, "")}`;
const AUDIO_VARIANTS = { original: "origineel", denoised: "ruisonderdrukt", isolated: "oude geïsoleerde opname", extracted: "legacy-extractie" };
const state = {
  speakers: [], samples: [], testSample: null, recording: null, satellites: [], persons: [], satelliteSession: null,
  previewUrls: [], speechExamples: [], speechExampleIndex: -1, overview: {}, route: "profiles",
  analysis: { items: [], total: 0, offset: 0, limit: 24, selected: new Set(), detail: null, audioUrl: null, duration: 0, trim: { start: 0, end: 0 }, waveform: null },
  calibration: null, confirmation: null, profile: null,
};

const elements = Object.fromEntries([
  "enroll-dialog enroll-form speaker-name speaker-person samples save-speaker form-error record-button audio-files speaker-grid empty-state search engine-status toast",
  "test-dialog test-form test-audio-file test-sample test-form-error test-record-button recognize-sample test-result test-voice-satellite test-voice-record-button voice-satellite voice-record-button speech-example-text new-speech-example",
  "speaker-count sample-count storage-status retention-value",
  "analysis-period analysis-outcome analysis-source analysis-speaker analysis-query analysis-selection-count delete-selected delete-filtered refresh-analysis analysis-loading analysis-grid analysis-empty analysis-pagination analysis-previous analysis-next analysis-page-label",
  "refresh-calibration calibration-loading calibration-content genuine-count impostor-count current-threshold calibration-warning calibration-chart recommended-title recommended-threshold recommended-margin false-accepts false-rejects calibration-detail apply-calibration reset-calibration unknown-speaker-policy extraction-mode policy-min-margin save-policy",
  "profile-dialog profile-dialog-title profile-dialog-description profile-samples profile-delete profile-delete-dialog profile-delete-title archive-profile-audio delete-profile-audio",
  "analysis-dialog analysis-dialog-title analysis-detail-loading analysis-detail analysis-original-audio analysis-denoised-audio denoised-unavailable analysis-waveform trim-start trim-end waveform-range recognition-metadata pipeline-metadata extract-audio download-original promote-audio delete-analysis",
  "promote-dialog promote-existing-field promote-new-fields promote-speaker promote-name promote-person promote-error confirm-promote",
  "confirm-dialog confirm-title confirm-message confirm-checkbox-row confirm-checkbox confirm-checkbox-label confirm-action",
].join(" ").split(" ").filter(Boolean).map((id) => [id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), document.querySelector(`#${id}`)]));
elements.dialog = elements.enrollDialog; elements.form = elements.enrollForm; elements.name = elements.speakerName; elements.person = elements.speakerPerson;
elements.samples = elements.samples; elements.error = elements.formError; elements.grid = elements.speakerGrid; elements.empty = elements.emptyState; elements.search = elements.search; elements.status = elements.engineStatus;
elements.record = elements.recordButton; elements.audioFiles = elements.audioFiles; elements.save = elements.saveSpeaker;
elements.testRecord = elements.testRecordButton; elements.testFile = elements.testAudioFile; elements.testRecognize = elements.recognizeSample;
elements.testSatellite = elements.testVoiceSatellite; elements.testVoiceRecord = elements.testVoiceRecordButton; elements.satellite = elements.voiceSatellite; elements.voiceRecord = elements.voiceRecordButton;
elements.speechExample = elements.speechExampleText; elements.newSpeechExample = elements.newSpeechExample;

function setText(node, value) { node.textContent = value ?? "—"; }
function appendText(parent, tag, value, className) { const child = document.createElement(tag); if (className) child.className = className; child.textContent = value; parent.append(child); return child; }
function number(value, fallback = 0) { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : fallback; }
function formatDuration(seconds) { seconds = number(seconds); return seconds < 60 ? `${seconds.toFixed(seconds < 10 ? 1 : 0)} sec` : `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, "0")}`; }
function formatBytes(bytes) { bytes = number(bytes); if (!bytes) return "0 MB"; const units = ["B", "KB", "MB", "GB"]; const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1); return `${(bytes / 1024 ** exponent).toFixed(exponent > 1 ? 1 : 0)} ${units[exponent]}`; }
function formatScore(value) { return value == null ? "—" : `${(number(value) * 100).toFixed(1)}%`; }
function formatDate(value) { if (!value) return "Onbekend tijdstip"; const parsed = new Date(value); return Number.isNaN(parsed) ? String(value) : new Intl.DateTimeFormat("nl-NL", { dateStyle: "medium", timeStyle: "short" }).format(parsed); }
function initials(name) { return String(name || "?").split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase(); }
function getId(item) { return item.id || item.recording_id || item.speaker_id || item.profile_id; }
function htmlSafeFilename(value) { return String(value || "audio").replace(/[^\p{L}\p{N}._-]+/gu, "-"); }

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(apiUrl(path), { ...options, headers });
  if (!response.ok) {
    let message = `Verzoek mislukt (${response.status})`;
    try { const body = await response.json(); message = body.detail || body.message || message; } catch (_) { /* no JSON response */ }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response;
}

function debounce(callback, delay = 350) { let timer; return (...args) => { clearTimeout(timer); timer = setTimeout(() => callback(...args), delay); }; }
function showToast(message) { setText(elements.toast, message); elements.toast.classList.add("show"); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => elements.toast.classList.remove("show"), 3600); }
function setFormError(message) { setText(elements.formError, message); elements.formError.hidden = false; }
function setTestError(message) { setText(elements.testFormError, message); elements.testFormError.hidden = false; }
function empty(node) { node.replaceChildren(); return node; }

function routeFromHash() { return ["profiles", "analysis", "calibration"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "profiles"; }
async function setRoute(route) {
  state.route = route;
  document.querySelectorAll("[data-page]").forEach((page) => { page.hidden = page.dataset.page !== route; });
  document.querySelectorAll("[data-route]").forEach((link) => link.classList.toggle("active", link.dataset.route === route));
  if (route === "analysis") await loadAnalysis(true);
  if (route === "calibration") await loadCalibration();
}

function analysisFilters() {
  const period = elements.analysisPeriod.value;
  const filters = { outcome: elements.analysisOutcome.value, source: elements.analysisSource.value, speaker_id: elements.analysisSpeaker.value, q: elements.analysisQuery.value.trim() };
  if (period !== "all") {
    const days = period === "today" ? 1 : Number.parseInt(period, 10);
    filters.since = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
  }
  return Object.fromEntries(Object.entries(filters).filter(([, value]) => value));
}
function queryString(parameters) { return new URLSearchParams(Object.entries(parameters).filter(([, value]) => value !== "" && value != null)).toString(); }

async function refreshOverview() {
  try {
    const [health, speakers, overview, policy] = await Promise.all([
      request("health"), request("api/speakers"), request("api/overview").catch(() => ({})), request("api/pipeline-policy").catch(() => ({})),
    ]);
    state.speakers = Array.isArray(speakers) ? speakers : speakers.items || [];
    state.overview = { ...(policy || {}), ...(overview || {}) };
    renderPolicy(policy || {});
    elements.engineStatus.classList.toggle("ready", Boolean(health.ready));
    setText(elements.engineStatus.querySelector("span"), health.ready ? "Engine gereed" : "Engine start…");
    const used = overview.analysis_storage_used_bytes ?? overview.storage_used_bytes;
    const maximum = overview.analysis_max_storage_bytes ?? overview.storage_limit_bytes ?? policy.max_storage_bytes;
    setText(elements.storageStatus, maximum ? `${formatBytes(used)} / ${formatBytes(maximum)} analyse` : used != null ? `${formatBytes(used)} analyse` : "Lokale analyse-opslag");
    const retention = overview.analysis_retention_days ?? overview.retention_days ?? policy.retention_days;
    setText(elements.retentionValue, retention ? `${retention} dagen` : "7 dagen");
    populateSpeakerOptions(); renderSpeakers();
  } catch (error) { setText(elements.engineStatus.querySelector("span"), "Niet bereikbaar"); showToast(error.message); }
}

function populateSpeakerOptions() {
  const current = elements.analysisSpeaker.value;
  elements.analysisSpeaker.replaceChildren(new Option("Alle speakers", ""), ...state.speakers.map((speaker) => new Option(speaker.name, getId(speaker))));
  elements.analysisSpeaker.value = current;
}
function renderPolicy(policy) {
  elements.unknownSpeakerPolicy.value = policy.unknown_speaker_policy || "allow";
  elements.extractionMode.value = policy.extraction_mode || "off";
  elements.policyMinMargin.value = policy.min_margin ?? "0";
}
async function savePolicy() {
  const minMargin = number(elements.policyMinMargin.value, -1);
  if (minMargin < 0 || minMargin > 1) { showToast("De minimale scoremarge moet tussen 0 en 1 liggen."); return; }
  elements.savePolicy.disabled = true;
  try {
    const policy = await request("api/pipeline-policy", { method: "PATCH", body: JSON.stringify({ unknown_speaker_policy: elements.unknownSpeakerPolicy.value, extraction_mode: elements.extractionMode.value, min_margin: minMargin }) });
    state.overview = { ...state.overview, ...policy }; renderPolicy(policy); showToast("Pipelinebeleid opgeslagen");
  } catch (error) { showToast(error.message); } finally { elements.savePolicy.disabled = false; }
}
function renderSpeakers() {
  const query = elements.search.value.trim().toLocaleLowerCase();
  const speakers = state.speakers.filter((item) => String(item.name).toLocaleLowerCase().includes(query));
  empty(elements.speakerGrid);
  speakers.forEach((speaker) => {
    const card = document.createElement("article"); card.className = "speaker-card clickable"; card.tabIndex = 0; card.setAttribute("role", "button"); card.setAttribute("aria-label", `${speaker.name} openen`);
    const avatar = appendText(card, "div", initials(speaker.name), "avatar"); avatar.setAttribute("aria-hidden", "true");
    const meta = document.createElement("div"); meta.className = "speaker-meta"; appendText(meta, "strong", speaker.name); appendText(meta, "span", `${speaker.sample_count ?? speaker.active_sample_count ?? 0} actieve sample${Number(speaker.sample_count ?? speaker.active_sample_count) === 1 ? "" : "s"}`);
    if (speaker.person_entity_id) appendText(meta, "span", `Persoon: ${speaker.person_entity_id}`); card.append(meta);
    const arrow = appendText(card, "span", "›", "card-arrow"); arrow.setAttribute("aria-hidden", "true");
    card.addEventListener("click", () => openProfile(speaker)); card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openProfile(speaker); } });
    elements.speakerGrid.append(card);
  });
  elements.emptyState.hidden = state.speakers.length !== 0;
  setText(elements.speakerCount, state.speakers.length);
  setText(elements.sampleCount, state.speakers.reduce((sum, item) => sum + number(item.sample_count ?? item.active_sample_count), 0));
}

async function loadAnalysis(reset = false) {
  if (reset) { state.analysis.offset = 0; state.analysis.selected.clear(); }
  elements.analysisLoading.hidden = false; elements.analysisEmpty.hidden = true;
  try {
    const params = { ...analysisFilters(), offset: state.analysis.offset, limit: state.analysis.limit, sort: "newest" };
    const result = await request(`api/analysis?${queryString(params)}`);
    state.analysis.items = Array.isArray(result) ? result : result.items || [];
    state.analysis.total = Array.isArray(result) ? result.length : number(result.total, state.analysis.items.length);
    renderAnalysis();
  } catch (error) { empty(elements.analysisGrid); elements.analysisEmpty.hidden = false; setText(elements.analysisEmpty.querySelector("p"), error.message); }
  finally { elements.analysisLoading.hidden = true; }
}

function outcomeLabel(outcome) { return ({ matched: "Herkend", unmatched: "Niet herkend", ambiguous: "Twijfelachtig", error: "Fout", blocked: "Geblokkeerd" })[outcome] || outcome || "Onbekend"; }
function recordingOutcome(item) { return item.outcome || (item.blocked ? "blocked" : item.error ? "error" : item.ambiguous ? "ambiguous" : item.matched ? "matched" : "unmatched"); }
function renderAnalysis() {
  empty(elements.analysisGrid);
  const selected = state.analysis.selected;
  state.analysis.items.forEach((item) => {
    const id = getId(item); const outcome = recordingOutcome(item); const card = document.createElement("article"); card.className = "analysis-card";
    const head = document.createElement("div"); head.className = "analysis-card-head";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = selected.has(id); checkbox.ariaLabel = `${item.transcript || "Opname"} selecteren`; checkbox.addEventListener("click", (event) => event.stopPropagation()); checkbox.addEventListener("change", () => { checkbox.checked ? selected.add(id) : selected.delete(id); updateSelection(); });
    const badge = appendText(head, "span", outcomeLabel(outcome), `outcome outcome-${outcome}`); badge.title = outcomeLabel(outcome); head.append(checkbox); card.append(head);
    const title = appendText(card, "strong", item.speaker_name || item.speaker?.name || item.person_name || "Onbekende speaker", "analysis-speaker");
    appendText(card, "p", item.transcript || "Geen transcript beschikbaar", "analysis-transcript");
    const facts = document.createElement("div"); facts.className = "analysis-facts"; appendText(facts, "span", formatDate(item.created_at || item.timestamp)); const duration = item.duration ?? item.duration_seconds; appendText(facts, "span", `${formatScore(item.confidence ?? item.score)}${duration != null ? ` · ${formatDuration(duration)}` : ""}`); card.append(facts);
    const source = appendText(card, "span", item.source === "test" ? "Test" : item.source || "Pipeline", "source-tag"); source.title = item.satellite_name || item.satellite || "";
    card.addEventListener("click", (event) => { if (event.target !== checkbox) openAnalysisDetail(id); }); elements.analysisGrid.append(card);
  });
  elements.analysisEmpty.hidden = state.analysis.items.length > 0;
  elements.analysisPagination.hidden = state.analysis.total <= state.analysis.limit;
  elements.analysisPrevious.disabled = state.analysis.offset === 0;
  elements.analysisNext.disabled = state.analysis.offset + state.analysis.limit >= state.analysis.total;
  const start = state.analysis.total ? state.analysis.offset + 1 : 0; const end = Math.min(state.analysis.offset + state.analysis.items.length, state.analysis.total);
  setText(elements.analysisPageLabel, `${start}–${end} van ${state.analysis.total}`); updateSelection();
}
function updateSelection() { const amount = state.analysis.selected.size; setText(elements.analysisSelectionCount, amount ? `${amount} geselecteerd` : "Geen selectie"); elements.deleteSelected.disabled = !amount; }

async function openAnalysisDetail(id) {
  elements.analysisDialog.showModal(); elements.analysisDetail.hidden = true; elements.analysisDetailLoading.hidden = false; state.analysis.detail = null;
  try {
    const detail = await request(`api/analysis/${encodeURIComponent(id)}`); state.analysis.detail = detail; renderAnalysisDetail(detail); elements.analysisDetail.hidden = false;
  } catch (error) { setText(elements.analysisDetailLoading, error.message); }
  finally { elements.analysisDetailLoading.hidden = Boolean(state.analysis.detail); }
}
function metadataList(node, pairs) { empty(node); pairs.filter(([, value]) => value !== undefined && value !== null && value !== "").forEach(([key, value]) => { const wrapper = document.createElement("div"); appendText(wrapper, "dt", key); appendText(wrapper, "dd", String(value)); node.append(wrapper); }); }
function audioEndpoint(id, variant) { return apiUrl(`api/analysis/${encodeURIComponent(id)}/audio?variant=${variant}`); }
function renderAnalysisDetail(item) {
  const id = getId(item); setText(elements.analysisDialogTitle, item.transcript ? item.transcript.slice(0, 90) : `Opname ${id}`);
  elements.analysisOriginalAudio.src = audioEndpoint(id, "original"); elements.analysisOriginalAudio.load();
  const variants = [
    ["denoised", elements.analysisDenoisedAudio, elements.denoisedUnavailable],
  ];
  variants.forEach(([variant, player, unavailable]) => {
    const available = Boolean(item[`${variant}_available`]);
    player.hidden = !available; unavailable.hidden = available;
    if (available) { player.src = audioEndpoint(id, variant); player.load(); }
    else { player.pause(); player.removeAttribute("src"); }
  });
  elements.downloadOriginal.href = audioEndpoint(id, "original"); elements.downloadOriginal.download = `${htmlSafeFilename(item.speaker_name || "recording")}-${id}.wav`;
  state.analysis.duration = number(item.duration ?? item.duration_seconds); state.analysis.trim = { start: 0, end: state.analysis.duration }; elements.trimStart.value = 0; elements.trimEnd.value = state.analysis.duration || ""; setText(elements.waveformRange, state.analysis.duration ? `0.0 – ${state.analysis.duration.toFixed(1)} sec` : "Duur laden…");
  metadataList(elements.recognitionMetadata, [["Uitkomst", outcomeLabel(recordingOutcome(item))], ["Speaker", item.speaker_name || item.speaker?.name || "Niet herkend"], ["Person", item.person_entity_id || item.person_entity || "—"], ["Confidence", formatScore(item.confidence ?? item.score)], ["Threshold", item.threshold != null ? formatScore(item.threshold) : "—"], ["Marge", (item.margin ?? item.min_margin) != null ? formatScore(item.margin ?? item.min_margin) : "—"], ["Beste segment", item.best_segment ? `${formatDuration(item.best_segment.start ?? item.best_segment.start_seconds)} – ${formatDuration(item.best_segment.end ?? item.best_segment.end_seconds)}` : "—"], ["Kandidaten", item.candidate_count], ["Alle scores", scoresText(item.scores || item.all_scores)]]);
  const timing = item.timings || {};
  const quality = item.processing_quality || {};
  const stages = item.processing_stages || {};
  const stageText = Object.entries(stages).map(([stage, value]) => `${stage}: ${typeof value === "object" ? value.status || JSON.stringify(value) : value}`).join(", ");
  metadataList(elements.pipelineMetadata, [["Bron", item.source || "pipeline"], ["Satelliet", item.satellite_name || item.satellite], ["STT-engine", item.stt_entity_id || item.stt_entity], ["Herkenning", timing.recognition_ms != null ? `${timing.recognition_ms} ms` : item.recognition_ms != null ? `${item.recognition_ms} ms` : "—"], ["Ruisonderdrukking (warm, vergelijkbaar)", timing.denoise_ms != null ? `${timing.denoise_ms} ms` : "Niet meegeteld"], ["Model laden (koude start)", timing.model_load_ms != null ? `${timing.model_load_ms} ms` : "—"], ["Koude modelrun", timing.cold_request_ms != null ? `${timing.cold_request_ms} ms (niet vergelijkbaar)` : timing.cold_start_ms != null ? `${timing.cold_start_ms} ms (niet vergelijkbaar)` : "—"], ["Extra audiobewerking", timing.audio_processing_ms != null ? `${timing.audio_processing_ms} ms` : "Niet meegeteld bij koude start"], ["STT-tijd", timing.stt_ms != null ? `${timing.stt_ms} ms` : "—"], ["Totale pipeline", timing.total_ms != null ? `${timing.total_ms} ms` : "—"], ["Modelstatus", quality.model_was_loaded === false ? "Koude start; timing uitgesloten" : stageText || "niet geladen"], ["Verwerking", item.processing_status || "niet gestart"], ["Fallbackreden", item.processing_fallback_reason], ["Duur behouden", quality.duration_preserved == null ? null : quality.duration_preserved ? "Ja" : "Nee"], ["RMS origineel", quality.original_rms], ["RMS ruisonderdrukt", quality.denoised_rms], ["Clipping", quality.clipping_ratio == null ? null : `${(quality.clipping_ratio * 100).toFixed(2)}%`], ["Stilte", quality.silence_ratio == null ? null : `${(quality.silence_ratio * 100).toFixed(1)}%`], ["Gebruikte audio", AUDIO_VARIANTS[item.audio_variant] || item.audio_variant], ["Geblokkeerd", item.blocked ? "Ja" : "Nee"], ["Conversation doorgestuurd", item.conversation_forwarded == null ? "—" : item.conversation_forwarded ? "Ja" : "Nee"]]);
  drawWaveformFromAudio();
}
function scoresText(scores) { if (!scores) return "—"; if (Array.isArray(scores)) return scores.map((entry) => `${entry.name || entry.speaker_name || entry.id}: ${formatScore(entry.score ?? entry.confidence)}`).join(", "); return Object.entries(scores).map(([name, score]) => `${name}: ${formatScore(score)}`).join(", "); }

async function drawWaveformFromAudio() {
  try {
    const response = await fetch(elements.analysisOriginalAudio.src); if (!response.ok) throw new Error("WAV niet beschikbaar");
    const context = new AudioContext(); const buffer = await context.decodeAudioData(await response.arrayBuffer()); await context.close();
    state.analysis.duration = buffer.duration; state.analysis.trim = { start: 0, end: buffer.duration }; elements.trimStart.value = 0; elements.trimEnd.value = buffer.duration.toFixed(1); drawWaveform(buffer.getChannelData(0));
  } catch (_) { drawWaveform(null); }
}
function drawWaveform(samples) {
  const canvas = elements.analysisWaveform; const ratio = Math.max(1, window.devicePixelRatio || 1); const width = Math.max(300, canvas.clientWidth || 640); const height = 120; canvas.width = width * ratio; canvas.height = height * ratio; canvas.style.height = `${height}px`; const context = canvas.getContext("2d"); context.scale(ratio, ratio); context.fillStyle = "#0b121f"; context.fillRect(0, 0, width, height);
  if (samples?.length) { context.strokeStyle = "#27d3a2"; context.lineWidth = 1; const stride = Math.max(1, Math.floor(samples.length / width)); context.beginPath(); for (let x = 0; x < width; x += 1) { let peak = 0; for (let offset = 0; offset < stride; offset += 1) peak = Math.max(peak, Math.abs(samples[x * stride + offset] || 0)); const y = peak * (height * .42); context.moveTo(x + .5, height / 2 - y); context.lineTo(x + .5, height / 2 + y); } context.stroke(); } else { context.fillStyle = "#96a2b6"; context.font = "12px system-ui"; context.fillText("Audiogolfvorm niet beschikbaar", 16, height / 2); }
  state.analysis.waveform = { width, height, samples }; drawTrimOverlay();
}
function drawTrimOverlay() {
  const waveform = state.analysis.waveform; if (!waveform) return; const { width, height } = waveform; const canvas = elements.analysisWaveform; const ratio = Math.max(1, window.devicePixelRatio || 1); const context = canvas.getContext("2d"); context.save(); context.scale(ratio, ratio); const duration = state.analysis.duration || 1; const left = width * state.analysis.trim.start / duration; const right = width * state.analysis.trim.end / duration; context.fillStyle = "rgba(2,5,12,.58)"; context.fillRect(0, 0, left, height); context.fillRect(right, 0, width - right, height); context.strokeStyle = "#f4f7fb"; context.lineWidth = 2; [left, right].forEach((x) => { context.beginPath(); context.moveTo(x, 0); context.lineTo(x, height); context.stroke(); context.fillStyle = "#f4f7fb"; context.fillRect(x - 4, 4, 8, 16); }); context.restore(); setText(elements.waveformRange, `${state.analysis.trim.start.toFixed(1)} – ${state.analysis.trim.end.toFixed(1)} sec`);
}
function setTrim(start, end) { const duration = state.analysis.duration || Math.max(start, end); start = Math.max(0, Math.min(number(start), duration)); end = Math.max(start + .1, Math.min(number(end, duration), duration)); state.analysis.trim = { start, end }; elements.trimStart.value = start.toFixed(1); elements.trimEnd.value = end.toFixed(1); drawTrimOverlay(); }
function handleWaveformPointer(event) { const waveform = state.analysis.waveform; if (!waveform || !state.analysis.duration) return; const rect = elements.analysisWaveform.getBoundingClientRect(); const seconds = Math.max(0, Math.min(state.analysis.duration, (event.clientX - rect.left) / rect.width * state.analysis.duration)); const startDistance = Math.abs(seconds - state.analysis.trim.start); const endDistance = Math.abs(seconds - state.analysis.trim.end); state.analysis.drag = startDistance < endDistance ? "start" : "end"; updateWaveformDrag(event); elements.analysisWaveform.setPointerCapture?.(event.pointerId); }
function updateWaveformDrag(event) { if (!state.analysis.drag) return; const rect = elements.analysisWaveform.getBoundingClientRect(); const seconds = Math.max(0, Math.min(state.analysis.duration, (event.clientX - rect.left) / rect.width * state.analysis.duration)); if (state.analysis.drag === "start") setTrim(Math.min(seconds, state.analysis.trim.end - .1), state.analysis.trim.end); else setTrim(state.analysis.trim.start, Math.max(seconds, state.analysis.trim.start + .1)); }

async function openProfile(speaker) {
  state.profile = speaker; setText(elements.profileDialogTitle, speaker.name); setText(elements.profileDialogDescription, "Permanente enrollment-WAV’s maken later hertrainen mogelijk. Inactieve samples tellen niet mee voor herkenning."); empty(elements.profileSamples); appendText(elements.profileSamples, "p", "Samples laden…", "field-help"); elements.profileDialog.showModal();
  try { const data = await request(`api/speakers/${encodeURIComponent(getId(speaker))}/samples`); renderProfileSamples(Array.isArray(data) ? data : data.items || []); } catch (error) { empty(elements.profileSamples); appendText(elements.profileSamples, "p", error.message, "error-box"); }
}
function renderProfileSamples(samples) {
  empty(elements.profileSamples);
  if (!samples.length) { appendText(elements.profileSamples, "p", "Voor dit legacyprofiel zijn nog geen permanente WAV-samples beschikbaar.", "field-help"); return; }
  samples.forEach((sample) => {
    const row = document.createElement("article"); row.className = "profile-sample"; const id = sample.id || sample.sample_id; appendText(row, "strong", sample.label || sample.filename || `Sample ${id}`); appendText(row, "span", `${formatDuration(sample.duration ?? sample.duration_seconds)} · ${sample.active === false ? "inactief" : "actief"}`, "field-help");
    const actions = document.createElement("div"); actions.className = "sample-actions"; const audio = document.createElement("audio"); audio.controls = true; audio.preload = "metadata"; audio.src = apiUrl(`api/speakers/${encodeURIComponent(getId(state.profile))}/samples/${encodeURIComponent(id)}/audio`); const download = document.createElement("a"); download.href = audio.src; download.download = htmlSafeFilename(sample.filename || `${state.profile.name}-${id}.wav`); download.className = "text-button"; download.textContent = "Download"; const toggle = document.createElement("button"); toggle.className = "secondary-button small"; toggle.type = "button"; toggle.textContent = sample.active === false ? "Activeer" : "Deactiveer"; toggle.addEventListener("click", async () => { await request(`api/speakers/${encodeURIComponent(getId(state.profile))}/samples/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ active: sample.active === false }) }); await openProfile(state.profile); await refreshOverview(); }); const remove = document.createElement("button"); remove.className = "danger-text text-button"; remove.type = "button"; remove.textContent = "Permanent verwijderen"; remove.addEventListener("click", () => confirmAction({ title: "Enrollment-sample verwijderen?", message: "Deze permanente WAV wordt definitief verwijderd en het profiel wordt opnieuw getraind.", requireCheck: true, label: "Ik wil deze permanente sample verwijderen", action: async () => { await request(`api/speakers/${encodeURIComponent(getId(state.profile))}/samples/${encodeURIComponent(id)}`, { method: "DELETE" }); await openProfile(state.profile); await refreshOverview(); } })); actions.append(audio, download, toggle, remove); row.append(actions); elements.profileSamples.append(row);
  });
}

function confirmAction({ title, message, label = "Ik begrijp dat deze actie niet ongedaan kan worden gemaakt", requireCheck = true, action, button = "Verwijderen" }) {
  state.confirmation = action; setText(elements.confirmTitle, title); setText(elements.confirmMessage, message); setText(elements.confirmCheckboxLabel, label); elements.confirmCheckbox.checked = false; elements.confirmCheckboxRow.hidden = !requireCheck; elements.confirmAction.disabled = requireCheck; setText(elements.confirmAction, button); elements.confirmDialog.showModal();
}
async function runConfirmation() { const action = state.confirmation; if (!action || (elements.confirmCheckboxRow.hidden === false && !elements.confirmCheckbox.checked)) return; elements.confirmAction.disabled = true; try { await action(); elements.confirmDialog.close(); showToast("Wijziging opgeslagen"); } catch (error) { showToast(error.message); elements.confirmAction.disabled = false; } }

function openPromoteDialog() {
  const item = state.analysis.detail; if (!item) return; elements.promoteError.hidden = true; elements.promoteName.value = ""; elements.promoteSpeaker.replaceChildren(...state.speakers.map((speaker) => new Option(speaker.name, getId(speaker)))); elements.promotePerson.replaceChildren(new Option("Niet gekoppeld", ""), ...state.persons.map((person) => new Option(person.name, person.entity_id))); updatePromoteMode(); elements.promoteDialog.showModal();
}
function updatePromoteMode() { const newTarget = document.querySelector('input[name="promote-target"]:checked').value === "new"; elements.promoteExistingField.hidden = newTarget; elements.promoteNewFields.hidden = !newTarget; }
async function promoteAudio() {
  const item = state.analysis.detail; if (!item) return; const newTarget = document.querySelector('input[name="promote-target"]:checked').value === "new"; const payload = { start_seconds: state.analysis.trim.start, end_seconds: state.analysis.trim.end, speaker_id: newTarget ? null : elements.promoteSpeaker.value, new_speaker_name: newTarget ? elements.promoteName.value.trim() : null, person_entity_id: newTarget ? elements.promotePerson.value || null : null };
  if ((newTarget && !payload.new_speaker_name) || (!newTarget && !payload.speaker_id)) { setText(elements.promoteError, "Kies een bestaand profiel of vul een nieuwe naam in."); elements.promoteError.hidden = false; return; }
  elements.confirmPromote.disabled = true; try { await request(`api/analysis/${encodeURIComponent(getId(item))}/promote`, { method: "POST", body: JSON.stringify(payload) }); elements.promoteDialog.close(); showToast("Audio permanent toegevoegd en profiel opnieuw getraind"); await refreshOverview(); } catch (error) { setText(elements.promoteError, error.message); elements.promoteError.hidden = false; } finally { elements.confirmPromote.disabled = false; }
}
async function extractAudio() { const item = state.analysis.detail; if (!item) return; elements.extractAudio.disabled = true; setText(elements.extractAudio, "Ruisonderdrukking starten…"); try { await request(`api/analysis/${encodeURIComponent(getId(item))}/process`, { method: "POST", body: JSON.stringify({}) }); showToast("Ruisonderdrukking gestart"); await pollProcessing(getId(item)); } catch (error) { showToast(error.message); } finally { elements.extractAudio.disabled = false; setText(elements.extractAudio, "Ruis onderdrukken"); } }
async function pollProcessing(id) { for (let attempt = 0; attempt < 225; attempt += 1) { await new Promise((resolve) => setTimeout(resolve, 800)); const detail = await request(`api/analysis/${encodeURIComponent(id)}`); state.analysis.detail = detail; renderAnalysisDetail(detail); if (!["queued", "running"].includes(detail.processing_status)) { if (detail.processing_status === "complete" && detail.denoised_available) showToast("Ruisonderdrukking afgerond"); else showToast(detail.processing_fallback_reason || "Ruisonderdrukking viel terug op het origineel"); return; } } showToast("Verwerking loopt nog; open dit detail later opnieuw."); }

async function deleteAnalysis(ids = null, filtered = false) {
  const count = filtered ? state.analysis.total : ids?.length || 0; if (!count) { showToast("Er zijn geen opnamen om te verwijderen."); return; }
  confirmAction({ title: filtered ? "Alle gefilterde opnamen verwijderen?" : "Opnamen verwijderen?", message: `${count} analyse-opname${count === 1 ? "" : "n"} wordt definitief verwijderd. Permanente enrollment-WAV’s blijven behouden.`, requireCheck: true, label: "Ik wil deze analyse-audio definitief verwijderen", action: async () => { const endpoint = ids?.length === 1 && !filtered ? `api/analysis/${encodeURIComponent(ids[0])}` : "api/analysis/delete"; const options = ids?.length === 1 && !filtered ? { method: "DELETE" } : { method: "POST", body: JSON.stringify(filtered ? { ids: null, filters: analysisFilters(), all_filtered: true } : { ids }) }; await request(endpoint, options); closeAnalysis(); state.analysis.selected.clear(); await loadAnalysis(true); await refreshOverview(); } }); }

async function loadCalibration() {
  elements.calibrationLoading.hidden = false; elements.calibrationContent.hidden = true;
  try { state.calibration = await request("api/calibration"); renderCalibration(state.calibration); elements.calibrationContent.hidden = false; } catch (error) { setText(elements.calibrationLoading, error.message); }
  finally { elements.calibrationLoading.hidden = Boolean(state.calibration); }
}
function renderCalibration(data) {
  const preview = data.preview || data; const genuine = preview.genuine_count ?? preview.genuine_samples?.length ?? 0; const impostor = preview.impostor_count ?? preview.impostor_samples?.length ?? 0; const recommendation = preview.recommendation || preview.recommended || preview;
  const applied = data.applied || {}; setText(elements.genuineCount, genuine); setText(elements.impostorCount, impostor); setText(elements.currentThreshold, applied.threshold != null ? `${formatScore(applied.threshold)} / ${formatScore(applied.margin)}` : data.current_threshold != null ? `${formatScore(data.current_threshold)} / ${formatScore(data.current_margin)}` : "Standaard");
  const enough = genuine >= 3 && impostor >= 3; elements.calibrationWarning.hidden = enough; if (!enough) setText(elements.calibrationWarning, `Nog ${Math.max(0, 3 - genuine)} genuine en ${Math.max(0, 3 - impostor)} impostor-waarneming(en) nodig voordat je een advies kunt toepassen.`);
  setText(elements.recommendedTitle, enough && preview.ready !== false ? "Veilige aanbeveling" : "Nog onvoldoende gegevens"); setText(elements.recommendedThreshold, recommendation.threshold != null ? formatScore(recommendation.threshold) : "—"); setText(elements.recommendedMargin, recommendation.margin != null ? formatScore(recommendation.margin) : "—"); setText(elements.falseAccepts, recommendation.false_accepts ?? recommendation.false_accept_count ?? "—"); setText(elements.falseRejects, recommendation.false_rejects ?? recommendation.false_reject_count ?? "—"); setText(elements.calibrationDetail, recommendation.summary || "False accepts wegen viermaal zwaarder dan false rejects."); elements.applyCalibration.disabled = !enough || preview.ready === false || recommendation.threshold == null; drawCalibration(preview.genuine_scores || preview.genuine || [], preview.impostor_scores || preview.impostor || []);
}
function drawCalibration(genuine, impostor) {
  const canvas = elements.calibrationChart; const ratio = Math.max(1, window.devicePixelRatio || 1); const width = Math.max(320, canvas.clientWidth || 600); const height = 220; canvas.width = width * ratio; canvas.height = height * ratio; const context = canvas.getContext("2d"); context.scale(ratio, ratio); context.fillStyle = "#0b121f"; context.fillRect(0, 0, width, height); const bins = 10; const counts = (values) => { const result = Array(bins).fill(0); values.forEach((value) => { const score = typeof value === "object" ? value.score : value; result[Math.min(bins - 1, Math.max(0, Math.floor(number(score) * bins)))] += 1; }); return result; }; const own = counts(genuine); const other = counts(impostor); const max = Math.max(1, ...own, ...other); const step = width / bins; for (let index = 0; index < bins; index += 1) { const ownHeight = own[index] / max * (height - 36); const otherHeight = other[index] / max * (height - 36); context.fillStyle = "#27d3a2"; context.fillRect(index * step + 4, height - 20 - ownHeight, step * .42 - 5, ownHeight); context.fillStyle = "#ff8da1"; context.fillRect(index * step + step * .5, height - 20 - otherHeight, step * .42 - 5, otherHeight); context.fillStyle = "#96a2b6"; context.font = "10px system-ui"; context.fillText((index / bins).toFixed(1), index * step + 2, height - 5); } }
async function applyCalibration() { const preview = state.calibration?.preview || state.calibration || {}; const recommended = preview.recommendation || preview.recommended || preview; if (recommended.threshold == null) return; elements.applyCalibration.disabled = true; try { await request("api/calibration", { method: "POST", body: JSON.stringify({ threshold: recommended.threshold, margin: recommended.margin }) }); showToast("Kalibratie toegepast"); await loadCalibration(); } catch (error) { showToast(error.message); } }
function resetCalibration() { confirmAction({ title: "Kalibratie resetten?", message: "De expliciete drempel en marge worden verwijderd. De standaardwaarden uit de add-onconfiguratie worden weer gebruikt.", requireCheck: false, button: "Resetten", action: async () => { await request("api/calibration", { method: "DELETE" }); await loadCalibration(); } }); }

async function openEnroll() { revokePreviewUrls(); state.samples = []; elements.enrollForm.reset(); elements.formError.hidden = true; renderSamples(); elements.dialog.showModal(); elements.speakerName.focus(); await Promise.all([loadSatellites(), loadPersons(), loadSpeechExamples()]); }
async function openTest() { revokePreviewUrls(); state.testSample = null; elements.testForm.reset(); elements.testFormError.hidden = true; renderTestSample(); elements.testDialog.showModal(); await loadTestSatellites(); }
async function loadSpeechExamples() { if (!state.speechExamples.length) { try { state.speechExamples = await request("assets/speech-prompts.json"); } catch (_) { return; } } selectSpeechExample(); }
function selectSpeechExample() { if (!state.speechExamples.length) return; let next = Math.floor(Math.random() * state.speechExamples.length); if (next === state.speechExampleIndex && state.speechExamples.length > 1) next = (next + 1) % state.speechExamples.length; state.speechExampleIndex = next; setText(elements.speechExample, state.speechExamples[next]); }
async function loadPersons() { elements.speakerPerson.replaceChildren(new Option("Personen laden…", "")); try { state.persons = await request("api/home-assistant-persons"); elements.speakerPerson.replaceChildren(new Option("Niet gekoppeld", ""), ...state.persons.map((person) => new Option(person.name, person.entity_id))); } catch (error) { elements.speakerPerson.replaceChildren(new Option("Personen niet bereikbaar", "")); showToast(error.message); } }
async function loadSatellites() { await loadSatellitesInto(elements.satellite, elements.voiceRecord, setFormError); }
async function loadTestSatellites() { await loadSatellitesInto(elements.testSatellite, elements.testVoiceRecord, setTestError); }
async function loadSatellitesInto(select, button, onError) { select.replaceChildren(new Option("Voice-apparaten laden…", "")); button.disabled = true; try { state.satellites = await request("api/assist-satellites"); const available = state.satellites.filter((item) => item.state === "idle"); select.replaceChildren(new Option(available.length ? "Kies een Voice-apparaat" : "Geen beschikbaar Voice-apparaat", ""), ...available.map((item) => new Option(item.name, item.entity_id))); } catch (error) { select.replaceChildren(new Option("Voice-apparaten niet bereikbaar", "")); onError(error.message); } }
async function captureFromSatellite(mode = "enroll") { const testing = mode === "test"; const select = testing ? elements.testSatellite : elements.satellite; const button = testing ? elements.testVoiceRecord : elements.voiceRecord; const dialog = testing ? elements.testDialog : elements.dialog; const entityId = select.value; if (!entityId) return; let sessionId; (testing ? elements.testFormError : elements.formError).hidden = true; button.disabled = true; setText(button, "Luisteren…"); try { let session = await request("api/satellite-enrollment", { method: "POST", body: JSON.stringify({ satellite_entity_id: entityId, start_mode: "remote" }) }); sessionId = session.id; state.satelliteSession = sessionId; while (["armed", "capturing"].includes(session.status)) { await new Promise((resolve) => setTimeout(resolve, 600)); if (state.satelliteSession !== session.id) return; session = await request(`api/satellite-enrollment/${session.id}`); if (session.status === "capturing") setText(button, "Opname ontvangen…"); } if (session.status !== "complete" || !session.audio) throw new Error(session.error || "Geen stemfragment ontvangen. Controleer of de Assist-pipeline Speaker Recognition STT gebruikt."); const sample = { ...session.audio, duration: atob(session.audio.audio_data).length / 2 / session.audio.sample_rate, label: state.satellites.find((item) => item.entity_id === entityId)?.name || "Home Assistant Voice", source: "voice" }; if (testing) { state.testSample = sample; renderTestSample(); } else { state.samples.push(sample); renderSamples(); selectSpeechExample(); } showToast("Voice-fragment ontvangen"); } catch (error) { if (dialog.open) (testing ? setTestError : setFormError)(error.message); } finally { if (state.satelliteSession === sessionId) state.satelliteSession = null; setText(button, "Opnemen via Voice"); button.disabled = !select.value; } }
function revokePreviewUrls() { state.previewUrls.forEach((url) => URL.revokeObjectURL(url)); state.previewUrls = []; }
function samplePreviewUrl(sample) { const binary = atob(sample.audio_data); const pcm = new Uint8Array(binary.length); for (let index = 0; index < binary.length; index += 1) pcm[index] = binary.charCodeAt(index); const wav = new ArrayBuffer(44 + pcm.length); const view = new DataView(wav); const write = (offset, value) => [...value].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0))); write(0, "RIFF"); view.setUint32(4, 36 + pcm.length, true); write(8, "WAVE"); write(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, sample.sample_rate, true); view.setUint32(28, sample.sample_rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, "data"); view.setUint32(40, pcm.length, true); new Uint8Array(wav, 44).set(pcm); return URL.createObjectURL(new Blob([wav], { type: "audio/wav" })); }
function sampleRow(sample, removeHandler) { const row = document.createElement("div"); row.className = "sample-row"; appendText(row, "span", sample.source === "microfoon" ? "●" : sample.source === "voice" ? "◉" : "♪"); appendText(row, "span", `${sample.label} · ${formatDuration(sample.duration)}`); const preview = document.createElement("audio"); preview.controls = true; preview.preload = "metadata"; preview.ariaLabel = `${sample.label} afspelen`; preview.src = samplePreviewUrl(sample); state.previewUrls.push(preview.src); const remove = document.createElement("button"); remove.type = "button"; remove.ariaLabel = "Sample verwijderen"; remove.textContent = "×"; remove.addEventListener("click", removeHandler); row.append(preview, remove); return row; }
function renderSamples() { revokePreviewUrls(); empty(elements.samples); state.samples.forEach((sample, index) => elements.samples.append(sampleRow(sample, () => { state.samples.splice(index, 1); renderSamples(); }))); updateSaveState(); }
function renderTestSample() { revokePreviewUrls(); empty(elements.testSample); if (state.testSample) elements.testSample.append(sampleRow(state.testSample, () => { state.testSample = null; renderTestSample(); })); elements.testRecognize.disabled = !state.testSample; }
function updateSaveState() { elements.save.disabled = state.samples.length === 0 || !elements.speakerName.value.trim(); }
async function decodeFile(file) { if (file.size > 30 * 1024 * 1024) throw new Error(`${file.name} is groter dan 30 MB`); const context = new AudioContext(); try { const buffer = await context.decodeAudioData(await file.arrayBuffer()); return makeSample(buffer.getChannelData(0), buffer.sampleRate, file.name, "upload"); } catch (_) { throw new Error(`${file.name} kan door deze browser niet worden gelezen`); } finally { await context.close(); } }
function makeSample(floatSamples, sourceRate, label, source) { const targetRate = 16000; const duration = floatSamples.length / sourceRate; if (duration < 1) throw new Error("Een stemfragment moet minimaal 1 seconde lang zijn"); if (duration > 120) throw new Error("Een stemfragment mag maximaal 120 seconden duren"); const pcm = new Int16Array(Math.round(floatSamples.length * targetRate / sourceRate)); for (let index = 0; index < pcm.length; index += 1) { const position = index * sourceRate / targetRate; const left = Math.floor(position); const right = Math.min(left + 1, floatSamples.length - 1); const fraction = position - left; const value = Math.max(-1, Math.min(1, floatSamples[left] * (1 - fraction) + floatSamples[right] * fraction)); pcm[index] = value < 0 ? value * 32768 : value * 32767; } const bytes = new Uint8Array(pcm.buffer); let binary = ""; for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000)); return { audio_data: btoa(binary), sample_rate: targetRate, duration, label, source }; }
async function toggleRecording(mode = "enroll") { if (state.recording) { stopRecording(); return; } const testing = mode === "test"; const button = testing ? elements.testRecord : elements.record; if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext) { (testing ? setTestError : setFormError)("Microfoonopname is hier niet beschikbaar. Open Home Assistant via HTTPS of upload een audiofragment."); return; } try { const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false }); const context = new AudioContext(); const source = context.createMediaStreamSource(stream); const processor = context.createScriptProcessor(4096, 1, 1); const chunks = []; processor.onaudioprocess = (event) => chunks.push(new Float32Array(event.inputBuffer.getChannelData(0))); source.connect(processor); processor.connect(context.destination); state.recording = { stream, context, source, processor, chunks, mode }; button.classList.add("recording"); setText(button.querySelector("span"), "Opname stoppen"); } catch (_) { (testing ? setTestError : setFormError)("Geen toegang tot de microfoon. Controleer de browsertoestemming of gebruik upload."); } }
function discardRecording() { const recording = state.recording; if (!recording) return; const button = recording.mode === "test" ? elements.testRecord : elements.record; recording.processor.disconnect(); recording.source.disconnect(); recording.stream.getTracks().forEach((track) => track.stop()); recording.context.close(); state.recording = null; button.classList.remove("recording"); setText(button.querySelector("span"), "Opnemen"); }
function stopRecording() { const recording = state.recording; if (!recording) return; const testing = recording.mode === "test"; const button = testing ? elements.testRecord : elements.record; recording.processor.disconnect(); recording.source.disconnect(); recording.stream.getTracks().forEach((track) => track.stop()); const length = recording.chunks.reduce((sum, chunk) => sum + chunk.length, 0); const merged = new Float32Array(length); let offset = 0; recording.chunks.forEach((chunk) => { merged.set(chunk, offset); offset += chunk.length; }); const rate = recording.context.sampleRate; recording.context.close(); state.recording = null; button.classList.remove("recording"); setText(button.querySelector("span"), "Opnemen"); try { const sample = makeSample(merged, rate, "Microfoonopname", "microfoon"); if (testing) { state.testSample = sample; renderTestSample(); } else { state.samples.push(sample); renderSamples(); selectSpeechExample(); } } catch (error) { (testing ? setTestError : setFormError)(error.message); } }
function closeEnroll() { if (state.recording) discardRecording(); cancelSatelliteSession(); revokePreviewUrls(); state.samples = []; if (elements.dialog.open) elements.dialog.close("cancel"); }
function closeTest() { if (state.recording?.mode === "test") discardRecording(); cancelSatelliteSession(); revokePreviewUrls(); state.testSample = null; if (elements.testDialog.open) elements.testDialog.close("cancel"); }
function cancelSatelliteSession() { const id = state.satelliteSession; state.satelliteSession = null; if (id) request(`api/satellite-enrollment/${id}`, { method: "DELETE" }).catch(() => {}); }
async function saveSpeaker() { const name = elements.speakerName.value.trim(); if (!name || !state.samples.length) return; elements.save.disabled = true; setText(elements.save, "Embedding maken…"); elements.formError.hidden = true; try { await request("api/enroll", { method: "POST", body: JSON.stringify({ speaker_name: name, person_entity_id: elements.speakerPerson.value || null, replace: document.querySelector("#replace-profile").checked, samples: state.samples.map((sample) => ({ audio: { audio_data: sample.audio_data, sample_rate: sample.sample_rate } })) }) }); elements.dialog.close(); showToast(`${name} is succesvol enrolled`); await refreshOverview(); } catch (error) { setFormError(error.message); } finally { setText(elements.save, "Profiel opslaan"); updateSaveState(); } }
async function testAudio() { if (!state.testSample) return; elements.testResult.hidden = false; elements.testResult.classList.remove("no-match"); setText(elements.testResult, "Analyseren…"); elements.testRecognize.disabled = true; setText(elements.testRecognize, "Analyseren…"); try { const sample = state.testSample; const result = await request("api/analyze", { method: "POST", body: JSON.stringify({ audio: { audio_data: sample.audio_data, sample_rate: sample.sample_rate }, source: "test" }) }); elements.testResult.classList.toggle("no-match", !result.matched); setText(elements.testResult, result.matched ? `${result.speaker?.name || result.speaker_name} · ${formatScore(result.confidence)}` : `Onbekende speaker · ${formatScore(result.confidence)}`); closeTest(); } catch (error) { elements.testResult.classList.add("no-match"); setText(elements.testResult, error.message); } finally { setText(elements.testRecognize, "Fragment testen"); elements.testRecognize.disabled = !state.testSample; } }
function closeAnalysis() { if (elements.analysisDialog.open) elements.analysisDialog.close(); [elements.analysisOriginalAudio, elements.analysisDenoisedAudio].forEach((player) => { player.pause(); player.removeAttribute("src"); }); state.analysis.detail = null; state.analysis.waveform = null; }
function closeDialog(dialog) { if (dialog.open) dialog.close("cancel"); }

document.querySelector("#open-enroll").addEventListener("click", openEnroll); document.querySelector('[data-action="enroll"]').addEventListener("click", openEnroll); document.querySelector("#open-test").addEventListener("click", openTest); elements.speakerName.addEventListener("input", updateSaveState); elements.search.addEventListener("input", renderSpeakers); elements.record.addEventListener("click", () => toggleRecording("enroll")); elements.testRecord.addEventListener("click", () => toggleRecording("test")); elements.satellite.addEventListener("change", () => { elements.voiceRecord.disabled = !elements.satellite.value; }); elements.testSatellite.addEventListener("change", () => { elements.testVoiceRecord.disabled = !elements.testSatellite.value; }); elements.voiceRecord.addEventListener("click", () => captureFromSatellite("enroll")); elements.testVoiceRecord.addEventListener("click", () => captureFromSatellite("test")); elements.newSpeechExample.addEventListener("click", selectSpeechExample); elements.save.addEventListener("click", saveSpeaker); elements.testRecognize.addEventListener("click", testAudio);
elements.audioFiles.addEventListener("change", async () => { elements.formError.hidden = true; for (const file of elements.audioFiles.files) { try { state.samples.push(await decodeFile(file)); } catch (error) { setFormError(error.message); } } elements.audioFiles.value = ""; renderSamples(); }); elements.testFile.addEventListener("change", async () => { elements.testFormError.hidden = true; if (elements.testFile.files[0]) { try { state.testSample = await decodeFile(elements.testFile.files[0]); renderTestSample(); } catch (error) { setTestError(error.message); } } elements.testFile.value = ""; });
document.querySelectorAll('[data-action="close-enroll"]').forEach((button) => button.addEventListener("click", closeEnroll)); document.querySelectorAll('[data-action="close-test"]').forEach((button) => button.addEventListener("click", closeTest)); document.querySelectorAll('[data-action="close-profile"]').forEach((button) => button.addEventListener("click", () => closeDialog(elements.profileDialog))); document.querySelectorAll('[data-action="close-analysis"]').forEach((button) => button.addEventListener("click", closeAnalysis)); document.querySelectorAll('[data-action="close-promote"]').forEach((button) => button.addEventListener("click", () => closeDialog(elements.promoteDialog))); document.querySelectorAll('[data-action="close-confirm"]').forEach((button) => button.addEventListener("click", () => closeDialog(elements.confirmDialog)));
elements.dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeEnroll(); }); elements.testDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeTest(); }); elements.analysisDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeAnalysis(); }); [elements.profileDialog, elements.promoteDialog, elements.confirmDialog].forEach((dialog) => dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeDialog(dialog); }));
elements.profileDelete.addEventListener("click", () => { if (!state.profile) return; setText(elements.profileDeleteTitle, `“${state.profile.name}” verwijderen`); elements.profileDeleteDialog.showModal(); });
document.querySelectorAll('[data-action="close-profile-delete"]').forEach((button) => button.addEventListener("click", () => closeDialog(elements.profileDeleteDialog)));
async function deleteProfile(audioAction) { if (!state.profile) return; const profile = state.profile; const destructive = audioAction === "delete"; if (destructive) { closeDialog(elements.profileDeleteDialog); confirmAction({ title: `Profiel en audio van “${profile.name}” verwijderen?`, message: "Alle permanente enrollment-WAV’s van dit profiel worden definitief gewist. Dit kan niet ongedaan worden gemaakt.", requireCheck: true, label: "Ik wil het profiel en alle permanente audio definitief verwijderen", action: async () => { await request(`api/speakers/${encodeURIComponent(getId(profile))}`, { method: "DELETE", body: JSON.stringify({ audio_action: "delete" }) }); closeDialog(elements.profileDialog); await refreshOverview(); } }); return; } try { await request(`api/speakers/${encodeURIComponent(getId(profile))}`, { method: "DELETE", body: JSON.stringify({ audio_action: "archive" }) }); closeDialog(elements.profileDeleteDialog); closeDialog(elements.profileDialog); await refreshOverview(); showToast("Profiel verwijderd; audio is gearchiveerd"); } catch (error) { showToast(error.message); } }
elements.archiveProfileAudio.addEventListener("click", () => deleteProfile("archive")); elements.deleteProfileAudio.addEventListener("click", () => deleteProfile("delete"));
elements.confirmCheckbox.addEventListener("change", () => { elements.confirmAction.disabled = !elements.confirmCheckbox.checked; }); elements.confirmAction.addEventListener("click", runConfirmation); elements.deleteSelected.addEventListener("click", () => deleteAnalysis([...state.analysis.selected])); elements.deleteFiltered.addEventListener("click", () => deleteAnalysis(null, true)); elements.refreshAnalysis.addEventListener("click", () => loadAnalysis(true)); elements.analysisPrevious.addEventListener("click", () => { state.analysis.offset = Math.max(0, state.analysis.offset - state.analysis.limit); loadAnalysis(); }); elements.analysisNext.addEventListener("click", () => { state.analysis.offset += state.analysis.limit; loadAnalysis(); }); [elements.analysisPeriod, elements.analysisOutcome, elements.analysisSource, elements.analysisSpeaker].forEach((control) => control.addEventListener("change", () => loadAnalysis(true))); elements.analysisQuery.addEventListener("input", debounce(() => loadAnalysis(true)));
elements.analysisWaveform.addEventListener("pointerdown", handleWaveformPointer); elements.analysisWaveform.addEventListener("pointermove", updateWaveformDrag); ["pointerup", "pointercancel", "pointerleave"].forEach((event) => elements.analysisWaveform.addEventListener(event, () => { state.analysis.drag = null; })); elements.trimStart.addEventListener("change", () => setTrim(elements.trimStart.value, state.analysis.trim.end)); elements.trimEnd.addEventListener("change", () => setTrim(state.analysis.trim.start, elements.trimEnd.value)); elements.extractAudio.addEventListener("click", extractAudio); elements.promoteAudio.addEventListener("click", openPromoteDialog); elements.confirmPromote.addEventListener("click", promoteAudio); elements.deleteAnalysis.addEventListener("click", () => { if (state.analysis.detail) deleteAnalysis([getId(state.analysis.detail)]); }); document.querySelectorAll('input[name="promote-target"]').forEach((input) => input.addEventListener("change", updatePromoteMode));
elements.refreshCalibration.addEventListener("click", loadCalibration); elements.applyCalibration.addEventListener("click", applyCalibration); elements.resetCalibration.addEventListener("click", resetCalibration); elements.savePolicy.addEventListener("click", savePolicy); window.addEventListener("hashchange", () => setRoute(routeFromHash())); window.addEventListener("resize", debounce(() => { if (state.analysis.waveform) drawWaveform(state.analysis.waveform.samples); if (state.calibration) { const preview = state.calibration.preview || state.calibration; drawCalibration(preview.genuine_scores || preview.genuine || [], preview.impostor_scores || preview.impostor || []); } }, 120));

if (!location.hash) history.replaceState(null, "", "#profiles"); setRoute(routeFromHash()); refreshOverview();
