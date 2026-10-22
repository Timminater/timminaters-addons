(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = { settings: null, entities: [], dashboard: null, analysis: null, analysisLoaded: false, activeView: 'dashboard', timer: null, promptedConfig: false, chartScrollInitialized: false };
  const DEFAULT_TARIFF = 'sensor.zonneplan_current_quarter_hourly_electricity_tariff';
  const TZ = 'Europe/Amsterdam';
  const statusNames = { known: 'Bekend', predicted: 'Voorspeld', missing: 'Ontbreekt' };
  const fmtDate = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short' });
  const fmtTime = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, hour: '2-digit', minute: '2-digit', timeZoneName: 'short' });
  const fmtDateTime = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  const fmtWindowDateTime = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  const fmtNumber = new Intl.NumberFormat('nl-NL', { minimumFractionDigits: 3, maximumFractionDigits: 5 });
  const fmtFullDateTime = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, weekday: 'long', day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
  function setText(id, value, fallback = '—') { const e = $(id); if (e) e.textContent = value == null || value === '' ? fallback : String(value); }
  function show(id, visible) { const e = $(id); if (e) e.hidden = !visible; }
  function describePriceSource(source = {}) {
    if (source.price_source === 'energy_charts_nl') {
      const choice = source.price_choice === 'all_in' ? 'all-in afnameprijs' : 'kale beursprijs';
      const supplier = source.price_choice === 'all_in' && source.supplier ? ` · ${source.supplier}` : '';
      return `Energy-Charts Nederland · ${choice}${supplier}`;
    }
    return source.tariff_entity || state.settings?.tariff_entity || 'Niet ingesteld';
  }
  function safeDate(value) { const d = new Date(value); return Number.isNaN(d.getTime()) ? null : d; }
  function dateKey(d) { return new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d); }
  function fmtPrice(value, unit) { if (value == null || value === '') return '—'; const n = Number(value); return Number.isFinite(n) ? `${fmtNumber.format(n)} ${unit || ''}`.trim() : '—'; }
  function humanTime(value, withDate = false) { const d = safeDate(value); return d ? (withDate ? fmtDateTime : fmtTime).format(d) : '—'; }
  function windowTime(value) { const d = safeDate(value); if (!d) return '—'; const text = fmtWindowDateTime.format(d); return text.charAt(0).toUpperCase() + text.slice(1); }
  function setConnection(online, text) { const e = $('connection-status'); e.classList.toggle('offline', !online); e.innerHTML = `<i></i> ${online ? 'Lokaal verbonden' : 'Niet verbonden'}`; if (text) e.title = text; }
  async function api(path, options = {}) {
    const response = await fetch(new URL(path.replace(/^\//, ''), document.baseURI), { cache: 'no-store', credentials: 'same-origin', ...options, headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) } });
    const type = response.headers.get('content-type') || '';
    if (!response.ok) { let message = `Aanvraag mislukt (${response.status})`; if (type.includes('json')) { const body = await response.json().catch(() => ({})); message = body.error || body.message || message; } throw new Error(message); }
    if (!type.includes('json')) throw new Error('De lokale app gaf geen JSON-antwoord.');
    return response.json();
  }
  function reportErrors(errors = []) {
    const relevant = Array.isArray(errors) ? errors : [];
    if (relevant.length) { $('notice').textContent = relevant.map(e => e.message || e.source || 'Bronfout').join(' · '); show('notice', true); }
    else show('notice', false);
  }
  function metricText(metrics) {
    if (!metrics || !metrics.n) return '';
    const unit = state.settings?.tariff_unit || '';
    return `Gemeten op ${metrics.n} kwartieren: MAE ${fmtPrice(metrics.mae, unit)}, bias ${fmtPrice(metrics.bias, unit)}, kwartierbasislijn MAE ${fmtPrice(metrics.baseline_mae, unit)}.`;
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
    setText('calculation-cadence', `Automatisch: elke ${data.calculation_interval_minutes || 5} min`);
    setText('price-source-state', data.stale ? 'Verouderd' : (data.last_price_update ? 'Actueel' : 'Wacht'));
    setText('weather-source-state', data.last_weather_update ? 'Beschikbaar' : 'Wacht');
    setText('model-source-state', data.last_model_update ? 'Gereed' : 'Wacht');
    setText('detail-price-source', describePriceSource(data.sources || {}));
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
    const svg = $('price-chart'), axis = $('chart-axis'), scroll = svg.parentElement, tooltip = $('chart-tooltip');
    const oldScroll = scroll.scrollLeft;
    svg.replaceChildren(); axis.replaceChildren(); show('chart-tooltip', false);
    const data = fillGaps(slots);
    show('chart-empty', !data.length); svg.hidden = !data.length;
    if (!data.length) return;
    const compact = window.matchMedia('(max-width: 620px)').matches;
    const step = 11, height = compact ? 190 : 265;
    const pad = { l: 52, r: 18, t: compact ? 12 : 18, b: compact ? 34 : 43 }, width = Math.max(scroll.clientWidth, pad.l + data.length * step + pad.r), plotH = height - pad.t - pad.b;
    svg.style.width = `${width}px`;
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const values = data.flatMap(s => [s.value, Number(s.lower), Number(s.upper)]).filter(Number.isFinite);
    const low = Math.min(0, ...values), high = Math.max(0, ...values);
    const span = Math.max(.05, high - low), min = low < 0 ? low - span * .07 : 0, max = high + span * .12;
    const el = (tag, attrs = {}, parent = svg) => { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v)); parent.append(n); return n; };
    const y = v => pad.t + (max - v) / (max - min) * plotH;
    axis.setAttribute('viewBox', `0 0 ${pad.l} ${height}`);
    for (let i = 0; i <= 4; i++) { const value = min + (max - min) * i / 4, yy = y(value); el('line', { x1: pad.l, x2: width - pad.r, y1: yy, y2: yy, stroke: Math.abs(value) < .0001 ? '#9ab1ac' : '#e8efed', 'stroke-width': 1 }); const label = el('text', { x: pad.l - 7, y: yy + 3, 'text-anchor': 'end', fill: '#647c7a', 'font-size': 9 }, axis); label.textContent = fmtNumber.format(value); }
    if (min < 0) el('line', { x1: pad.l, x2: width - pad.r, y1: y(0), y2: y(0), stroke: '#8da9a2', 'stroke-width': 1.4 });
    const count = data.length, barW = 7; let firstPredX = null, previousDay = '';
    const showTooltip = (slot, x) => {
      const unit = slot.unit || state.settings?.tariff_unit || 'EUR/kWh';
      const lines = [fmtFullDateTime.format(slot.startDate), ...(slot.status === 'predicted' ? [methodName(state.dashboard?.method?.id || state.settings?.active_method, state.dashboard?.model_version, state.dashboard?.method?.timestral_model)] : []), `${statusNames[slot.status]} · ${fmtPrice(slot.value, unit)}`];
      if (slot.status === 'predicted' && Number.isFinite(Number(slot.lower)) && Number.isFinite(Number(slot.upper)) && slot.lower != null && slot.upper != null) lines.push(`Indicatieve marge: ${fmtPrice(slot.lower, unit)} tot ${fmtPrice(slot.upper, unit)}`);
      tooltip.textContent = lines.join('\n'); show('chart-tooltip', true);
      tooltip.style.left = `${Math.max(4, Math.min(scroll.clientWidth - tooltip.offsetWidth - 4, x - scroll.scrollLeft + 12))}px`;
      tooltip.style.top = '12px';
    };
    data.forEach((s, i) => {
      const x = pad.l + i * step + (step - barW) / 2, center = x + barW / 2;
      const c = s.status === 'known' ? '#13866f' : s.status === 'predicted' ? '#df963f' : '#dce5e3';
      const day = dateKey(s.startDate);
      if (day !== previousDay) { if (i) el('line', { x1: pad.l + i * step, x2: pad.l + i * step, y1: pad.t, y2: pad.t + plotH, stroke: '#b7c9c5', 'stroke-dasharray': '3 4' }); const label = el('text', { x: center + 3, y: height - 7, fill: '#526b69', 'font-size': 10, 'font-weight': 700 }); label.textContent = fmtDate.format(s.startDate); previousDay = day; }
      if (s.status === 'predicted' && firstPredX === null) firstPredX = center;
      if (s.status === 'predicted' && s.lower != null && s.upper != null && Number.isFinite(Number(s.lower)) && Number.isFinite(Number(s.upper))) {
        const top = y(Number(s.upper)), bottom = y(Number(s.lower));
        el('rect', { x: x - 1, y: top, width: barW + 2, height: Math.max(1, bottom - top), fill: '#df963f', opacity: .16 });
        el('line', { x1: center, x2: center, y1: top, y2: bottom, stroke: '#bf7624', 'stroke-width': 1 });
        for (const yy of [top, bottom]) el('line', { x1: x - 1, x2: x + barW + 1, y1: yy, y2: yy, stroke: '#bf7624', 'stroke-width': 1 });
      }
      if (Number.isFinite(s.value)) { const yy = y(s.value), zero = y(0); el('rect', { x, y: Math.min(yy, zero), width: barW, height: Math.max(2, Math.abs(zero - yy)), rx: Math.min(2, barW / 2), fill: c, opacity: s.status === 'predicted' ? .7 : .92 }); }
      else el('rect', { x, y: y(0) - 2, width: barW, height: 3, rx: 1, fill: c });
      if (s.startDate.getMinutes() === 0 && s.startDate.getHours() % 3 === 0) { const tick = el('text', { x: center, y: height - 24, 'text-anchor': 'middle', fill: '#758689', 'font-size': 9 }); tick.textContent = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, hour: '2-digit' }).format(s.startDate); }
      const hit = el('rect', { x: pad.l + i * step, y: pad.t, width: step, height: plotH, fill: 'transparent', tabindex: 0, role: 'button', 'aria-label': `${fmtFullDateTime.format(s.startDate)}, ${statusNames[s.status]}, ${fmtPrice(s.value, s.unit)}` });
      hit.addEventListener('pointerenter', () => showTooltip(s, center));
      hit.addEventListener('focus', () => showTooltip(s, center));
      hit.addEventListener('pointerleave', () => show('chart-tooltip', false));
      hit.addEventListener('blur', () => show('chart-tooltip', false));
      hit.addEventListener('click', () => showTooltip(s, center));
      hit.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); showTooltip(s, center); } });
    });
    if (firstPredX !== null) { el('line', { x1: firstPredX, x2: firstPredX, y1: pad.t, y2: pad.t + plotH, stroke: '#c67925', 'stroke-width': 1.3, 'stroke-dasharray': '4 4' }); const t = el('text', { x: firstPredX + 5, y: pad.t + 10, fill: '#a86922', 'font-size': 9 }); t.textContent = 'prognose start'; }
    const now = new Date();
    const current = data.findIndex(s => s.startDate <= now && now < s.endDate);
    if (current >= 0) {
      const fraction = (now - data[current].startDate) / (data[current].endDate - data[current].startDate);
      const nowX = pad.l + (current + fraction) * step;
      el('line', { x1: nowX, x2: nowX, y1: pad.t, y2: pad.t + plotH, stroke: '#bb4e36', 'stroke-width': 2, 'stroke-dasharray': '5 4', 'pointer-events': 'none' });
      const label = el('text', { x: nowX + 4, y: pad.t + 10, fill: '#a4402c', 'font-size': 10, 'font-weight': 800, 'pointer-events': 'none' });
      label.textContent = 'NU';
    }
    const boundary = slots.find(s => s.status === 'predicted'); setText('forecast-boundary', boundary ? `Prognose begint ${humanTime(boundary.startDate, true)}.` : 'Er is nog geen prognosepunt.');
    setText('chart-unit', slots.find(s => s.unit)?.unit || state.settings?.tariff_unit || '');
    if (!state.chartScrollInitialized) { const next = data.findIndex(s => s.endDate > now), nextIndex = next < 0 ? 0 : next; scroll.scrollLeft = compact ? Math.max(0, pad.l + nextIndex * step - scroll.clientWidth / 2) : Math.max(0, nextIndex * step - 110); state.chartScrollInitialized = true; }
    else scroll.scrollLeft = oldScroll;
  }
  function renderWindows(windows, unit) {
    const root = $('window-groups'); root.replaceChildren();
    const groups = [['Bekend', windows?.known || []], ['Met prognose', windows?.mixed || []]];
    let shown = 0;
    for (const [title, entries] of groups) {
      if (!entries.length) continue;
      const section = document.createElement('section'); section.className = 'window-group'; const h = document.createElement('div'); h.className = 'group-title'; h.textContent = title; section.append(h);
      entries.slice(0, 2).forEach((w, i) => { const row = document.createElement('div'); row.className = 'window-item'; const rank = document.createElement('span'); rank.className = 'window-rank'; rank.textContent = String(i + 1); const desc = document.createElement('span'); const start = windowTime(w.start), end = windowTime(w.end); const strong = document.createElement('strong'); strong.textContent = `${start} – ${end}`; const small = document.createElement('small'); small.textContent = `${w.slots ?? ''}${w.slots != null ? ' kwartieren · ' : ''}${title === 'Bekend' ? 'volledig bekend' : 'bevat prognose'}`; desc.append(strong, small); const price = document.createElement('span'); price.className = 'window-price'; price.textContent = fmtPrice(w.average_price, unit); row.append(rank, desc, price); section.append(row); shown++; }); root.append(section);
    }
    if (!shown) { const empty = document.createElement('div'); empty.className = 'empty-state'; empty.textContent = 'Geen compleet, aaneengesloten venster gevonden voor deze duur.'; root.append(empty); }
  }
  function renderDashboard(data) {
    const method = data.method?.id || state.settings?.active_method;
    const shown = methodName(method, data.model_version, data.method?.timestral_model);
    const enabled = data.method?.enabled ?? state.settings?.forecast_methods?.[method];
    setText('shown-model', shown);
    setText('shown-quality', data.quality?.label || 'Voorlopig');
    setText('shown-model-state', enabled === false ? 'Uitgeschakeld · alleen bekende tarieven' : data.updated_at ? `Dit model levert de prognose in de grafiek en voor MQTT.${method === 'timestral' ? data.model_version?.includes('-calendar-1:') ? ' Weekendinformatie: aan.' : ' Zonder expliciete weekendinformatie.' : ''}${method === 'timestral' ? data.model_version?.includes(':weather-1-') ? ' Regionale weerinvoer: aan.' : ' Zonder weerinvoer.' : ''}` : 'Geselecteerd · nog geen prognose beschikbaar');
    setText('shown-model-issued', data.updated_at ? `Uitgegeven ${humanTime(data.updated_at, true)}${data.method?.weather_region ? ' · Weergebied: ' + ({local:'lokaal',nl:'Nederland (4 punten)',europe:'Nederland en buurlanden (10 punten)'}[data.method.weather_region] || data.method.weather_region) : ''}` : 'Bekende tarieven blijven zichtbaar zonder modelberekening.');
    setText('chart-model', data.updated_at ? `Prognose: ${shown}` : enabled === false ? 'Alleen bekende tarieven' : `Geselecteerd: ${shown} · wacht op prognose`);
    state.dashboard = data; const slots = normalizeSlots(data.slots); const current = data.current || {};
    setText('current-price', current.price == null ? '—' : fmtNumber.format(Number(current.price))); setText('current-unit', current.unit || ''); setText('current-entity', current.entity || describePriceSource(data.source || {})); setText('current-time', current.start ? `Start ${humanTime(current.start, true)}` : 'Geen huidig kwartier'); setText('current-origin', data.source?.price_source === 'energy_charts_nl' && current.status === 'known' ? 'Bekende marktprijs' : current.status === 'known' ? 'Bekend tarief' : current.status === 'predicted' ? 'Modelprognose' : 'Geen actuele waarde'); setText('current-state', statusNames[current.status] || 'Onbekend');
    const q = data.quality || {}; setText('uncertainty', q.uncertainty || 'Niet gekalibreerd voor kwartieren'); setText('band-note', q.uncertainty || 'De getoonde marge is een ongekalibreerde modelindicatie, geen betrouwbaarheidsinterval of vaste prijs.'); $('forecast-legend').lastChild.textContent = q.band_calibrated ? ' Voorspeld met empirische band' : ' Voorspeld met indicatieve marge'; setText('missing-inputs', (q.missing_inputs || []).join(', ') || 'Geen gemeld'); setText('provisional-detail', [(q.reasons || []).join(' · ') || 'Deze kwartierprognose is indicatief; bandbreedte is niet gekalibreerd.', metricText(q.maturity?.metrics)].filter(Boolean).join(' '));
    const noteTitle = document.querySelector('.provisional-note strong');
    if (noteTitle) noteTitle.textContent = q.maturity?.ready ? 'Lokaal geëvalueerde prognose' : 'Voorlopige prognose';
    const unit = current.unit || slots.find(s => s.unit)?.unit || ''; renderChart(slots); renderWindows(data.windows, unit); setText('last-refresh', `Laatst bijgewerkt: ${data.updated_at ? humanTime(data.updated_at, true) : '—'}`);
  }
  async function loadDashboard() {
    try { show('load-error', false); const hours = Number($('window-duration').value) || 1; const data = await api(`api/timeline?window_hours=${hours}`); renderDashboard(data); setConnection(true); }
    catch (error) { setConnection(false, error.message); $('load-error-text').textContent = error.message; show('load-error', true); }
  }
  function appendCell(row, value) { const cell = document.createElement('td'); cell.textContent = value == null ? '—' : String(value); row.append(cell); }
  function renderAnalysis(data) {
    state.analysis = data; state.analysisLoaded = true;
    const detailTitle = document.querySelector('.analysis-details > summary');
    if (detailTitle) detailTitle.textContent = 'Details actieve methode · ' + methodName(state.settings?.active_method, state.dashboard?.model_version, state.settings?.timestral_model);
    const unit = data.unit || state.settings?.tariff_unit || '';
    const summary = $('analysis-summary'); summary.replaceChildren();
    const addMetric = (label, value) => { const card = document.createElement('div'); card.className = 'card analysis-metric'; const caption = document.createElement('span'); caption.textContent = label; const strong = document.createElement('strong'); strong.textContent = value; card.append(caption, strong); summary.append(card); };
    addMetric('Evaluatieperiode', `${data.summary?.days ?? 0} dagen`); addMetric('Vergelijkbare kwartieren', String(data.summary?.points ?? 0));
    const pointCount = Number(data.summary?.points) || 0;
    const waiting = Number(data.summary?.pending_points) || 0;
    const firstWaiting = data.summary?.first_pending_start ? humanTime(data.summary.first_pending_start, true) : null;
    const emptyReason = data.summary?.runs
      ? `${data.summary.runs} dagelijkse prognose${data.summary.runs === 1 ? '' : 's'} bewaard; ${waiting} voorspelde kwartieren hebben nog geen later bekend tarief.${firstWaiting ? ` Eerste openstaande kwartier: ${firstWaiting}.` : ''}`
      : 'Nog geen bewaarde prognoses voor deze tariefbron en instelling; de analyse begint na de eerste modelrun.';
    setText('analysis-state', pointCount ? `Evaluatie van de afgelopen ${data.summary?.days ?? 0} dagen; de tabel toont maximaal 40 recente kwartierparen.` : emptyReason);
    const horizons = $('analysis-horizons'); horizons.replaceChildren();
    (Array.isArray(data.horizons) ? data.horizons : []).forEach(item => { const row = document.createElement('tr'); appendCell(row, item.horizon); appendCell(row, item.n ?? 0); appendCell(row, fmtPrice(item.mae, unit)); appendCell(row, fmtPrice(item.bias, unit)); horizons.append(row); });
    if (!horizons.children.length) { const row = document.createElement('tr'), cell = document.createElement('td'); cell.colSpan = 4; cell.className = 'table-empty'; cell.textContent = 'Nog onvoldoende evaluatiepunten per horizon.'; row.append(cell); horizons.append(row); }
    const calibration = $('analysis-calibration'), c = data.calibration || {}; calibration.replaceChildren();
    const calibrated = c.ready === true && c.nominal_coverage != null && c.observed_coverage != null && Number.isFinite(Number(c.nominal_coverage)) && Number.isFinite(Number(c.observed_coverage));
    $('analysis-band-heading').hidden = !calibrated;
    if (calibrated) {
      const p = document.createElement('p'); p.className = 'calibration-state'; p.textContent = 'Gekalibreerd interval'; calibration.append(p);
      const detail = document.createElement('p'); detail.className = 'subtle'; detail.textContent = `Nominaal ${(Number(c.nominal_coverage) * 100).toFixed(1)}% · gemeten ${(Number(c.observed_coverage) * 100).toFixed(1)}% · ${c.points ?? 0} punten over ${c.days ?? 0} dagen.`; calibration.append(detail);
    } else { const p = document.createElement('p'); p.textContent = c.reason || 'Nog niet gekalibreerd. Er wordt geen intervaldekking geclaimd.'; calibration.append(p); }
    const comparisons = $('analysis-comparisons'); comparisons.replaceChildren(); const entries = Array.isArray(data.comparisons) ? data.comparisons : [];
    entries.slice(0, 40).forEach(item => { const row = document.createElement('tr'); appendCell(row, humanTime(item.start, true)); appendCell(row, humanTime(item.issued_at, true)); appendCell(row, fmtPrice(item.forecast, unit)); appendCell(row, fmtPrice(item.actual, unit)); appendCell(row, fmtPrice(item.error, unit)); if (calibrated) appendCell(row, item.lower == null || item.upper == null ? '—' : `${fmtPrice(item.lower, unit)} – ${fmtPrice(item.upper, unit)}`); comparisons.append(row); });
    show('comparison-empty', entries.length === 0);
    if (!entries.length) { const row = document.createElement('tr'), cell = document.createElement('td'); cell.colSpan = calibrated ? 6 : 5; cell.className = 'table-empty'; cell.textContent = 'Nog geen prognosekwartieren met later bekend tarief beschikbaar.'; row.append(cell); comparisons.append(row); }
    show('analysis-error', false);
  }
  async function loadAnalysis() {
    try { show('analysis-error', false); renderAnalysis(await api('api/analysis')); await loadMethods(); }
    catch (error) { state.analysisLoaded = false; const box = $('analysis-error'); box.replaceChildren(); const text = document.createElement('span'); text.textContent = `Analyse kon niet worden geladen: ${error.message}`; box.append(text); show('analysis-error', true); }
  }
  let historyTimer = null;
  async function loadMethods() {
    if (!state.compareInitialized) {
      for (const id of ['local', 'v4', 'epex', 'timestral']) $(`compare-${id}`).checked = state.settings?.forecast_methods?.[id] ?? (id === 'local');
      state.compareInitialized = true;
    }
    const request = state.compareRequest = (state.compareRequest || 0) + 1;
    const selected = ['local', 'v4', 'epex', 'timestral'].filter(id => $(`compare-${id}`).checked);
    const query = new URLSearchParams({mode: $('comparison-mode').value, horizon: $('comparison-horizon').value, days: $('comparison-days').value, methods: selected.join(',') || 'none'});
    let data;
    try { data = await api('api/methods?' + query); } catch (error) { if (request !== state.compareRequest) return; throw error; }
    if (request !== state.compareRequest) return;
    renderPerformance(data);
    const body = $('method-comparisons'); body.replaceChildren();
    const score = value => `${fmtPrice(value?.mae, data.unit)} / ${value?.n ?? 0}`;
    for (const method of data.methods || []) {
      const row = document.createElement('tr');
      appendCell(row, `${method.label}${method.id === data.active_method && method.enabled ? ' (actief)' : ''}`);
      appendCell(row, !method.enabled ? 'Uit' : method.status?.error || (method.status?.last_success ? 'Gereed' : 'Wacht op berekening'));
      appendCell(row, score(method.live));
      appendCell(row, method.historical_supported ? score(method.historical) : 'Niet beschikbaar');
      body.append(row);
    }
    const horizons = $('method-horizons'); horizons.replaceChildren();
    for (const method of data.methods || []) {
      const row = document.createElement('tr'); appendCell(row, method.label + (method.historical?.weather_reconstruction ? ' · archiefweer' : ''));
      for (const name of ['0-24u', '24-48u', '48-72u', '72-96u', '96-120u', '120-144u', '144-168u']) appendCell(row, score(method.historical?.horizons?.find(h => h.horizon === name)));
      horizons.append(row);
    }
    setText('method-pairing', data.pairing_notice);
    setText('history-notice', data.historical_notice);
    const job = data.history_job || {};
    const running = job.state === 'running'; $('history-test').disabled = running; $('history-test-horizon').disabled = running; $('history-weather-mode').disabled = running;
    const labels = {idle: 'Nog geen historische test gestart.', running: 'Historische test bezig', completed: 'Historische test afgerond', completed_with_errors: 'Afgerond met fouten', completed_with_skips: 'Afgerond met overgeslagen historische uitgiftes', failed: 'Historische test mislukt', cancelled: 'Historische test gestopt'};
    setText('history-progress', state.historyError || `${labels[job.state] || job.state}${job.horizon_hours ? ` · ${job.horizon_hours} uur per uitgifte` : ''} ${job.total ? `${job.completed || 0}/${job.total}` : ''} ${(job.errors || []).map(e => `${e.method || ''}: ${e.message}`).join(' · ')}${job.skipped?.length ? ` · ${job.skipped.length} overgeslagen. ${[...new Set(job.skipped.map(e => `${e.method}: ${e.message}`))].join(' · ')}` : ''}`);
    if (historyTimer) clearTimeout(historyTimer);
    if (running) historyTimer = setTimeout(() => loadMethods().catch(error => { setText('history-progress', error.message); $('history-test').disabled = false; }), 2500);
  }
  async function testHistory() {
    state.historyError = null;
    $('history-test').disabled = true;
    try { await api('api/history-test?horizon_hours=' + encodeURIComponent($('history-test-horizon').value) + '&weather=' + encodeURIComponent($('history-weather-mode').value), {method: 'POST'}); await loadMethods(); }
    catch (error) { state.historyError = error.message; setText('history-progress', error.message); $('history-test').disabled = false; }
  }
  function updateTimestralModel() {
    const selected = $('timestral-model').value;
    const spec = state.settings?.timestral_models?.[selected];
    $('timestral-consent').checked = false;
    $('timestral-license').href = `https://huggingface.co/mistralai/Timestral-${selected}/blob/${spec?.revision || 'main'}/LICENSE`;
    $('timestral-model-help').textContent = `Gewichten circa ${Math.round((spec?.bytes || 0) / 1000000)} MB. Download alleen na bevestiging; inferentie gebruikt uitsluitend lokale bestanden. PyTorch blijft onderdeel van de add-on, ook bij een klein model.`;
  }
  let modelTimer = null;
  async function loadTimestralStatus() {
    try {
      const selected = $('timestral-model').value;
      const status = await api('api/timestral?model=' + encodeURIComponent(selected));
      if ($('timestral-model').value !== selected) return;
      const running = status.job?.state === 'running';
      const mb = value => Math.round((value || 0) / 1024 / 1024);
      $('timestral-download').disabled = running;
      $('timestral-download').textContent = running ? 'Download bezig…' : status.ready ? 'Controleer / herstel modeldownload' : 'Download Timestral-model';
      setText('timestral-download-status', status.ready ? `Timestral-${status.size} staat lokaal klaar. Inschakelen start geen nieuwe download.` : status.job?.state === 'failed' && status.job.size === selected ? status.job.message : running ? status.job.size !== selected ? status.job.message : `${status.job.message} · ${mb(status.downloaded_bytes)} / ${mb(status.download_bytes)} MB` : 'Model nog niet gedownload. Inschakelen alleen start geen download.');
      if (modelTimer) clearTimeout(modelTimer);
      if (running) modelTimer = setTimeout(loadTimestralStatus, 2500);
    } catch (error) { setText('timestral-download-status', error.message); }
  }
  async function downloadTimestral() {
    if (!$('timestral-consent').checked) { setText('timestral-download-status', 'Bevestig eerst de download en lees de modellicentie.'); return; }
    $('timestral-download').disabled = true;
    try { await api('api/timestral/download', {method: 'POST', body: JSON.stringify({confirmed: true, model: $('timestral-model').value})}); await loadTimestralStatus(); }
    catch (error) { setText('timestral-download-status', error.message); $('timestral-download').disabled = false; }
  }
  function activateView(view, focus = false) {
    state.activeView = view;
    for (const id of ['dashboard', 'analysis', 'data']) {
      const active = id === view;
      $(id + '-panel').hidden = !active;
      $(id + '-tab').classList.toggle('is-active', active);
      $(id + '-tab').setAttribute('aria-selected', String(active));
      $(id + '-tab').tabIndex = active ? 0 : -1;
    }
    if (focus) $(view + '-tab').focus();
    if (view === 'analysis') void loadAnalysis();
    if (view === 'data') void loadStorage();
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
  function setWeatherFields() { const ha = document.querySelector('input[name="weather_mode"]:checked')?.value === 'ha'; show('ha-weather-fields', ha); show('open-meteo-fields', $('weather-region').value === 'local'); }
  async function openSettings() {
    if (state.settingsOpening) return;
    state.settingsOpening = true; state.promptedConfig = true;
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
    state.settingsOpening = false;
  }
  function fillSettings(s = {}) {
    $('price-source').value = s.price_source || 'home_assistant';
    const tariff = $('tariff-entity'); tariff.value = s.tariff_entity || '';
    if (!tariff.value && state.entities.some(e => e.entity_id === DEFAULT_TARIFF)) tariff.value = DEFAULT_TARIFF;
    const mode = s.weather_source === 'ha' || s.weather_source === 'home_assistant' ? 'ha' : 'open_meteo'; const radio = document.querySelector(`input[name="weather_mode"][value="${mode}"]`); if (radio) radio.checked = true;
    $('solar-entity').value = s.weather_entities?.solar || ''; $('wind-entity').value = s.weather_entities?.wind || ''; $('temperature-entity').value = s.weather_entities?.temperature || ''; $('tariff-unit').value = s.tariff_unit || 'EUR/kWh'; $('price-field').value = s.price_field || 'tax_included';
    $('supplier').replaceChildren(...Object.entries(s.supplier_presets || {custom: {label: 'Eigen tarief'}}).map(([key, preset]) => new Option(preset.label, key)));
    $('price-choice').value = s.price_choice || 'bare'; $('supplier').value = s.supplier || 'custom';
    $('energy-tax').value = s.energy_tax_eur_kwh ?? 0.09161; $('vat-percent').value = s.vat_percent ?? 21;
    $('supplier-fee').value = s.supplier_fee_eur_kwh_incl_vat ?? 0;
    $('tariff-valid-from').value = s.tariff_valid_from || ''; $('tariff-valid-to').value = s.tariff_valid_to || '';
    $('supplier-fee-confirmed').checked = s.supplier_fee_confirmed === true;
    updateSupplierHelp();
    updatePriceSourceFields();
    $('calculation-interval').value = String(s.calculation_interval_minutes || 5);
    $('mqtt-enabled').checked = s.mqtt_enabled === true;
    for (const method of ['local', 'v4', 'epex', 'timestral']) $('method-' + method).checked = s.forecast_methods?.[method] ?? (method === 'local');
    $('active-method').value = s.active_method || 'local';
    $('ml-threads').value = s.ml_threads ?? 2;
    $('epex-training').value = s.epex_training_days ?? 180;
    const profiles = $('epex-profile');
    profiles.replaceChildren(new Option('Eigen tarief / andere leverancier', 'custom'));
    for (const [key, profile] of Object.entries(s.epex_tariff_profiles || {})) profiles.add(new Option(profile.label, key));
    profiles.value = s.epex_tariff_profile || 'custom';
    updateEpexProfile();
    $('epex-multiplier').value = s.epex_multiplier ?? 1;
    $('epex-offset').value = s.epex_offset_eur_kwh ?? 0;
    $('epex-confirmed').checked = s.epex_tariff_confirmed === true;
    $('weather-region').value = s.weather_region || 'nl';
    $('timestral-weather').checked = s.timestral_weather === true;
    $('timestral-calendar').checked = s.timestral_calendar !== false;
    $('timestral-model').value = s.timestral_model || '28M';
    updateTimestralModel();
    $('timestral-consent').checked = false;
    void loadTimestralStatus();
    $('timestral-context').value = s.timestral_context_days ?? 28;
    $('history-days').value = s.history_days ?? 90;
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
    event.preventDefault(); const weatherSource = document.querySelector('input[name="weather_mode"]:checked')?.value || 'open_meteo'; const tariff = $('tariff-entity').value; const priceSource = $('price-source').value;
    if (priceSource === 'home_assistant' && !tariff) { show('settings-error', true); $('settings-error').textContent = 'Selecteer een bestaande tariefentiteit.'; return; }
    const settings = { tariff_entity: tariff, price_source: priceSource, price_choice: $('price-choice').value, supplier: $('supplier').value, energy_tax_eur_kwh: Number($('energy-tax').value), vat_percent: Number($('vat-percent').value), supplier_fee_eur_kwh_incl_vat: Number($('supplier-fee').value), tariff_valid_from: $('tariff-valid-from').value, tariff_valid_to: $('tariff-valid-to').value, supplier_fee_confirmed: $('supplier-fee-confirmed').checked, tariff_unit: $('tariff-unit').value, price_field: $('price-field').value, weather_source: weatherSource, weather_entities: { solar: $('solar-entity').value || null, wind: $('wind-entity').value || null, temperature: $('temperature-entity').value || null }, latitude: $('latitude').value === '' ? null : Number($('latitude').value), longitude: $('longitude').value === '' ? null : Number($('longitude').value), calculation_interval_minutes: Number($('calculation-interval').value), mqtt_enabled: $('mqtt-enabled').checked };
    settings.forecast_methods = Object.fromEntries(['local', 'v4', 'epex', 'timestral'].map(m => [m, $('method-' + m).checked]));
    settings.active_method = $('active-method').value;
    settings.ml_threads = Number($('ml-threads').value);
    settings.epex_training_days = Number($('epex-training').value);
    settings.epex_tariff_profile = $('epex-profile').value;
    settings.epex_multiplier = Number($('epex-multiplier').value);
    settings.epex_offset_eur_kwh = Number($('epex-offset').value);
    settings.epex_tariff_confirmed = $('epex-confirmed').checked;
    settings.weather_region = $('weather-region').value;
    settings.timestral_weather = $('timestral-weather').checked;
    settings.timestral_calendar = $('timestral-calendar').checked;
    settings.timestral_model = $('timestral-model').value;
    settings.timestral_context_days = Number($('timestral-context').value);
    settings.history_days = Number($('history-days').value);
    if (priceSource === 'energy_charts_nl' && settings.price_choice === 'all_in' && (!$('supplier-fee').value || (settings.supplier === 'tibber' && !settings.supplier_fee_confirmed))) { show('settings-error', true); $('settings-error').textContent = 'Vul de gecontroleerde inkoopvergoeding incl. btw in en bevestig de btw-basis.'; return; }
    if (weatherSource === 'ha' && Object.values(settings.weather_entities).some(v => !v)) { show('settings-error', true); $('settings-error').textContent = 'Kies zon, wind en temperatuur om HA-weer te gebruiken.'; return; }
    const button = $('settings-save'); button.disabled = true; button.textContent = 'Opslaan…';
    try { state.settings = await api('api/settings', { method: 'PUT', body: JSON.stringify(settings) }); $('settings-dialog').close(); toast('Instellingen opgeslagen. De app haalt de bronnen opnieuw op.'); await loadAll(); }
    catch (error) { show('settings-error', true); $('settings-error').textContent = error.message; }
    finally { button.disabled = false; button.textContent = 'Opslaan'; }
  }
  function updateEpexProfile() {
    const profile = state.settings?.epex_tariff_profiles?.[$('epex-profile').value];
    show('epex-custom-tariff', !profile);
    $('epex-profile-help').textContent = profile ? `Automatisch: beursprijs × ${profile.multiplier} + € ${profile.offset_eur_kwh.toFixed(7)}/kWh. Toegepast van ${profile.valid_from} t/m ${profile.valid_to}, voor afname inclusief belasting en btw. Peildatum ${profile.as_of || "onbekend"}. Controleer of dit voorstel bij je contract past. Geen teruglevertarief of vaste maandkosten.` : 'Vul de omrekening van de kale beursprijs naar jouw HA-tarief in. Afwijkend contract? Gebruik Eigen tarief.';
  }
  function updatePriceSourceFields() {
    const market = $('price-source').value === 'energy_charts_nl';
    show('ha-tariff-fields', !market);
    show('epex-ha-tariff', !market);
    show('market-tariff-fields', market);
    $('tariff-entity').required = !market;
    $('tariff-entity').disabled = market;
    $('tariff-unit').disabled = market;
    $('price-field').disabled = market;
  }
  function updateSupplierHelp() {
    const preset = state.settings?.supplier_presets?.[$('supplier').value];
    $('supplier-preset-help').textContent = preset?.as_of ? `Tariefvoorstel uit Tweakers, peildatum ${preset.as_of}. Controleer je contract; de peildatum bewijst geen geldigheid voor eerdere kwartieren. Bedragen zijn aanpasbaar. Geen vaste maandkosten, saldering of teruglevering.` : 'Eigen tariefcomponenten en toepassingsperiode; bestaande waarden blijven behouden.';
  }
  function supplierDefaults() {
    const preset = state.settings?.supplier_presets?.[$('supplier').value];
    // Eigen tarief keeps the current values so a proposal can be adjusted.
    if (preset?.fee_incl_vat != null) {
      $('energy-tax').value = preset.energy_tax_eur_kwh;
      $('vat-percent').value = preset.vat_percent;
      $('tariff-valid-from').value = preset.valid_from;
      $('tariff-valid-to').value = preset.valid_to;
      $('supplier-fee').value = preset.fee_incl_vat;
    }
    $('supplier-fee-confirmed').checked = false;
    updateSupplierHelp();
  }
  function toast(message) { const t = $('toast'); t.textContent = message; show('toast', true); setTimeout(() => show('toast', false), 3600); }
  async function calculateNow() {
    const button = $('calculate-now'); button.disabled = true; button.textContent = 'Bezig…';
    try { const result = await api('api/calculate', { method: 'POST' }); await loadAll(); toast(`Berekening gereed · ${result.points ?? 0} prognosekwartieren.`); }
    catch (error) { show('notice', true); $('notice').textContent = `Berekenen mislukt: ${error.message}`; }
    finally { button.disabled = false; button.textContent = '↻ Bereken nu'; }
  }
  function storageBytes(value) {
    if (value < 1024) return `${value} B`;
    const unit = value >= 1073741824 ? 'GiB' : value >= 1048576 ? 'MiB' : 'KiB';
    const divisor = unit === 'GiB' ? 1073741824 : unit === 'MiB' ? 1048576 : 1024;
    return `${(value / divisor).toLocaleString('nl-NL', {maximumFractionDigits: 1})} ${unit}`;
  }
  function renderStorage(data) {
    setText('storage-open', `Opslag: ${storageBytes(data.total_bytes)}`);
    setText('storage-summary', `${storageBytes(data.total_bytes)} appdata · ${storageBytes(data.free_bytes)} vrij op het bestandssysteem`);
    setText('storage-breakdown', `Database: ${storageBytes(data.database_bytes)} · Timestral: ${storageBytes(data.timestral_bytes)} · Epex-cache: ${storageBytes(data.epex_cache_bytes)} · Overig: ${storageBytes(data.other_bytes)}`);
    const count = data.removable;
    setText('storage-preview', `Ouder dan ${data.days} dagen: ${count.quarter_prices} kwartierprijzen, ${count.quarter_price_revisions} revisies, ${count.hourly_history} uurprijzen, ${count.forecast_runs} prognoseruns en ${count.input_snapshots} bronsnapshots. Punten van verwijderde runs verdwijnen ook.`);
  }
  async function loadStorage() {
    try { renderStorage(await api('api/storage?days=' + encodeURIComponent($('storage-days').value))); }
    catch (error) { setText('storage-message', error.message); }
  }
  async function cleanStorage(action) {
    const days = Number($('storage-days').value);
    if (!Number.isInteger(days) || days < 7 || days > 3650) { setText('storage-message', 'Kies 7 t/m 3650 hele dagen.'); return; }
    const texts = {history: `Alle historie, revisies, bronsnapshots en prognoseruns ouder dan ${days} dagen definitief verwijderen? Dit vermindert de beschikbare analyse- en trainingshistorie.`, epex_cache: 'Epex-cache definitief verwijderen? De volgende Epex-run moet opnieuw brondata ophalen en trainen.', unused_models: 'Alle gedownloade Timestral-modellen behalve het momenteel opgeslagen gekozen model definitief verwijderen? Opnieuw gebruiken vereist een handmatige download.'};
    await loadStorage();
    if (!window.confirm(texts[action])) return;
    const buttons = ['storage-clean-history', 'storage-clean-cache', 'storage-clean-models'];
    buttons.forEach(id => $(id).disabled = true);
    setText('storage-message', 'Bezig met opruimen en zo nodig verkleinen van de database…');
    try {
      const result = await api('api/storage/cleanup', {method: 'POST', body: JSON.stringify({action, days, confirmed: true})});
      renderStorage(result.storage); setText('storage-message', result.message);
      state.analysisLoaded = false; await loadAll();
    } catch (error) { setText('storage-message', error.message); }
    finally { buttons.forEach(id => $(id).disabled = false); }
  }
  async function exportData() {
    const button = $('data-export'); button.disabled = true;
    setText('storage-message', 'Analyse-export voorbereiden…');
    try {
      const response = await fetch(new URL('api/export?days=' + encodeURIComponent($('export-days').value), document.baseURI), {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) { const error = await response.json(); throw new Error(error.error || 'Export mislukt'); }
      const blob = await response.blob(); const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = 'stroomvoorspeller-analyse.zip';
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      setText('storage-message', 'Export gedownload. Je kunt het ZIP-bestand zelf delen in de chat; er is niets automatisch verstuurd.');
    } catch (error) { setText('storage-message', error.message); }
    finally { button.disabled = false; }
  }
  const modelColors = {local: '#087e69', v4: '#466ac6', epex: '#bf6b20', timestral: '#9a52b3'};
  function methodName(method, version, size) {
    if (method === 'timestral') return `Timestral ${size || state.settings?.timestral_model || ''}`.trim();
    if (method === 'local' && version?.startsWith('local-v4-')) return 'Lokale v4 (oude berekening)';
    if (method === 'local') return /adaptive-quarter-(\d+)/.test(version || '') ? `Lokaal adaptief v${version.match(/adaptive-quarter-(\d+)/)[1]}` : 'Lokaal adaptief';
    return {v4: 'Oorspronkelijke v4', epex: 'EpexPredictor NL'}[method] || 'Nog niet gekozen';
  }
  function renderPerformance(data) {
    state.comparison = data;
    const view = data.view || {}, unit = data.unit || state.settings?.tariff_unit || '';
    const names = Object.fromEntries((data.methods || []).map(m => [m.id, m.label]));
    setText('compare-timestral-label', names.timestral || 'Timestral');
    for (const id of ['local', 'v4', 'epex', 'timestral']) $(`compare-${id}`).parentElement.style.setProperty('--method-color', modelColors[id]);
    const mode = view.mode === 'historical' ? 'Historische reconstructie' : 'Live voorspellingen';
    setText('comparison-context', view.selected?.length ? `${mode} · ${view.n || 0} vergelijkbare voorspellingen per methode · ${view.available?.length || 0} methode(n)` : 'Geen methoden geselecteerd.');
    const warnings = [];
    if (view.mode === 'historical') warnings.push('Reconstructie met huidige archiefdata; geen bewijs van live prestaties.');
    if (view.missing?.length) warnings.push(`Nog geen resultaten in dit venster: ${view.missing.map(m => names[m]).join(', ')}.`);
    if (view.available?.length === 1) warnings.push('Eén methode heeft resultaten; er is nog geen onderlinge vergelijking.');
    if (view.available?.length > 1 && !view.n) warnings.push('Er zijn resultaten, maar nog geen gemeenschappelijke uitgiftes en doelkwartieren. Er wordt geen rangorde berekend.');
    $('comparison-warning').textContent = warnings.join(' '); show('comparison-warning', warnings.length > 0);
    const bars = $('performance-bars'); bars.replaceChildren();
    const finite = (view.metrics || []).filter(m => m.n && m.mae != null && Number.isFinite(Number(m.mae)));
    const maximum = Math.max(0, ...finite.map(m => m.mae));
    for (const metric of view.metrics || []) {
      const row = document.createElement('div'); row.className = 'performance-bar-row';
      const label = document.createElement('span'); label.className = 'performance-method'; label.textContent = metric.label;
      const track = document.createElement('div'); track.className = 'performance-track';
      const fill = document.createElement('div'); fill.className = 'performance-fill'; fill.style.background = modelColors[metric.id];
      fill.style.width = `${metric.n && maximum ? metric.mae / maximum * 100 : 0}%`; track.append(fill);
      const value = document.createElement('strong'); value.textContent = metric.n ? fmtPrice(metric.mae, unit) : 'Nog geen score';
      const detail = document.createElement('small'); detail.textContent = `${metric.n || 0} paren · bias ${metric.n ? fmtPrice(metric.bias, unit) : '—'}`;
      row.append(label, track, value, detail); bars.append(row);
    }
    if (!view.metrics?.length) { const empty = document.createElement('p'); empty.className = 'empty-state'; empty.textContent = 'Nog geen vergelijkbare resultaten voor deze selectie.'; bars.append(empty); }
    renderPerformanceChart();
  }
  function renderPerformanceChart() {
    const data = state.comparison; if (!data) return;
    const view = data.view || {}, errors = $('comparison-chart-kind').value === 'errors';
    const svg = $('comparison-chart'); svg.replaceChildren();
    const legend = $('comparison-legend'); legend.replaceChildren();
    const models = view.metrics || [], names = Object.fromEntries(models.map(m => [m.id, m.label]));
    const series = [];
    if (!errors) series.push({id: 'actual', label: 'Werkelijk tarief (gepubliceerd)', color: '#263d43'});
    models.forEach(m => series.push({id: m.id, label: m.label, color: modelColors[m.id]}));
    for (const item of series) { const label = document.createElement('span'); label.style.setProperty('--method-color', item.color); label.textContent = item.label; legend.append(label); }
    const rows = errors ? (view.daily || []).map(r => ({time: new Date(`${r.date}T12:00:00Z`), values: r.mae, n: r.n})) : (view.points || []).map(r => ({time: new Date(r.start), issued: r.issued_at, values: {...r.predictions, actual: r.actual}}));
    const populated = rows.length && view.n;
    show('comparison-empty-state', !populated); svg.hidden = !populated; svg.style.display = populated ? 'block' : 'none'; show('comparison-legend', !!populated); show('comparison-chart-note', !!populated);
    setText('comparison-empty-state', view.selected?.length ? 'Nog geen gemeenschappelijke punten. Kies eventueel Historische reconstructie of een andere periode.' : 'Selecteer minimaal één methode.');
    setText('comparison-chart-title', errors ? 'Voorspelfout per uitgiftedag' : 'Prognoses naast werkelijke tarieven');
    setText('comparison-chart-note', errors ? `Dagelijkse MAE in ${data.unit || ''}; lager is beter. ${view.notice || ''}` : `Maximaal 192 recente doelkwartieren in ${data.unit || ''}. Beweeg over een punt voor tijd, uitgifte en tarieven. Gaten blijven zichtbaar. ${view.notice || ''}`);
    if (!populated) return;
    const width = 960, height = 310, pad = {l: 68, r: 24, t: 20, b: 44};
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('aria-label', `${errors ? 'Dagelijkse voorspelfout' : 'Tarieven en prognoses'}, ${rows.length} tijdstippen, ${Object.values(names).join(', ')}`);
    const values = rows.flatMap(r => Object.values(r.values)).filter(v => v != null && Number.isFinite(Number(v))).map(Number);
    let min = Math.min(...values), max = Math.max(...values); const spread = Math.max(max-min, .001);
    min = errors ? 0 : min-spread*.12; max += spread*.12; if (max <= min) max = min+.01;
    const left = rows[0].time.getTime(), right = rows[rows.length-1].time.getTime();
    const x = time => pad.l+(time-left)/Math.max(1,right-left)*(width-pad.l-pad.r);
    const y = value => pad.t+(max-value)/(max-min)*(height-pad.t-pad.b);
    const el = (tag,attrs={},parent=svg) => { const node=document.createElementNS('http://www.w3.org/2000/svg',tag); for(const [k,v] of Object.entries(attrs))node.setAttribute(k,String(v));parent.append(node);return node; };
    for(let i=0;i<=4;i++) { const value=min+(max-min)*i/4, yy=y(value);el('line',{x1:pad.l,x2:width-pad.r,y1:yy,y2:yy,stroke:'#e2ebea'});const label=el('text',{x:pad.l-9,y:yy+4,'text-anchor':'end',fill:'#647c7a','font-size':11});label.textContent=fmtNumber.format(value); }
    // Use actual UTC spacing so DST repeats and missing quarters remain distinct.
    const maxGap = errors ? 36*3600000 : 900000*1.5;
    for(const item of series) {
      let path='',previous=null;
      for(const row of rows) {
        const value=row.values[item.id];if(value == null || !Number.isFinite(Number(value))){previous=null;continue;}
        path += `${previous && row.time-previous<=maxGap ? 'L' : 'M'}${x(row.time)} ${y(value)} `;previous=row.time;
        const point=el('circle',{cx:x(row.time),cy:y(value),r:rows.length<20?3:2,fill:item.color,tabindex:row===rows[0]||row===rows[rows.length-1]?0:-1});
        const tip=el('title',{},point);tip.textContent=`${item.label} · ${humanTime(row.time,true)} · ${fmtPrice(value,data.unit)}${row.issued ? ` · uitgifte ${humanTime(row.issued,true)}` : ` · ${row.n} paren`}`;
      }
      el('path',{d:path,fill:'none',stroke:item.color,'stroke-width':item.id==='actual'?2.5:1.7,'stroke-dasharray':item.id==='actual'?'':'5 2','pointer-events':'none'});
    }
    const indices=new Set([0,Math.floor((rows.length-1)/3),Math.floor((rows.length-1)*2/3),rows.length-1]);
    for(const i of indices){const label=el('text',{x:x(rows[i].time),y:height-15,'text-anchor':i===0?'start':i===rows.length-1?'end':'middle',fill:'#647c7a','font-size':11});label.textContent=errors?fmtDate.format(rows[i].time):humanTime(rows[i].time,true);}
  }

  async function loadAll() {
    try { state.settings = await api('api/settings'); } catch (_) { state.settings = null; }
    try { updateStatus(await api('api/status')); } catch (error) { setConnection(false, error.message); $('load-error-text').textContent = error.message; show('load-error', true); return; }
    await loadStorage();
    await loadDashboard(); if (state.activeView === 'analysis') await loadAnalysis();
  }
  function init() {
    $('storage-open').addEventListener('click', () => { activateView('data'); $('storage-card').scrollIntoView({behavior: 'smooth'}); void loadStorage(); });
    $('storage-refresh').addEventListener('click', loadStorage);
    $('storage-days').addEventListener('change', loadStorage);
    $('storage-clean-history').addEventListener('click', () => cleanStorage('history'));
    $('storage-clean-cache').addEventListener('click', () => cleanStorage('epex_cache'));
    $('storage-clean-models').addEventListener('click', () => cleanStorage('unused_models'));
    $('data-export').addEventListener('click', exportData);
    $('history-test').addEventListener('click', testHistory);
    for (const id of ['comparison-mode', 'comparison-horizon', 'comparison-days', 'compare-local', 'compare-v4', 'compare-epex', 'compare-timestral']) $(id).addEventListener('change', () => { state.comparison = null; $('comparison-chart').replaceChildren(); $('performance-bars').replaceChildren(); setText('comparison-context', 'Vergelijking laden…'); loadMethods().catch(error => { setText('comparison-warning', error.message); show('comparison-warning', true); }); });
    $('comparison-chart-kind').addEventListener('change', renderPerformanceChart);
    $('weather-region').addEventListener('change', setWeatherFields);
    $('timestral-model').addEventListener('change', () => { updateTimestralModel(); void loadTimestralStatus(); });
    $('timestral-download').addEventListener('click', downloadTimestral);
    $('calculate-now').addEventListener('click', calculateNow); $('settings-open').addEventListener('click', openSettings); $('settings-close').addEventListener('click', () => $('settings-dialog').close()); $('settings-cancel').addEventListener('click', () => $('settings-dialog').close()); $('settings-form').addEventListener('submit', saveSettings); $('retry').addEventListener('click', loadAll); $('window-duration').addEventListener('change', loadDashboard); document.querySelectorAll('input[name="weather_mode"]').forEach(r => r.addEventListener('change', setWeatherFields));
    $('epex-profile').addEventListener('change', () => {
      if ($('epex-profile').value !== 'custom') $('price-field').value = 'tax_included';
      updateEpexProfile();
    });
    $('price-source').addEventListener('change', updatePriceSourceFields); $('supplier').addEventListener('change', supplierDefaults);
    $('dashboard-tab').addEventListener('click', () => activateView('dashboard')); $('analysis-tab').addEventListener('click', () => activateView('analysis')); $('data-tab').addEventListener('click', () => activateView('data')); $('analysis-refresh').addEventListener('click', loadAnalysis);
    for (const [index, view] of ['dashboard', 'analysis', 'data'].entries()) $(view + '-tab').addEventListener('keydown', event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); const views = ['dashboard', 'analysis', 'data']; activateView(views[(index + (event.key === 'ArrowRight' ? 1 : 2)) % 3], true); }
    });
    window.addEventListener('resize', () => { if (state.dashboard) renderChart(normalizeSlots(state.dashboard.slots)); if (state.comparison) renderPerformanceChart(); });
    loadAll(); state.timer = setInterval(loadAll, 60000);
  }
  document.addEventListener('DOMContentLoaded', init);
})();
