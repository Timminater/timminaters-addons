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
  const fmtNumber = new Intl.NumberFormat('nl-NL', { minimumFractionDigits: 3, maximumFractionDigits: 5 });
  const fmtFullDateTime = new Intl.DateTimeFormat('nl-NL', { timeZone: TZ, weekday: 'long', day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
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
      const lines = [fmtFullDateTime.format(slot.startDate), `${statusNames[slot.status]} · ${fmtPrice(slot.value, unit)}`];
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
      entries.slice(0, 3).forEach((w, i) => { const row = document.createElement('div'); row.className = 'window-item'; const rank = document.createElement('span'); rank.className = 'window-rank'; rank.textContent = String(i + 1); const desc = document.createElement('span'); const start = humanTime(w.start, true), end = humanTime(w.end, true); const strong = document.createElement('strong'); strong.textContent = `${start} – ${end}`; const small = document.createElement('small'); small.textContent = `${w.slots ?? ''}${w.slots != null ? ' kwartieren · ' : ''}${title === 'Bekend' ? 'volledig bekend' : 'bevat prognose'}`; desc.append(strong, small); const price = document.createElement('span'); price.className = 'window-price'; price.textContent = fmtPrice(w.average_price, unit); row.append(rank, desc, price); section.append(row); shown++; }); root.append(section);
    }
    if (!shown) { const empty = document.createElement('div'); empty.className = 'empty-state'; empty.textContent = 'Geen compleet, aaneengesloten venster gevonden voor deze duur.'; root.append(empty); }
  }
  function renderDashboard(data) {
    state.dashboard = data; const slots = normalizeSlots(data.slots); const current = data.current || {};
    setText('current-price', current.price == null ? '—' : fmtNumber.format(Number(current.price))); setText('current-unit', current.unit || ''); setText('current-entity', current.entity || state.settings?.tariff_entity || 'Tariefbron niet ingesteld'); setText('current-time', current.start ? `Start ${humanTime(current.start, true)}` : 'Geen huidig kwartier'); setText('current-origin', current.status === 'known' ? 'Bekend tarief' : current.status === 'predicted' ? 'Modelprognose' : 'Geen actuele waarde'); setText('current-state', statusNames[current.status] || 'Onbekend');
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
    try { show('analysis-error', false); renderAnalysis(await api('api/analysis')); }
    catch (error) { state.analysisLoaded = false; const box = $('analysis-error'); box.replaceChildren(); const text = document.createElement('span'); text.textContent = `Analyse kon niet worden geladen: ${error.message}`; box.append(text); show('analysis-error', true); }
  }
  function activateView(view, focus = false) {
    state.activeView = view; const analysis = view === 'analysis';
    $('dashboard-panel').hidden = analysis; $('analysis-panel').hidden = !analysis;
    $('dashboard-tab').classList.toggle('is-active', !analysis); $('analysis-tab').classList.toggle('is-active', analysis);
    $('dashboard-tab').setAttribute('aria-selected', String(!analysis)); $('analysis-tab').setAttribute('aria-selected', String(analysis));
    $('dashboard-tab').tabIndex = analysis ? -1 : 0; $('analysis-tab').tabIndex = analysis ? 0 : -1;
    if (focus) $(analysis ? 'analysis-tab' : 'dashboard-tab').focus();
    if (analysis && !state.analysisLoaded) void loadAnalysis();
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
    $('calculation-interval').value = String(s.calculation_interval_minutes || 5);
    $('mqtt-enabled').checked = s.mqtt_enabled === true;
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
    const settings = { tariff_entity: tariff, tariff_unit: $('tariff-unit').value, price_field: $('price-field').value, weather_source: weatherSource, weather_entities: { solar: $('solar-entity').value || null, wind: $('wind-entity').value || null, temperature: $('temperature-entity').value || null }, latitude: $('latitude').value === '' ? null : Number($('latitude').value), longitude: $('longitude').value === '' ? null : Number($('longitude').value), calculation_interval_minutes: Number($('calculation-interval').value), mqtt_enabled: $('mqtt-enabled').checked };
    if (weatherSource === 'ha' && Object.values(settings.weather_entities).some(v => !v)) { show('settings-error', true); $('settings-error').textContent = 'Kies zon, wind en temperatuur om HA-weer te gebruiken.'; return; }
    const button = $('settings-save'); button.disabled = true; button.textContent = 'Opslaan…';
    try { state.settings = await api('api/settings', { method: 'PUT', body: JSON.stringify(settings) }); $('settings-dialog').close(); toast('Instellingen opgeslagen. De app haalt de bronnen opnieuw op.'); await loadAll(); }
    catch (error) { show('settings-error', true); $('settings-error').textContent = error.message; }
    finally { button.disabled = false; button.textContent = 'Opslaan'; }
  }
  function toast(message) { const t = $('toast'); t.textContent = message; show('toast', true); setTimeout(() => show('toast', false), 3600); }
  async function calculateNow() {
    const button = $('calculate-now'); button.disabled = true; button.textContent = 'Bezig…';
    try { const result = await api('api/calculate', { method: 'POST' }); await loadAll(); toast(`Berekening gereed · ${result.points ?? 0} prognosekwartieren.`); }
    catch (error) { show('notice', true); $('notice').textContent = `Berekenen mislukt: ${error.message}`; }
    finally { button.disabled = false; button.textContent = '↻ Bereken nu'; }
  }
  async function loadAll() {
    try { state.settings = await api('api/settings'); } catch (_) { state.settings = null; }
    try { updateStatus(await api('api/status')); } catch (error) { setConnection(false, error.message); $('load-error-text').textContent = error.message; show('load-error', true); return; }
    await loadDashboard(); if (state.activeView === 'analysis') await loadAnalysis();
  }
  function init() {
    $('calculate-now').addEventListener('click', calculateNow); $('settings-open').addEventListener('click', openSettings); $('settings-close').addEventListener('click', () => $('settings-dialog').close()); $('settings-cancel').addEventListener('click', () => $('settings-dialog').close()); $('settings-form').addEventListener('submit', saveSettings); $('retry').addEventListener('click', loadAll); $('window-duration').addEventListener('change', loadDashboard); document.querySelectorAll('input[name="weather_mode"]').forEach(r => r.addEventListener('change', setWeatherFields));
    $('dashboard-tab').addEventListener('click', () => activateView('dashboard')); $('analysis-tab').addEventListener('click', () => activateView('analysis')); $('analysis-refresh').addEventListener('click', loadAnalysis);
    for (const [button, view] of [[$('dashboard-tab'), 'dashboard'], [$('analysis-tab'), 'analysis']]) button.addEventListener('keydown', event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); activateView(view === 'dashboard' ? 'analysis' : 'dashboard', true); } });
    window.addEventListener('resize', () => { if (state.dashboard) renderChart(normalizeSlots(state.dashboard.slots)); });
    loadAll(); state.timer = setInterval(loadAll, 60000);
  }
  document.addEventListener('DOMContentLoaded', init);
})();
