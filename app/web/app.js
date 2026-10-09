// Generator LOD1 — browser side.
// The local server (server.py) does the processing; this file only sends
// commands to /api/* and renders the live events from /api/events.

const $ = (id) => document.getElementById(id);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const SVGNS = 'http://www.w3.org/2000/svg';
const STORAGE_KEY = 'generator-lod1-form-v2';
const MODE_NAMES = { las: 'LAS ground', dtm: 'External DTM', auto: 'Auto DTM' };
const FLAGS = {
  low_structure: { color: 'var(--qc-low)', text: 'Under half of the points rise above the min height' },
  few_points: { color: 'var(--qc-few)', text: 'Fewer than 10 usable points' },
  vegetation_cover: { color: 'var(--qc-veg)', text: 'Tree canopy over the roof (class 5 / multi-return)' },
  edge_of_data: { color: 'var(--qc-edge)', text: 'Footprint partly outside the point cloud' },
};
const BHM_STOPS = [3, 6, 9, 12];   // class breaks in metres → 5 colours
// Mirrors main.PIPELINES, shown before the first run of a mode.
const PIPELINES = {
  las: ['Compute heights'],
  dtm: ['Read LAS', 'Heights', 'Export'],
  auto: ['Read LAS', 'Remove noise', 'Ground filter', 'Build DTM', 'Heights', 'Export'],
};

let S = null;            // last snapshot from the server
let inspect = null;      // /api/inspect result for the current input folder
let connected = false;
let lastLogId = 0;
let clockSkew = 0;       // browser clock minus server clock (s)

// ------------------------------------------------------------------ //
// Helpers
// ------------------------------------------------------------------ //
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = (n) => Number(n).toLocaleString('en-US');

function fmtDuration(sec) {
  if (!isFinite(sec)) return '--:--:--';
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return [h, m, s].map((n) => String(n).padStart(2, '0')).join(':');
}

async function api(path, body = {}) {
  const res = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  return res.json().catch(() => ({ ok: false }));
}

let toastTimer;
function toast(message, kind = '') {
  const t = $('toast');
  t.textContent = message;
  t.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = 'toast'), 6000);
}

const basename = (p) => String(p || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop();
const median = (a) => { if (!a.length) return NaN; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

// ------------------------------------------------------------------ //
// Form state
// ------------------------------------------------------------------ //
const form = {
  input: $('input'), output: $('output'), dtm: $('dtm'), dtmOffset: $('dtm-offset'),
  dtmRes: $('dtm-res'), terrain: $('terrain'), percentile: $('percentile'),
  minHeight: $('min-height'), innerBuffer: $('inner-buffer'),
};
const groundValue = () => document.querySelector('input[name="ground"]:checked').value;

function saveForm() {
  try {
    const f = { ground: groundValue() };
    for (const [k, el] of Object.entries(form)) f[k] = el.value;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(f));
  } catch (_) { /* storage unavailable */ }
}
function loadForm() {
  try {
    const f = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    for (const [k, el] of Object.entries(form)) if (f[k] != null && f[k] !== '') el.value = f[k];
    if (f.ground) $$('input[name="ground"]').forEach((r) => (r.checked = r.value === f.ground));
  } catch (_) { /* storage unavailable */ }
}

function applyMode() {
  const mode = groundValue();
  document.body.classList.remove('mode-las', 'mode-dtm', 'mode-auto');
  document.body.classList.add(`mode-${mode}`);
  const p = form.percentile.value || 95;
  $('rule-text').textContent = mode === 'las'
    ? `Legacy rule: BHM = P${p} of height above the nearest class-2 point, all points inside the footprint.`
    : `BHM = P${p} of height above the DTM inside each footprint (shrunk ${form.innerBuffer.value} m when larger than 20 m²); points below ${form.minHeight.value} m and vegetation are ignored.`;
  const unclassified = inspect && inspect.tiles.some((t) => t.ground_share !== undefined && t.ground_share < 0.01);
  $('ground-warn').hidden = !(mode === 'las' && unclassified);
  if (!(S && S.running)) renderPipeline();
}

// ------------------------------------------------------------------ //
// Input inspection (Data facts)
// ------------------------------------------------------------------ //
let inspectTimer;
function scheduleInspect() {
  clearTimeout(inspectTimer);
  inspectTimer = setTimeout(runInspect, 400);
}
async function runInspect() {
  const folder = form.input.value.trim();
  if (!folder) { inspect = null; renderFacts(); refreshMapTiles(); return; }
  const r = await api('/api/inspect', { input: folder });
  inspect = r.ok ? r : null;
  renderFacts(r.ok ? null : (r.error || 'Folder not found'));
  applyMode();
  refreshMapTiles();
  if (S) renderResults();   // output table shows CRS from the inspection
}

function renderFacts(error) {
  const facts = $('facts');
  const note = $('facts-note');
  const cells = (items) => items.map(([k, v, cls]) =>
    `<div class="fact"><span>${esc(k)}</span><b class="${cls || ''}" title="${esc(v)}">${esc(v)}</b></div>`).join('');
  if (!inspect) {
    facts.innerHTML = cells([['LAS', '—'], ['Footprints', '—'], ['CRS', '—'], ['Classified', '—']]);
    note.hidden = !error;
    note.className = 'note warn';
    note.textContent = error || '';
    return;
  }
  const t = inspect.tiles;
  const pts = t.reduce((a, x) => a + (x.points || 0), 0);
  const fps = t.reduce((a, x) => a + (x.footprints || 0), 0);
  const crs = [...new Set(t.map((x) => x.crs || 'unknown'))];
  const sampled = t.filter((x) => x.ground_share !== undefined);
  let cls = ['—', ''];
  if (sampled.length) {
    const g = Math.min(...sampled.map((x) => x.ground_share));
    const u = Math.max(...sampled.map((x) => x.unclassified_share));
    cls = g >= 0.01 ? [`Yes · ${(g * 100).toFixed(0)}% ground`, ''] : ['No ground class', 'warn'];
  }
  facts.innerHTML = cells([
    [t.length === 1 ? basename(t[0].las) : `${t.length} LAS files`, `${(pts / 1e6).toFixed(1)} M pts`],
    ['Footprints', `${nf(fps)} polygons`],
    ['CRS', crs.length === 1 ? crs[0] : `${crs.length} different`, crs.length === 1 ? '' : 'warn'],
    ['Classified', cls[0], cls[1]],
  ]);
  const msgs = [];
  if (!t.length) msgs.push('No LAS/LAZ with a GPKG in the same folder was found.');
  for (const s of inspect.skipped) msgs.push(`Skipped ${s.name}: ${s.reason}`);
  for (const x of t) if (x.error) msgs.push(`${x.label}: ${x.error}`);
  note.hidden = !msgs.length;
  note.className = 'note warn';
  note.textContent = msgs.slice(0, 4).join(' · ') + (msgs.length > 4 ? ` · +${msgs.length - 4} more` : '');
}

// ------------------------------------------------------------------ //
// Map
// ------------------------------------------------------------------ //
const mapEl = $('map');
const svg = $('map-svg');
const M = { tiles: [], idx: 0, data: null, mode: 'bhm', vb: null, home: null, paths: [], origin: [0, 0], dtmUrl: null, dtmFor: null };

function heightColor(v) {
  let i = 0;
  while (i < BHM_STOPS.length && v >= BHM_STOPS[i]) i++;
  return `var(--h${i})`;
}

function currentTiles() {
  // During/after a run on the same folder, the run's tile list (with results) wins.
  if (S && S.tiles && S.tiles.length && S.params && S.params['Input folder'] === form.input.value.trim()) return S.tiles;
  return inspect ? inspect.tiles : [];
}

function refreshMapTiles() {
  const tiles = currentTiles();
  const same = tiles.length === M.tiles.length && tiles.every((t, i) => t.las === M.tiles[i].las);
  M.tiles = tiles;
  const sel = $('tile-select');
  sel.hidden = tiles.length < 2;
  if (!same) {
    sel.innerHTML = tiles.map((t, i) => `<option value="${i}">${esc(t.label)}</option>`).join('');
    M.idx = Math.min(M.idx, Math.max(tiles.length - 1, 0));
    sel.value = M.idx;
    loadTile(M.idx);
  } else {
    recolor();
  }
}

async function loadTile(i) {
  M.data = null;
  M.paths = [];
  svg.innerHTML = '';
  M.dtmFor = null;
  const t = M.tiles[i];
  $('map-empty').hidden = false;
  $('map-empty').textContent = t ? 'Loading footprints…' : 'No footprints loaded';
  $('map-meta').textContent = t ? t.label : 'Choose an input folder to preview footprints';
  if (!t) return;
  const r = await api('/api/footprints', { las: t.las, gpkg: t.gpkg });
  if (!r.ok || M.tiles[i] !== t) { if (!r.ok) $('map-empty').textContent = 'Could not read footprints'; return; }
  M.data = r;
  const [x0, , , y1] = r.bounds;
  M.origin = [x0, y1];
  const g = document.createElementNS(SVGNS, 'g');
  g.id = 'fp-layer';
  r.features.forEach((rings, k) => {
    const d = rings.map((ring) => 'M' + ring.map(([x, y]) => `${(x - x0).toFixed(2)},${(y1 - y).toFixed(2)}`).join('L') + 'Z').join('');
    const p = document.createElementNS(SVGNS, 'path');
    p.setAttribute('d', d || 'M0,0');
    p.setAttribute('class', 'fp');
    p.dataset.i = k;
    g.appendChild(p);
    M.paths.push(p);
  });
  svg.appendChild(g);
  const w = r.bounds[2] - r.bounds[0], h = r.bounds[3] - r.bounds[1];
  const pad = Math.max(w, h) * 0.04 + 2;
  M.home = { x: -pad, y: -pad, w: w + 2 * pad, h: h + 2 * pad };
  setView({ ...M.home });
  $('map-empty').hidden = true;
  $('map-meta').textContent = `${t.label} · ${nf(r.features.length)} footprints · ${w.toFixed(0)} × ${h.toFixed(0)} m`;
  recolor();
}

function tileResult() {
  const t = M.tiles[M.idx];
  return t && S && S.results ? S.results[t.label] : null;
}

function recolor() {
  const res = tileResult();
  const hasDtm = !!(res && res.dtm);
  const dtmBtn = document.querySelector('[data-map="dtm"]');
  dtmBtn.disabled = !hasDtm;
  if (M.mode === 'dtm' && !hasDtm) M.mode = 'bhm';
  $$('[data-map]').forEach((b) => b.classList.toggle('on', b.dataset.map === M.mode));
  M.paths.forEach((p, i) => {
    const bhm = res && res.bhm ? res.bhm[i] : undefined;
    const qc = res && res.qc_flag ? res.qc_flag[i] : null;
    p.classList.remove('pending', 'outline');
    p.style.fill = '';
    if (M.mode === 'dtm') { p.classList.add('outline'); return; }
    if (M.mode === 'qc') {
      if (!res) { p.classList.add('pending'); return; }
      const first = qc && qc !== 'ok' ? qc.split(';')[0] : null;
      p.style.fill = first && FLAGS[first] ? FLAGS[first].color : 'var(--qc-ok)';
      return;
    }
    if (bhm == null) { p.classList.add('pending'); return; }
    p.style.fill = heightColor(bhm);
  });
  renderLegend();
  if (M.mode === 'dtm') showDtm(res.dtm); else hideDtm();
}

function renderLegend() {
  const lg = $('legend');
  if (!M.data) { lg.hidden = true; return; }
  lg.hidden = false;
  if (M.mode === 'qc') {
    lg.innerHTML = '<span>QC flag</span><div class="keys">' +
      Object.entries(FLAGS).map(([k, f]) => `<div class="key"><i style="background:${f.color}"></i>${k}</div>`).join('') +
      '<div class="key"><i style="background:var(--qc-ok)"></i>ok</div></div>';
  } else if (M.mode === 'dtm') {
    lg.innerHTML = '<span>DTM hillshade · footprints outlined</span>';
  } else {
    lg.innerHTML = '<span>BHM (m)</span><div class="ramp">' +
      ['--h0', '--h1', '--h2', '--h3', '--h4'].map((v) => `<span style="background:var(${v})"></span>`).join('') +
      '</div><div class="ticks"><span>0</span><span>3</span><span>6</span><span>9</span><span>12</span><span>15+</span></div>';
  }
}

async function showDtm(path) {
  const layer = svg.querySelector('#dtm-layer');
  if (M.dtmFor === path && layer) { layer.style.display = ''; return; }
  M.dtmFor = path;
  const res = await fetch('/api/dtm-preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
  });
  if (!res.ok || M.dtmFor !== path) return;
  const b = JSON.parse(res.headers.get('X-Bounds'));
  if (M.dtmUrl) URL.revokeObjectURL(M.dtmUrl);
  M.dtmUrl = URL.createObjectURL(await res.blob());
  layer?.remove();
  const img = document.createElementNS(SVGNS, 'image');
  img.id = 'dtm-layer';
  img.setAttribute('href', M.dtmUrl);
  img.setAttribute('x', b[0] - M.origin[0]);
  img.setAttribute('y', M.origin[1] - b[3]);
  img.setAttribute('width', b[2] - b[0]);
  img.setAttribute('height', b[3] - b[1]);
  img.setAttribute('preserveAspectRatio', 'none');
  svg.insertBefore(img, svg.firstChild);
  if (M.mode !== 'dtm') img.style.display = 'none';
}
function hideDtm() {
  const layer = svg.querySelector('#dtm-layer');
  if (layer) layer.style.display = 'none';
}

// pan / zoom by rewriting the viewBox
function setView(v) {
  M.vb = v;
  svg.setAttribute('viewBox', `${v.x} ${v.y} ${v.w} ${v.h}`);
}
function toWorld(clientX, clientY) {
  const pt = svg.createSVGPoint();
  pt.x = clientX; pt.y = clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}
function zoomAt(factor, clientX, clientY) {
  if (!M.vb) return;
  const r = svg.getBoundingClientRect();
  const p = toWorld(clientX ?? r.left + r.width / 2, clientY ?? r.top + r.height / 2);
  const v = M.vb;
  const w = Math.min(Math.max(v.w * factor, 5), M.home.w * 4);
  const f = w / v.w;
  setView({ x: p.x - (p.x - v.x) * f, y: p.y - (p.y - v.y) * f, w, h: v.h * f });
}
svg.addEventListener('wheel', (e) => { e.preventDefault(); zoomAt(Math.exp(e.deltaY * 0.0015), e.clientX, e.clientY); }, { passive: false });
let drag = null;
svg.addEventListener('pointerdown', (e) => {
  if (!M.vb) return;
  drag = { x: e.clientX, y: e.clientY, vb: { ...M.vb }, scale: svg.getScreenCTM().a };
  svg.setPointerCapture(e.pointerId);
  svg.classList.add('dragging');
});
svg.addEventListener('pointermove', (e) => {
  if (drag) {
    setView({ ...drag.vb, x: drag.vb.x - (e.clientX - drag.x) / drag.scale, y: drag.vb.y - (e.clientY - drag.y) / drag.scale });
    return;
  }
  const tip = $('tooltip');
  const i = e.target.dataset ? e.target.dataset.i : undefined;
  if (i === undefined) { tip.hidden = true; return; }
  const res = tileResult();
  const lines = [`Footprint #${Number(i) + 1}`];
  if (res && res.bhm) {
    const v = res.bhm[i];
    lines.push(`BHM ${v == null ? '—' : v.toFixed(2) + ' m'}`);
    if (res.n_points) lines.push(`${nf(res.n_points[i])} pts`);
    if (res.qc_flag) lines.push(res.qc_flag[i]);
  }
  tip.textContent = lines.join(' · ');
  const r = mapEl.getBoundingClientRect();
  tip.hidden = false;
  tip.style.left = `${Math.min(e.clientX - r.left + 14, r.width - tip.offsetWidth - 8)}px`;
  tip.style.top = `${e.clientY - r.top + 14}px`;
});
const endDrag = () => { drag = null; svg.classList.remove('dragging'); };
svg.addEventListener('pointerup', endDrag);
svg.addEventListener('pointercancel', endDrag);
svg.addEventListener('pointerleave', () => { $('tooltip').hidden = true; });
svg.addEventListener('dblclick', () => M.home && setView({ ...M.home }));
$('zoom-in').addEventListener('click', () => zoomAt(0.7));
$('zoom-out').addEventListener('click', () => zoomAt(1 / 0.7));
$('zoom-fit').addEventListener('click', () => M.home && setView({ ...M.home }));
$('tile-select').addEventListener('change', (e) => { M.idx = Number(e.target.value); loadTile(M.idx); });
$$('[data-map]').forEach((b) => b.addEventListener('click', () => { M.mode = b.dataset.map; recolor(); }));

// ------------------------------------------------------------------ //
// Progress, pipeline, log
// ------------------------------------------------------------------ //
function renderControls() {
  const running = !!(S && S.running);
  $('start').disabled = running || !connected;
  $('start').textContent = running ? 'Processing…' : 'Start processing';
  $('stop').disabled = !running;
  for (const el of [...Object.values(form), ...$$('input[name="ground"]'), ...$$('[data-browse]')]) el.disabled = running;
}

function renderProgress() {
  if (!S) return;
  const frac = S.progress || 0;
  $('pct').innerHTML = `${(frac * 100).toFixed(1)}<small>%</small>`;
  $('fill').style.width = `${frac * 100}%`;
  const n = (S.tiles || []).length;
  $('tile-count').textContent = n ? `${Math.min(S.tile_index + 1, n)} / ${n}` : '– / –';
  if (!S.start_time) { $('elapsed').textContent = '00:00:00'; $('eta').textContent = '--:--:--'; return; }
  const end = S.running ? Date.now() / 1000 - clockSkew : S.end_time;
  const elapsed = end - S.start_time;
  $('elapsed').textContent = fmtDuration(elapsed);
  $('eta').textContent = !S.running ? '00:00:00' : frac > 0.02 ? '~' + fmtDuration(elapsed / frac - elapsed) : '--:--:--';
}

function renderPipeline() {
  const ol = $('pipeline');
  let stages = (S && S.stages) || [];
  if (!stages.length || (S && !S.running && !S.start_time)) {
    stages = PIPELINES[groundValue()].map((label) => ({ label, status: 'queued', detail: '' }));
  }
  const now = Date.now() / 1000 - clockSkew;
  ol.innerHTML = stages.map((s) => {
    const icon = s.status === 'done' ? '<svg><use href="#i-check"/></svg>Done'
      : s.status === 'running' ? `<svg><use href="#i-spin"/></svg>${s.frac > 0 ? Math.round(s.frac * 100) + '%' : 'Running'}`
      : s.status === 'failed' ? '<svg><use href="#i-x"/></svg>Failed' : 'Queued';
    const dur = s.started ? fmtDuration((s.ended || now) - s.started) : '';
    const detail = s.status === 'done' && dur ? `${dur}${s.detail ? ' · ' + s.detail : ''}` : (s.detail || dur);
    return `<li class="stage ${s.status}"><div class="st">${icon}</div><div class="name">${esc(s.label)}</div>` +
      `<div class="detail" title="${esc(detail)}">${esc(detail) || '&nbsp;'}</div></li>`;
  }).join('');
}

const LVL = { info: 'INFO', done: 'DONE', success: 'OK', warning: 'WARN', error: 'ERROR', setting: 'SET' };
function appendLog(entry) {
  if (entry.id <= lastLogId) return;
  lastLogId = entry.id;
  const log = $('log');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const row = document.createElement('div');
  row.className = 'log-row';
  row.innerHTML = `<time>${esc(entry.t)}</time><span class="lvl ${esc(entry.level)}">${LVL[entry.level] || 'INFO'}</span><span class="msg">${esc(entry.text)}</span>`;
  log.appendChild(row);
  if (atBottom) log.scrollTop = log.scrollHeight;
}
function renderWarnBadge() {
  const n = (S && S.warnings) || 0;
  const b = $('warn-badge');
  b.hidden = !n;
  b.textContent = `${n} warning${n === 1 ? '' : 's'}`;
}

// ------------------------------------------------------------------ //
// Results + history
// ------------------------------------------------------------------ //
function renderResults() {
  const res = S && S.results ? Object.entries(S.results).filter(([, r]) => !r.error) : [];
  const done = S && !S.running && S.end_time && res.length;
  $('results-empty').hidden = !!done;
  $('results-body').hidden = !done;
  if (!done) return;

  const all = res.flatMap(([, r]) => r.bhm || []);
  const vals = all.filter((v) => v != null);
  const flags = res.flatMap(([, r]) => r.qc_flag || []);
  const hasQc = flags.length > 0;
  const offs = res.map(([, r]) => r.ground_offset).filter((v) => v != null);

  $('res-eyebrow').textContent = `${S.status} · ${MODE_NAMES[S.mode] || ''} · ${S.params.Percentile || ''}`;
  $('res-title').textContent = basename(S.params['Input folder']);
  $('st-with').innerHTML = `${nf(vals.length)}<small> / ${nf(all.length)}</small>`;
  $('st-median').innerHTML = vals.length ? `${median(vals).toFixed(2)}<small> m</small>` : '—';
  $('st-review').textContent = hasQc ? nf(flags.filter((f) => f !== 'ok').length) : '—';
  $('st-ground').innerHTML = offs.length
    ? `±${Math.max(...offs.map(Math.abs)).toFixed(2)}<small> m</small>` : '—';

  // Histogram, 1 m bands
  const maxBand = Math.min(40, Math.max(16, Math.ceil(Math.max(0, ...vals))));
  const counts = new Array(maxBand).fill(0);
  vals.forEach((v) => counts[Math.min(maxBand - 1, Math.max(0, Math.floor(v)))]++);
  const peak = Math.max(1, ...counts);
  const hist = $('hist');
  hist.style.gridTemplateColumns = `repeat(${maxBand}, minmax(0, 1fr))`;
  hist.innerHTML = counts.map((c, i) =>
    `<div style="height:${(c / peak) * 100}%;background:${heightColor(i + 0.5)}" title="${i}–${i + 1} m${i === maxBand - 1 ? '+' : ''}: ${c} footprints"></div>`).join('');
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(f * maxBand));
  $('hist-axis').innerHTML = ticks.map((t, i) => `<span>${t}${i === 0 || i === ticks.length - 1 ? ' m' : ''}</span>`).join('');

  // QC flags
  const count = {};
  flags.forEach((f) => f.split(';').forEach((k) => (count[k] = (count[k] || 0) + 1)));
  $('flags').innerHTML = hasQc
    ? Object.entries(FLAGS).map(([k, f]) =>
      `<li><i style="background:${f.color}"></i><div class="t"><b>${k}</b><small>${esc(f.text)}</small></div><span class="n">${nf(count[k] || 0)}</span></li>`).join('') +
      `<li><i style="background:var(--qc-ok)"></i><div class="t"><b>ok</b><small>No issue found</small></div><span class="n">${nf(count.ok || 0)}</span></li>`
    : '<li><div class="t"><small>QC flags are produced by the External DTM and Auto DTM modes.</small></div></li>';
  $('show-flags').hidden = !hasQc;

  // Outputs
  // The run's tile list has no CRS; the folder inspection does.
  const tileCrs = (label) => (((inspect && inspect.tiles) || []).find((t) => t.label === label) || {}).crs || '—';
  $('outputs').innerHTML = (S.outputs || []).map((o) => {
    const r = S.results[o.label] || {};
    const what = o.kind === 'geojson' ? `${nf((r.bhm || []).length)} footprints · BHM${r.n_points ? ', n_points, qc_flag' : ''}`
      : o.kind === 'dtm' ? 'Ground model from the LAS (CSF + TIN)' : 'Parameters, timings, warnings';
    const crs = o.kind === 'geojson' ? 'EPSG:4326' : o.kind === 'dtm' ? tileCrs(o.label) : '—';
    return `<tr><td class="mono">${esc(o.name)}</td><td>${esc(what)}</td><td class="mono">${esc(crs)}</td>` +
      `<td class="right"><button type="button" class="btn small" data-reveal="${esc(o.path)}">Show in Explorer</button></td></tr>`;
  }).join('');

  $('params').innerHTML = Object.entries(S.params || {}).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
}

function renderHistory() {
  const h = (S && S.history) || [];
  $('history-empty').hidden = h.length > 0;
  $('history').innerHTML = h.map((r) =>
    `<tr><td class="mono">${esc(r.started)}</td><td>${esc(r.mode)}</td><td>${esc(r.status)}</td>` +
    `<td class="mono">${nf(r.with_height)} / ${nf(r.footprints)}</td><td class="mono">${esc(r.duration)}</td>` +
    `<td class="mono" title="${esc(r.output)}">${esc(basename(r.output))}</td>` +
    `<td class="right"><button type="button" class="btn small" data-reveal="${esc(r.output)}">Open</button></td></tr>`).join('');
}

document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-reveal]');
  if (b) api('/api/reveal', { path: b.dataset.reveal });
});

function renderAll() {
  $('status').textContent = connected ? (S.status || 'Ready') : 'Disconnected — is the local server running?';
  renderControls();
  renderProgress();
  renderPipeline();
  renderWarnBadge();
  renderResults();
  renderHistory();
  refreshMapTiles();
}

// ------------------------------------------------------------------ //
// Views (hash routing)
// ------------------------------------------------------------------ //
function showView() {
  const v = (location.hash || '#run').slice(1);
  const view = ['run', 'results', 'history'].includes(v) ? v : 'run';
  for (const name of ['run', 'results', 'history']) $(`view-${name}`).hidden = name !== view;
  $$('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
}
window.addEventListener('hashchange', showView);

// ------------------------------------------------------------------ //
// Live events from the local server
// ------------------------------------------------------------------ //
function setEngine(on) {
  connected = on;
  const e = $('engine');
  e.className = `engine ${on ? 'on' : 'off'}`;
  $('engine-text').textContent = on ? `Local engine · ${location.host}` : 'Local engine offline';
}

function connect() {
  const es = new EventSource('/api/events');
  es.addEventListener('state', (e) => {
    S = JSON.parse(e.data);
    clockSkew = Date.now() / 1000 - S.now;
    setEngine(true);
    $('log').innerHTML = '';
    lastLogId = 0;
    (S.logs || []).forEach(appendLog);
    $('log').scrollTop = $('log').scrollHeight;
    renderAll();
  });
  es.addEventListener('reset', (e) => { S = { ...S, ...JSON.parse(e.data) }; renderAll(); });
  es.addEventListener('log', (e) => {
    const entry = JSON.parse(e.data);
    appendLog(entry);
    if (entry.level === 'warning' && S && S.running) { S.warnings = (S.warnings || 0) + 1; renderWarnBadge(); }
  });
  es.addEventListener('progress', (e) => {
    Object.assign(S, JSON.parse(e.data));
    renderProgress();
    renderPipeline();
  });
  es.addEventListener('status', (e) => {
    Object.assign(S, JSON.parse(e.data));
    $('status').textContent = S.status;
    renderControls();
    renderProgress();
  });
  es.addEventListener('tile', (e) => {
    const d = JSON.parse(e.data);
    S.results = { ...(S.results || {}), [d.label]: d.stats };
    recolor();
  });
  es.addEventListener('done', (e) => {
    const d = JSON.parse(e.data);
    S = { ...d.snapshot, logs: [] };
    renderAll();
    toast(d.message, d.error ? 'error' : d.status === 'Completed' ? 'success' : 'warning');
  });
  es.onerror = () => {
    // EventSource reconnects by itself; reflect the outage meanwhile.
    setEngine(false);
    $('status').textContent = 'Disconnected — is the local server running?';
    renderControls();
  };
}

setInterval(() => { if (S && S.running) { renderProgress(); renderPipeline(); } }, 1000);

// ------------------------------------------------------------------ //
// User actions
// ------------------------------------------------------------------ //
async function start() {
  saveForm();
  $('start').disabled = true;
  const r = await api('/api/start', {
    input: form.input.value.trim(),
    output: form.output.value.trim(),
    ground: groundValue(),
    percentile: Number(form.percentile.value),
    min_height: Number(form.minHeight.value),
    inner_buffer: Number(form.innerBuffer.value),
    dtm_res: Number(form.dtmRes.value),
    rigidness: Number(form.terrain.value),
    dtm: form.dtm.value.trim(),
    dtm_offset: Number(form.dtmOffset.value || 0),
  });
  if (!r.ok) {
    toast(r.error || 'Could not start processing.', 'error');
    renderControls();
  } else {
    M.mode = 'bhm';
  }
}
$('start').addEventListener('click', start);
$('stop').addEventListener('click', () => api('/api/stop'));
$('run-again').addEventListener('click', () => { location.hash = '#run'; start(); });
$('open-output').addEventListener('click', () => S && api('/api/reveal', { path: S.params['Output folder'] }));
$('show-flags').addEventListener('click', () => { location.hash = '#run'; M.mode = 'qc'; recolor(); });

$$('[data-browse]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const target = $(btn.dataset.browse);
    btn.disabled = true;
    try {
      const r = await api('/api/browse', { initial: target.value, kind: btn.dataset.kind || 'folder' });
      if (r.path) {
        target.value = r.path;
        saveForm();
        if (target === form.input) runInspect();
      }
    } finally {
      btn.disabled = !!(S && S.running);
    }
  });
});

form.input.addEventListener('input', scheduleInspect);
for (const el of Object.values(form)) el.addEventListener('change', () => { saveForm(); applyMode(); });
$$('input[name="ground"]').forEach((r) => r.addEventListener('change', () => { applyMode(); saveForm(); }));

$('copy-log').addEventListener('click', async () => {
  const text = $$('#log .log-row').map((r) => r.innerText.replace(/\n/g, ' ')).join('\n');
  try { await navigator.clipboard.writeText(text); toast('Log copied'); } catch (_) { toast('Copy failed — select the text instead', 'warning'); }
});
$('clear-log').addEventListener('click', () => { $('log').innerHTML = ''; api('/api/clear-log'); });

// ------------------------------------------------------------------ //
// Theme (dark / light). The <head> script already applied it before paint.
// ------------------------------------------------------------------ //
const THEME_KEY = 'generator-lod1-theme';
function setTheme(theme, save) {
  document.documentElement.dataset.theme = theme;
  const next = theme === 'light' ? 'dark' : 'light';
  const btn = $('theme-toggle');
  btn.setAttribute('aria-label', `Switch to ${next} mode`);
  btn.title = `Switch to ${next} mode`;
  if (save) { try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* storage unavailable */ } }
}
$('theme-toggle').addEventListener('click', () =>
  setTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light', true));
// Follow the OS setting until the user picks a theme themselves.
window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (_) { /* storage unavailable */ }
  if (!saved) setTheme(e.matches ? 'light' : 'dark', false);
});
setTheme(document.documentElement.dataset.theme || 'dark', false);

// ------------------------------------------------------------------ //
loadForm();
applyMode();
showView();
renderFacts();
runInspect();
connect();
