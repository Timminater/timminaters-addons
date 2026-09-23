(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = { settings: null, entities: [], dashboard: null, date: '', timer: null, promptedConfig: false };
  const DEFAULT_TARIFF = 'sensor.zonneplan_current_quarter_hourly_electricity_tariff';
  const TZ = 'Europe/Amsterdam';
  const statusNames = { known: 'Bekend', predicted: 'Voorspeld', missing: 'Ontbreekt' };
  const statusClasses = { known: 'status-known', predicted: 'status-predicted', missing: 'status-missing' };
  const fmtDate = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short' });
  const fmtTime = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' });
  const fmtDateTime = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  const fmtNumber = new Intl.NumberFormat('nl-NL', { minimumFractionDigits: 3, maximumFractionDigits: 5 });
  function setText(id, value, fallback = '—') { const e = $(id); if (e) e.textContent = value == null || value === '' ? fallback : String(value); }
  function show(id, visible) { const e = $(id); if (e) e.hidden = !visible; }
  function safeDate(value) { const d = new Date(value); return Number.isNaN(d.getTime()) ? null : d; }
  function dateKey(d) { return new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d); }
  function fmtPrice(value, unit) { if (value == null || value === '') return '—'; const n = Number(value); return Number.isFinite(n) ? `${fmtNumber.format(n)} ${unit || ''}`.trim() : '—'; }
  function humanTime(value, withDate = false) { const d = safeDate(value); return d ? (withDate ? fmtDateTime : fmtTime).format(d) : '—'; }
  function setConnection(online, text) { const e = $('connection-status'); e.classList.toggle('offline', !online); e.innerHTML = `<i></i> ${online ? 'Lokaal verbonden' : 'Niet verbonden'}`; if (text) e.title = text; }
  async function api(path, options = {}) {
    const response = await fetch(new URL(path.replace(/^\//, ''), document.baseURI), { cache: 'no-store', credentials: 'same-origin', ...options, headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) } });
    const type = response.headers.get('content-type') || '';
    if (!response.ok) { let message = `Aanvraag mislukt (${response.status})`; if (type.includes('json')) { const body = await response.json().catch(() => ({})); message = body.error || body.message || message; } throw new Error(message); }
    if (!type.includes('json')) throw new Error('De lokale app gaf geen JSON-antwoord.');
    return response.json();
  }
  function setupDates() {
    const select = $('day-select'); select.replaceChildren();
    const now = new Date();
    for (let i = 0; i < 7; i++) { const d = new Date(now.getTime() + i * 86400000); const key = dateKey(d); const option = document.createElement('option'); option.value = key; option.textContent = i === 0 ? `Vandaag · ${fmtDate.format(d)}` : fmtDate.format(d); select.append(option); }
    state.date = select.value; select.addEventListener('change', () => { state.date = select.value; loadDashboard(); });
  }
  function reportErrors(errors = []) {
    const relevant = Array.isArray(errors) ? errors : [];
    if (relevant.length) { $('notice').textContent = relevant.map(e => e.message || e.source || 'Bronfout').join(' · '); show('notice', true); }
    else show('notice', false);
  }
  function metricText(metrics) {
    if (!metrics || !metrics.n) return '';
    const unit = state.settings?.tariff_unit || '';
    return `Gemeten op ${metrics.n} kwartieren: MAE ${fmtPrice(metrics.mae, unit)}, bias ${fmtPrice(metrics.bias, unit)}, kwartierbasislijn MAE ${fmtPrice(metrics.baseline_mae, unit)}. Banddekking: niet meetbaar (geen gekalibreerde kwartierband).`;
  }
  function updateStatus(data) {
    const configured = !!data.configured;
    setConnection(true);
    reportErrors(data.errors);
    const q = data.quality || {};
    const label = q.label || 'Voorlopig';
    setText('quality-tag', label); $('quality-tag').className = `tag ${/goed|volwassen|ready/i.test(label) ? 'tag-good' : 'tag-provisional'}`;
    setText('quality-title', /voorlopig/i.test(label) ? 'Opstartfase' : `Kwaliteit: ${label}`);
    setText('quality-reason', [(q.reasons || []).join(' · ') || (data.stale ? 'Brongegevens zijn verouderd.' : 'De prognose is adviserend.'), metricText(q.maturity?.metrics)].filter(Boolean).join(' '));
    const archive = data.archive || {};
    setText('coverage', archive.coverage_days != null ? `${fmtNumber.format(Number(archive.coverage_days))} dagen · ${archive.quarter_count ?? 0} kwartieren` : 'Nog geen kwartierarchief');
    setText('model-version', data.model_version || '—');
    setText('price-update', data.last_price_update ? `Bijgewerkt ${humanTime(data.last_price_update, true)}` : 'Nog geen update');
    setText('weather-update', data.last_weather_update ? `Bijgewerkt ${humanTime(data.last_weather_update, true)}` : 'Nog geen update');
    setText('model-update', data.last_model_update ? `Berekend ${humanTime(data.last_model_update, true)}` : 'Nog geen update');
    setText('price-source-state', data.stale ? 'Verouderd' : (data.last_price_update ? 'Actueel' : 'Wacht'));
    setText('weather-source-state', data.last_weather_update ? 'Beschikbaar' : 'Wacht');
    setText('model-source-state', data.last_model_update ? 'Gereed' : 'Wacht');
    setText('detail-price-source', data.sources?.tariff_entity || state.settings?.tariff_entity || 'Niet ingesteld');
    setText('detail-weather-source', data.sources?.weather_source || state.settings?.weather_source || 'Niet ingesteld');
    setText('detail-model-update', data.last_model_update ? humanTime(data.last_model_update, true) : 'Nog geen berekening');
    setText('missing-inputs', (q.missing_inputs || []).join(', ') || 'Geen gemeld');
    setText('provisional-detail', (q.reasons || []).join(' · ') || `Archiefdekking: ${archive.coverage_days ?? 0} dagen. De kwaliteit wordt lokaal opgebouwd.`);
    if (!configured) {
      if (!state.promptedConfig) { state.promptedConfig = true; void openSettings(); }
      show('notice', true); $('notice').textContent = 'Kies eerst een tariefbron om de prijsprognose te starten.';
    }
  }
  function normalizeSlots(input) {
    if (!Array.isArray(input)) return [];
    return input.map((s) => ({ ...s, startDate: safeDate(s.start), endDate: safeDate(s.end), value: s.price == null ? NaN : Number(s.price), status: ['known', 'predicted', 'missing'].includes(s.status) ? s.status : (s.price == null ? 'missing' : 'known') }))
      .filter(s => s.startDate && s.endDate && s.endDate > s.startDate).sort((a, b) => a.startDate - b.startDate);
  }
  function fillGaps(slots) {
    const result = [];
    for (let i = 0; i < slots.length; i++) {
      const current = slots[i]; result.push(current);
      const next = slots[i + 1]; if (!next) continue;
      for (let t = current.endDate.getTime(); t + 1 < next.startDate.getTime(); t += 900000) {
        const end = Math.min(t + 900000, next.startDate.getTime());
        if (end > t) result.push({ startDate: new Date(t), endDate: new Date(end), status: 'missing', value: NaN, source: 'Geen gegevens' });
        if (result.length > 1100) break;
      }
    }
    return result;
  }
  function renderChart(slots) {
    const svg = $('price-chart'); svg.replaceChildren();
    const data = fillGaps(slots);
    show('chart-empty', !data.length); svg.hidden = !data.length;
    if (!data.length) return;
    const width = Math.max(700, svg.parentElement.clientWidth || 700), height = 255;
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const pad = { l: 52, r: 15, t: 15, b: 35 }, plotW = width - pad.l - pad.r, plotH = height - pad.t - pad.b;
    const prices = data.filter(s => Number.isFinite(s.value)).map(s => s.value);
    let min = prices.length ? Math.min(...prices) : 0, max = prices.length ? Math.max(...prices) : 1;
    if (min === max) { min -= Math.max(.1, Math.abs(min) * .1); max += Math.max(.1, Math.abs(max) * .1); }
    const margin = (max - min) * .12; min -= margin; max += margin;
    const el = (tag, attrs = {}, parent = svg) => { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v)); parent.append(n); return n; };
    const y = v => pad.t + (max - v) / (max - min) * plotH;
    for (let i = 0; i < 4; i++) { const value = min + (max - min) * i / 3, yy = y(value); el('line', { x1: pad.l, x2: width - pad.r, y1: yy, y2: yy, stroke: '#e8efed', 'stroke-width': 1 }); const label = el('text', { x: pad.l - 8, y: yy + 3, 'text-anchor': 'end', fill: '#849395', 'font-size': 9 }); label.textContent = fmtNumber.format(value); }
    const count = data.length, step = plotW / count, barW = Math.max(1, step * .72); let firstPredX = null;
    data.forEach((s, i) => {
      const x = pad.l + i * step + (step - barW) / 2;
      const c = s.status === 'known' ? '#13866f' : s.status === 'predicted' ? '#df963f' : '#dce5e3';
      if (s.status === 'predicted' && firstPredX === null) firstPredX = x + barW / 2;
      if (Number.isFinite(s.value)) { const yy = y(s.value); el('rect', { x, y: yy, width: barW, height: Math.max(2, pad.t + plotH - yy), rx: Math.min(2, barW / 2), fill: c, opacity: s.status === 'predicted' ? .79 : .92 }); }
      else el('rect', { x, y: pad.t + plotH - 3, width: barW, height: 3, rx: 1, fill: c });
    });
    if (firstPredX !== null) { el('line', { x1: firstPredX, x2: firstPredX, y1: pad.t, y2: pad.t + plotH, stroke: '#c67925', 'stroke-width': 1.3, 'stroke-dasharray': '4 4' }); const t = el('text', { x: Math.min(firstPredX + 5, width - 103), y: pad.t + 10, fill: '#a86922', 'font-size': 9 }); t.textContent = 'prognose start'; }
    const marks = [0, Math.floor(count / 4), Math.floor(count / 2), Math.floor(count * 3 / 4), count - 1].filter((v, i, a) => a.indexOf(v) === i);
    marks.forEach(i => { const x = pad.l + i * step + step / 2; const label = el('text', { x, y: height - 10, 'text-anchor': 'middle', fill: '#758689', 'font-size': 9 }); label.textContent = fmtTime.format(data[i].startDate).replace(/\s[A-Z]{1,4}$/, ''); });
    const boundary = slots.find(s => s.status === 'predicted'); setText('forecast-boundary', boundary ? `Prognose begint ${humanTime(boundary.startDate)}.` : 'Er is voor deze dag nog geen prognosepunt.');
    setText('chart-unit', slots.find(s => s.unit)?.unit || '');
  }
  function renderWindows(windows, unit) {
    const root = $('window-groups'); root.replaceChildren();
    const groups = [['Bekend', windows?.known || []], ['Met prognose', windows?.mixed || []]];
    let shown = 0;
    for (const [title, entries] of groups) {
      if (!entries.length) continue;
      const section = document.createElement('section'); section.className = 'window-group'; const h = document.createElement('div'); h.className = 'group-title'; h.textContent = title; section.append(h);
      entries.slice(0, 3).forEach((w, i) => { const row = document.createElement('div'); row.className = 'window-item'; const rank = document.createElement('span'); rank.className = 'window-rank'; rank.textContent = String(i + 1); const desc = document.createElement('span'); const start = humanTime(w.start), end = humanTime(w.end); const strong = document.createElement('strong'); strong.textContent = `${start} – ${end}`; const small = document.createElement('small'); small.textContent = `${w.slots ?? ''}${w.slots != null ? ' kwartieren · ' : ''}${title === 'Bekend' ? 'volledig bekend' : 'bevat prognose'}`; desc.append(strong, small); const price = document.createElement('span'); price.className = 'window-price'; price.textContent = fmtPrice(w.average_price, unit); row.append(rank, desc, price); section.append(row); shown++; }); root.append(section);
    }
    if (!shown) { const empty = document.createElement('div'); empty.className = 'empty-state'; empty.textContent = 'Geen compleet, aaneengesloten venster gevonden voor deze duur en datum.'; root.append(empty); }
  }
  function renderTable(slots, unit) {
    const body = $('price-rows'); body.replaceChildren();
    const relevant = slots;
    if (!relevant.length) { const tr = document.createElement('tr'), td = document.createElement('td'); td.colSpan = 4; td.className = 'table-empty'; td.textContent = 'Geen kwartieren voor deze datum.'; tr.append(td); body.append(tr); }
    for (const s of relevant) { const tr = document.createElement('tr'), time = document.createElement('td'), price = document.createElement('td'), source = document.createElement('td'), status = document.createElement('td'); time.textContent = `${humanTime(s.startDate)} – ${humanTime(s.endDate)}`; price.textContent = fmtPrice(s.value, s.unit || unit); source.textContent = s.source || (s.status === 'known' ? 'Tariefentiteit' : s.status === 'predicted' ? 'Lokaal model' : 'Geen bron'); source.className = 'origin'; const pill = document.createElement('span'); pill.className = `status-pill ${statusClasses[s.status]}`; pill.textContent = statusNames[s.status]; status.append(pill); tr.append(time, price, source, status); body.append(tr); }
    setText('row-count', `${relevant.length} kwartieren`); setText('table-title', `Kwartieren · ${new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, dateStyle: 'full' }).format(safeDate(`${state.date}T12:00:00Z`) || new Date())}`);
  }
  function renderDashboard(data) {
    state.dashboard = data; const slots = normalizeSlots(data.slots); const current = data.current || {};
    setText('current-price', current.price == null ? '—' : fmtNumber.format(Number(current.price))); setText('current-unit', current.unit || ''); setText('current-entity', current.entity || state.settings?.tariff_entity || 'Tariefbron niet ingesteld'); setText('current-time', current.start ? `Start ${humanTime(current.start, true)}` : 'Geen huidig kwartier'); setText('current-origin', current.status === 'known' ? 'Bekend tarief' : current.status === 'predicted' ? 'Modelprognose' : 'Geen actuele waarde'); setText('current-state', statusNames[current.status] || 'Onbekend');
    const q = data.quality || {}; setText('uncertainty', q.uncertainty || 'Niet gekalibreerd voor kwartieren'); setText('missing-inputs', (q.missing_inputs || []).join(', ') || 'Geen gemeld'); setText('provisional-detail', [(q.reasons || []).join(' · ') || 'Deze kwartierprognose is indicatief; bandbreedte is niet gekalibreerd.', metricText(q.maturity?.metrics)].filter(Boolean).join(' '));
    const noteTitle = document.querySelector('.provisional-note strong');
    if (noteTitle) noteTitle.textContent = q.maturity?.ready ? 'Lokaal geëvalueerde prognose' : 'Voorlopige prognose';
    const unit = current.unit || slots.find(s => s.unit)?.unit || ''; renderChart(slots); renderTable(slots, unit); renderWindows(data.windows, unit); setText('last-refresh', `Laatst bijgewerkt: ${data.updated_at ? humanTime(data.updated_at, true) : '—'}`);
  }
  async function loadDashboard() {
    try { show('load-error', false); const hours = Number($('window-duration').value) || 1; const data = await api(`api/dashboard?date=${encodeURIComponent(state.date)}&window_hours=${hours}`); renderDashboard(data); setConnection(true); }
    catch (error) { setConnection(false, error.message); $('load-error-text').textContent = error.message; show('load-error', true); }
  }
  function optionFor(entity) { const o = document.createElement('option'); o.value = entity.entity_id; o.textContent = `${entity.friendly_name || entity.entity_id} · ${entity.entity_id}`; return o; }
  async function loadEntities() {
    const result = await api('api/entities'); state.entities = Array.isArray(result.entities) ? result.entities : [];
    if (result.error) {
      show('settings-error', true);
      $('settings-error').textContent = `Home Assistant-entiteiten zijn nu niet beschikbaar: ${result.error}`;
    }
    const targets = [['tariff-entity', e => e.domain === 'sensor'], ['solar-entity', e => e.domain === 'sensor'], ['wind-entity', e => e.domain === 'sensor'], ['temperature-entity', e => e.domain === 'sensor']];
    for (const [id, filter] of targets) { const select = $(id); const selected = select.value; select.replaceChildren(); const prompt = document.createElement('option'); prompt.value = ''; prompt.textContent = id === 'tariff-entity' ? 'Kies een kwartiertariefsensor' : 'Kies een entiteit'; select.append(prompt); for (const entity of state.entities.filter(filter)) select.append(optionFor(entity)); if (selected) select.value = selected; }
  }
  function setWeatherFields() { const ha = document.querySelector('input[name="weather_mode"]:checked')?.value === 'ha'; show('ha-weather-fields', ha); show('open-meteo-fields', !ha); }
  async function openSettings() {
    show('settings-error', false);
    try { state.settings = await api('api/settings'); }
    catch (error) { state.settings = {}; show('settings-error', true); $('settings-error').textContent = `Instellingen konden niet worden geladen: ${error.message}`; }
    try { await loadEntities(); }
    catch (error) {
      state.entities = [];
      show('settings-error', true);
      $('settings-error').textContent = `Entiteiten konden niet worden geladen: ${error.message}`;
    }
    fillSettings(state.settings || {});
    $('settings-dialog').showModal();
  }
  function fillSettings(s = {}) {
    const tariff = $('tariff-entity'); tariff.value = s.tariff_entity || '';
    if (!tariff.value && state.entities.some(e => e.entity_id === DEFAULT_TARIFF)) tariff.value = DEFAULT_TARIFF;
    const mode = s.weather_source === 'ha' || s.weather_source === 'home_assistant' ? 'ha' : 'open_meteo'; const radio = document.querySelector(`input[name="weather_mode"][value="${mode}"]`); if (radio) radio.checked = true;
    $('solar-entity').value = s.weather_entities?.solar || ''; $('wind-entity').value = s.weather_entities?.wind || ''; $('temperature-entity').value = s.weather_entities?.temperature || ''; $('tariff-unit').value = s.tariff_unit || 'EUR/kWh'; $('price-field').value = s.price_field || 'tax_included';
    const suggested = s.location_suggestion || {};
    $('latitude').value = s.latitude ?? suggested.latitude ?? '';
    $('longitude').value = s.longitude ?? suggested.longitude ?? '';
    let locationHelp = $('location-source');
    if (!locationHelp) {
      locationHelp = document.createElement('p');
      locationHelp.id = 'location-source';
      locationHelp.className = 'field-help';
      $('open-meteo-fields').after(locationHelp);
    }
    locationHelp.textContent = s.latitude != null && s.longitude != null ? 'Lokaal opgeslagen locatie.' : suggested.latitude != null && suggested.longitude != null ? 'Voorstel uit de Home-locatie van Home Assistant. Wordt pas voor Open-Meteo gebruikt nadat je Instellingen opslaat.' : 'Geen Home-locatie gevonden; vul de coördinaten zelf in.';
    setWeatherFields();
  }
  async function saveSettings(event) {
    event.preventDefault(); const weatherSource = document.querySelector('input[name="weather_mode"]:checked')?.value || 'open_meteo'; const tariff = $('tariff-entity').value;
    if (!tariff) { show('settings-error', true); $('settings-error').textContent = 'Selecteer een bestaande tariefentiteit.'; return; }
    const settings = { tariff_entity: tariff, tariff_unit: $('tariff-unit').value, price_field: $('price-field').value, weather_source: weatherSource, weather_entities: { solar: $('solar-entity').value || null, wind: $('wind-entity').value || null, temperature: $('temperature-entity').value || null }, latitude: $('latitude').value === '' ? null : Number($('latitude').value), longitude: $('longitude').value === '' ? null : Number($('longitude').value) };
    if (weatherSource === 'ha' && Object.values(settings.weather_entities).some(v => !v)) { show('settings-error', true); $('settings-error').textContent = 'Kies zon, wind en temperatuur om HA-weer te gebruiken.'; return; }
    const button = $('settings-save'); button.disabled = true; button.textContent = 'Opslaan…';
    try { state.settings = await api('api/settings', { method: 'PUT', body: JSON.stringify(settings) }); $('settings-dialog').close(); toast('Instellingen opgeslagen. De app haalt de bronnen opnieuw op.'); await loadAll(); }
    catch (error) { show('settings-error', true); $('settings-error').textContent = error.message; }
    finally { button.disabled = false; button.textContent = 'Opslaan'; }
  }
  function toast(message) { const t = $('toast'); t.textContent = message; show('toast', true); setTimeout(() => show('toast', false), 3600); }
  async function loadAll() {
    try { state.settings = await api('api/settings'); } catch (_) { state.settings = null; }
    try { updateStatus(await api('api/status')); } catch (error) { setConnection(false, error.message); $('load-error-text').textContent = error.message; show('load-error', true); return; }
    await loadDashboard();
  }
  function init() {
    setupDates(); $('settings-open').addEventListener('click', openSettings); $('settings-close').addEventListener('click', () => $('settings-dialog').close()); $('settings-cancel').addEventListener('click', () => $('settings-dialog').close()); $('settings-form').addEventListener('submit', saveSettings); $('retry').addEventListener('click', loadAll); $('window-duration').addEventListener('change', loadDashboard); document.querySelectorAll('input[name="weather_mode"]').forEach(r => r.addEventListener('change', setWeatherFields));
    window.addEventListener('resize', () => { if (state.dashboard) renderChart(normalizeSlots(state.dashboard.slots)); });
    loadAll(); state.timer = setInterval(loadAll, 60000);
  }
  document.addEventListener('DOMContentLoaded', init);
})();
