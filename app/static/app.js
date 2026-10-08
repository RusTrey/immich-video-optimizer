const page = document.body.dataset.page;
const replacementEnabled = document.body.dataset.replacementEnabled === 'true';
const messages = window.APP_MESSAGES || {};
const language = window.APP_LANGUAGE || 'ru';
const locale = messages['js.locale'] || 'ru-RU';
const statusGroups = window.APP_STATUSES || {blocking: [], retryable: [], discardable: [], needs_reconcile: [], attention: []};
const pendingJobs = new Set();
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function tr(key, values = {}) {
  const template = messages[key] ?? key;
  if (typeof template !== 'string') return template;
  return template.replace(/\{([a-z_]+)\}/gi, (_, name) => values[name] ?? `{${name}}`);
}

const statusLabel = (status) => messages[`status.${status}`] || status || '—';
const phaseLabel = (phase) => messages[`phase.${phase}`] || messages[`status.${phase}`] || phase || '—';
const sourceLabel = (value) => (value === 'scheduler' ? tr('source.scheduler') : tr('source.admin'));

// Server messages are Russian; in English show the phase name unless it is a diagnostic.
function runtimeMessage(message, phase, fallbackKey) {
  const diagnostic = ['failed', 'error', 'interrupted', 'replacement_interrupted', 'restore_interrupted'].includes(phase);
  if (language === 'ru' || diagnostic) return message || tr(fallbackKey);
  return phaseLabel(phase) || tr(fallbackKey);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  }[character]));
}

const svgIcon = (name) => `<svg aria-hidden="true"><use href="#icon-${name}"></use></svg>`;
const number = (value, digits = 1) => Number(value || 0).toLocaleString(locale, {maximumFractionDigits: digits});

function formatBytes(value, digits = null) {
  let size = Number(value || 0);
  const units = tr('js.byte_units');
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  const options = digits === null
    ? {maximumFractionDigits: size < 10 && unit ? 1 : 0}
    : {minimumFractionDigits: digits, maximumFractionDigits: digits};
  return `${size.toLocaleString(locale, options)} ${units[unit]}`;
}

function formatDuration(value) {
  if (value === null || value === undefined) return '—';
  const total = Math.round(Number(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

function toDate(value) {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDate(value, options = {dateStyle: 'short', timeStyle: 'short'}) {
  const date = toDate(value);
  return date ? date.toLocaleString(locale, options) : '—';
}

const formatDay = (value) => formatDate(value, {day: '2-digit', month: '2-digit', year: 'numeric'});

function relativeTime(value) {
  const date = toDate(value);
  if (!date) return '';
  const seconds = (date.getTime() - Date.now()) / 1000;
  const formatter = new Intl.RelativeTimeFormat(locale, {numeric: 'auto'});
  const absolute = Math.abs(seconds);
  if (absolute < 3600) return formatter.format(Math.round(seconds / 60), 'minute');
  if (absolute < 86400 * 2) return formatter.format(Math.round(seconds / 3600), 'hour');
  return formatter.format(Math.round(seconds / 86400), 'day');
}

const ageDays = (value) => { const date = toDate(value); return date ? (Date.now() - date.getTime()) / 86400000 : null; };
const formatSaving = (value) => (value === null || value === undefined ? '—' : `${number(value)}%`);

function statusBadge(status) {
  return `<span class="status status-${escapeHtml(status || 'unknown')}">${escapeHtml(statusLabel(status))}</span>`;
}

async function apiRequest(url, options = {}) {
  const requestOptions = {...options, headers: {'X-Optimizer-Request': '1', ...(options.headers || {})}};
  if (requestOptions.body && typeof requestOptions.body !== 'string') {
    requestOptions.headers['Content-Type'] = 'application/json';
    requestOptions.body = JSON.stringify(requestOptions.body);
  }
  const response = await fetch(url, {cache: 'no-store', ...requestOptions});
  const data = await response.json().catch(() => ({ok: false, error: `HTTP ${response.status}`}));
  if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

let toastTimer;
function toast(message, danger = false) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.toggle('danger', danger);
  element.classList.add('visible');
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => element.classList.remove('visible'), 4200);
}

function confirmAction(title, message, dangerous = true) {
  const dialog = $('#confirm-dialog');
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  $('#confirm-accept').classList.toggle('danger', dangerous);
  $('#confirm-accept').classList.toggle('warning', !dangerous);
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {once: true});
  });
}

// Polls while the tab is visible; a slow request never overlaps the next one.
function poll(callback, interval) {
  let busy = false;
  const run = async () => {
    if (busy || document.hidden) return;
    busy = true;
    try { await callback(); } finally { busy = false; }
  };
  window.setInterval(run, interval);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) run(); });
  return run();
}

const debounce = (callback, delay = 350) => {
  let timer;
  return (...args) => { window.clearTimeout(timer); timer = window.setTimeout(() => callback(...args), delay); };
};

function bindSortableHeaders(table, state, reload) {
  $$('th[data-sort]', table).forEach((header) => header.addEventListener('click', () => {
    const sort = header.dataset.sort;
    state.order = state.sort === sort && state.order === 'asc' ? 'desc' : 'asc';
    state.sort = sort;
    state.page = 1;
    reload();
  }));
}

function updateSortHeaders(table, state) {
  $$('th[data-sort]', table).forEach((header) => {
    header.dataset.direction = header.dataset.sort === state.sort ? state.order : '';
  });
}

function renderPagination(target, meta, onPage) {
  target.innerHTML = '';
  if (!meta || meta.total_pages <= 1) return;
  const add = (label, targetPage, disabled = false, active = false) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.disabled = disabled;
    button.classList.toggle('active', active);
    button.addEventListener('click', () => onPage(targetPage));
    target.append(button);
  };
  add('←', meta.page - 1, meta.page <= 1);
  const start = Math.max(1, meta.page - 2);
  const end = Math.min(meta.total_pages, meta.page + 2);
  if (start > 1) { add('1', 1); if (start > 2) target.append('…'); }
  for (let value = start; value <= end; value += 1) add(String(value), value, false, value === meta.page);
  if (end < meta.total_pages) { if (end < meta.total_pages - 1) target.append('…'); add(String(meta.total_pages), meta.total_pages); }
  add('→', meta.page + 1, meta.page >= meta.total_pages);
}

function bindChips(container, onChange) {
  container.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-filter]');
    if (!button) return;
    $$('button', container).forEach((item) => item.classList.toggle('active', item === button));
    onChange(button.dataset.filter);
  });
}

// Theme and navigation ---------------------------------------------------

function applyTheme(choice) {
  if (choice === 'black') choice = 'dark';  // the 0.9.5 preview had a separate black theme
  if (choice === 'light' || choice === 'dark') document.documentElement.dataset.theme = choice;
  else { choice = 'auto'; delete document.documentElement.dataset.theme; }
  $$('[data-theme-choice]').forEach((button) => button.classList.toggle('active', button.dataset.themeChoice === choice));
}

function initTheme() {
  let choice = 'auto';
  try { choice = localStorage.getItem('optimizer-theme') || 'auto'; } catch (_) {}
  applyTheme(choice);
  $$('[data-theme-choice]').forEach((button) => button.addEventListener('click', () => {
    applyTheme(button.dataset.themeChoice);
    try { localStorage.setItem('optimizer-theme', button.dataset.themeChoice); } catch (_) {}
  }));
}

function updateNavBadges(jobs) {
  const active = Number(jobs.queued || 0) + Number(jobs.running || 0);
  const attention = statusGroups.attention.reduce((sum, status) => sum + Number(jobs[status] || 0), 0);
  const queueBadge = $('#nav-queue-count');
  queueBadge.textContent = String(active);
  queueBadge.hidden = !active;
  const attentionBadge = $('#nav-attention');
  attentionBadge.textContent = String(attention);
  attentionBadge.hidden = !attention;
}

async function loadNavCounts() {
  try { updateNavBadges((await apiRequest('/api/runtime-status')).jobs); } catch (_) {}
}

function initShell() {
  initTheme();
  const sidebar = $('#sidebar');
  const backdrop = $('#sidebar-backdrop');
  const setMenu = (open) => {
    sidebar.classList.toggle('open', open);
    backdrop.classList.toggle('visible', open);
    document.body.classList.toggle('menu-open', open);
  };
  $('#menu-toggle').addEventListener('click', () => setMenu(!sidebar.classList.contains('open')));
  backdrop.addEventListener('click', () => setMenu(false));
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setMenu(false); });
  $$('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
  $$('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }));
}

// Jobs: cells, actions, backup expiry -------------------------------------

let schedulerSettingsPromise = null;
const schedulerSettings = () => {
  schedulerSettingsPromise ??= apiRequest('/api/settings/scheduler').then((data) => data.settings).catch(() => null);
  return schedulerSettingsPromise;
};

function backupExpiry(job, settings) {
  if (!job.replaced_at || !settings || !settings.enabled || !settings.cleanup_backups_enabled) return null;
  return new Date(new Date(job.replaced_at).getTime() + settings.backup_retention_days * 86400000);
}

function backupCell(job, settings) {
  if (job.status !== 'replaced') return '<span class="muted">—</span>';
  if (!job.backup_path) return `<span class="muted">${escapeHtml(tr('backup.deleted'))}</span>`;
  const expiry = backupExpiry(job, settings);
  if (!expiry) return escapeHtml(tr('backup.kept'));
  if (expiry.getTime() <= Date.now()) return `<span class="warn-text">${escapeHtml(tr('backup.next_run'))}</span>`;
  return `<span class="${ageDays(expiry) > -3 ? 'warn-text' : ''}">${escapeHtml(tr('backup.until', {date: formatDay(expiry)}))}</span>`;
}

function profileText(job) {
  let settings = {};
  try { settings = JSON.parse(job.encoder_settings || '{}'); } catch (_) {}
  const parts = [String(settings.encoder || job.encoder || '—').toUpperCase(), `RF ${settings.quality ?? '—'}`];
  if (settings.resolution) parts.push(settings.resolution);
  if (settings.audio_bitrate) parts.push(`AAC ${settings.audio_bitrate}`);
  if (settings.cpu_count) parts.push(`${settings.cpu_count} CPU`);
  return parts.join(' · ');
}

const canWatchOriginal = (job) => Boolean(job.backup_path) || !['replaced', 'replacing', 'replacement_interrupted'].includes(job.status);
const canWatchResult = (job) => ['ready', 'rejected_saving', 'replaced'].includes(job.status) && Boolean(job.optimized_size);

function jobFileCell(job) {
  const play = canWatchOriginal(job)
    ? `<button class="play-button" type="button" data-play-original="${job.id}" title="${escapeHtml(tr('job.watch_original'))}" aria-label="${escapeHtml(tr('job.watch_original'))}">${svgIcon('play')}</button>` : '';
  const error = job.error ? `<small class="error-line" title="${escapeHtml(job.error)}">${escapeHtml(job.error)}</small>` : '';
  return `<div class="file-cell">${play}<button class="file-name" type="button" data-open-job="${job.id}" title="${escapeHtml(job.relative_path)}">${escapeHtml(job.relative_path)}</button></div>${error}`;
}

// The current size on top; the saving and the original size in the second line.
function sizeCell(sourceSize, optimizedSize, saving) {
  if (!optimizedSize) return formatBytes(sourceSize);
  const percent = saving !== null && saving !== undefined ? `<span class="saving">−${number(saving)}%</span> ` : '';
  return `${formatBytes(optimizedSize)}<small>${percent}(${formatBytes(sourceSize)})</small>`;
}

const jobPrompts = {
  stop: ['js.prompt.stop_title', 'js.prompt.stop', true],
  reconcile: ['js.prompt.reconcile_title', 'js.prompt.reconcile', false],
  replace: ['js.prompt.replace_title', 'js.prompt.replace', false],
  retry: ['js.prompt.retry_title', 'js.prompt.retry', false],
  cancel: ['js.prompt.cancel_title', 'js.prompt.cancel', false],
  discard: ['js.prompt.discard_title', 'js.prompt.discard', true],
  restore: ['js.prompt.restore_title', 'js.prompt.restore', true],
  'delete-backup': ['js.prompt.delete_backup_title', 'js.prompt.delete_backup', true],
};
const jobRoutes = {
  replace: 'replace', retry: 'retry', cancel: 'cancel', restore: 'restore', discard: 'discard',
  stop: 'stop', reconcile: 'reconcile', 'delete-backup': 'backup/delete',
};

async function performJobAction(button, afterAction) {
  const {action, id, name} = button.dataset;
  if (pendingJobs.has(id)) return;
  const [titleKey, messageKey, danger] = jobPrompts[action];
  if (!await confirmAction(tr(titleKey), tr(messageKey, {name}), danger)) return;
  pendingJobs.add(id);
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    const result = await apiRequest(`/api/jobs/${id}/${jobRoutes[action]}`, {method: 'POST'});
    toast(result.accepted ? tr('js.accepted') : tr('js.operation_done'));
  } catch (error) { toast(error.message, true); }
  finally {
    pendingJobs.delete(id);
    afterAction();
  }
}

// Actions available for a job; `full` adds the ones shown only in the job card.
function jobActionButtons(job, {full = false, compact = true} = {}) {
  const size = compact ? 'small' : '';
  const button = (action, label, css = '') => `<button class="button ${size} ${css}" type="button" data-job-action data-action="${action}" data-id="${job.id}" data-name="${escapeHtml(job.relative_path)}">${escapeHtml(label)}</button>`;
  if (pendingJobs.has(String(job.id)) || ['replacing', 'restoring'].includes(job.status)) {
    return [`<span class="muted">${escapeHtml(tr('js.in_progress'))}</span>`];
  }
  const actions = [];
  if (canWatchResult(job) && (full || job.status === 'ready') && canWatchOriginal(job)) {
    actions.push(`<button class="button ${size}" type="button" data-compare-job="${job.id}">${svgIcon('compare')}${escapeHtml(tr('job.compare'))}</button>`);
  }
  if (job.status === 'ready') {
    if (replacementEnabled) actions.push(button('replace', tr('action.replace'), 'warning'));
    if (full || !replacementEnabled) actions.push(button('discard', tr('action.discard'), 'ghost'));
  } else if (job.status === 'running') actions.push(button('stop', tr('action.stop'), 'danger'));
  else if (statusGroups.needs_reconcile.includes(job.status)) actions.push(button('reconcile', tr('action.reconcile'), 'warning'));
  else if (statusGroups.retryable.includes(job.status)) actions.push(button('retry', tr('action.retry')), button('discard', tr('action.discard'), 'ghost'));
  else if (job.status === 'queued') actions.push(button('cancel', tr('action.cancel')));
  else if (full && job.status === 'replaced' && job.backup_path) {
    actions.push(button('restore', tr('action.restore'), 'warning'), button('delete-backup', tr('action.delete_backup'), 'danger'));
  }
  return actions;
}

// Video and A/B comparison ------------------------------------------------

const videoDialog = $('#video-dialog');
const videoStage = $('#video-stage');
const [slotA, slotB] = $$('figure', videoStage);
const [playerA, playerB] = [$('video', slotA), $('video', slotB)];
let compareMode = null;

function setCompareMode(mode) {
  compareMode = mode;
  videoStage.classList.toggle('toggle', mode === 'toggle');
  slotA.classList.remove('behind');
  slotB.classList.toggle('behind', mode === 'toggle');
  playerA.muted = false;
  playerB.muted = true;
  $$('[data-compare-mode]').forEach((button) => button.classList.toggle('active', button.dataset.compareMode === mode));
  $('#compare-swap').hidden = mode !== 'toggle';
  if (mode === 'toggle' && !playerB.paused) playerB.pause();
}

function swapCompare() {
  const [visible, hidden] = slotA.classList.contains('behind') ? [slotB, slotA] : [slotA, slotB];
  const from = $('video', visible);
  const to = $('video', hidden);
  const playing = !from.paused;
  to.currentTime = from.currentTime;
  from.pause();
  visible.classList.add('behind');
  hidden.classList.remove('behind');
  from.muted = true;
  to.muted = false;
  if (playing) to.play().catch(() => {});
}

function syncPlayers(master, follower) {
  master.addEventListener('play', () => { if (compareMode === 'side' && follower.paused) follower.play().catch(() => {}); });
  master.addEventListener('pause', () => { if (compareMode === 'side' && !follower.paused) follower.pause(); });
  master.addEventListener('seeked', () => {
    if (compareMode === 'side' && Math.abs(follower.currentTime - master.currentTime) > 0.25) follower.currentTime = master.currentTime;
  });
}
syncPlayers(playerA, playerB);
syncPlayers(playerB, playerA);
playerA.addEventListener('timeupdate', () => {
  if (compareMode === 'side' && !playerA.paused && Math.abs(playerB.currentTime - playerA.currentTime) > 0.4) playerB.currentTime = playerA.currentTime;
});

function openVideo(title, a, b = null) {
  $('#video-dialog-title').textContent = title;
  $('figcaption', slotA).textContent = b ? a.label : '';
  playerA.src = a.url;
  slotB.hidden = !b;
  $('#compare-modes').hidden = !b;
  if (b) {
    $('figcaption', slotB).textContent = b.label;
    playerB.src = b.url;
    setCompareMode(window.matchMedia('(max-width: 720px)').matches ? 'toggle' : 'side');
  } else {
    compareMode = null;
    videoStage.classList.remove('toggle');
    slotA.classList.remove('behind');
    playerA.muted = false;
    $('#compare-swap').hidden = true;
  }
  if (!videoDialog.open) videoDialog.showModal();
  if (!b) playerA.play().catch(() => {});
}

videoDialog.addEventListener('close', () => {
  [playerA, playerB].forEach((player) => { player.pause(); player.removeAttribute('src'); player.load(); });
});
$$('[data-compare-mode]').forEach((button) => button.addEventListener('click', () => setCompareMode(button.dataset.compareMode)));
$('#compare-swap').addEventListener('click', swapCompare);

const jobCache = new Map();
function rememberJobs(items) { items.forEach((job) => jobCache.set(String(job.id), job)); }

function openCompare(job) {
  const name = job.relative_path.split('/').pop();
  openVideo(name,
    {url: `/watch-original/${job.id}`, label: `${tr('job.original')} · ${formatBytes(job.source_size)}`},
    {url: `/watch-result/${job.id}`, label: `${tr('job.result')} · ${formatBytes(job.optimized_size)}`});
}

// Job card (drawer) -------------------------------------------------------

const drawer = $('#job-drawer');
let drawerJobId = null;
let refreshPage = () => {};

function validationList(job, validation) {
  const items = [];
  const add = (text, detail = '', state = '') => items.push(`<li class="${state}"><span>${escapeHtml(text)}${detail ? ` <small>${escapeHtml(detail)}</small>` : ''}</span></li>`);
  if (job.original_sha1) add(tr('check.sha1'), `${job.original_sha1.slice(0, 10)}… → ${(job.optimized_sha1 || '—').slice(0, 10)}…`);
  const video = [String(validation.video_codec || '—').toUpperCase(), `${validation.width}×${validation.height}`];
  if (validation.fps) video.push(`${number(validation.fps, 2)} ${tr('unit.fps')}`);
  if (validation.pixel_format) video.push(validation.pixel_format);
  add(tr('check.video'), video.join(' · '));
  add(tr('check.duration'), `${formatDuration(job.duration_seconds)} → ${formatDuration(validation.duration_seconds)}`);
  add(tr('check.audio'), tr('check.audio_detail', {count: validation.audio_streams ?? 0}));
  const metadata = Object.keys(validation.metadata || {});
  add(tr('check.metadata'), metadata.length ? metadata.join(', ') : tr('check.metadata_none'));
  add(tr('check.decode'), validation.full_decode === 'passed' ? tr('check.passed') : String(validation.full_decode || '—'));
  if (job.saving_percent !== null) {
    const passed = Number(job.saving_percent) >= Number(job.minimum_saving_percent);
    add(tr('check.saving', {minimum: number(job.minimum_saving_percent)}), formatSaving(job.saving_percent), passed ? '' : 'fail');
  }
  return `<ul class="checklist">${items.join('')}</ul>`;
}

function renderDrawer(job, logTail, settings) {
  jobCache.set(String(job.id), job);
  $('#job-drawer-number').textContent = `#${job.id} · ${sourceLabel(job.created_by)}${job.scheduler_run_id ? ` · ${tr('job.run', {id: job.scheduler_run_id})}` : ''}`;
  $('#job-drawer-title').textContent = job.relative_path;
  const sections = [];
  const actions = jobActionButtons(job, {full: true, compact: false});
  if (canWatchOriginal(job) && !actions.some((item) => item.includes('data-compare-job'))) {
    actions.unshift(`<button class="button" type="button" data-play-original="${job.id}">${svgIcon('play')}${escapeHtml(tr('job.watch_original'))}</button>`);
  }
  sections.push(`<section class="drawer-section"><div class="drawer-actions">${statusBadge(job.status)}</div>
    <div class="compare-line" style="margin-top:12px">
      <div><span>${escapeHtml(tr('job.before'))}</span><strong>${formatBytes(job.source_size)}</strong></div>
      <div><span>${escapeHtml(tr('job.after'))}</span><strong>${job.optimized_size ? formatBytes(job.optimized_size) : '—'}</strong></div>
      <div><span>${escapeHtml(tr('job.saving'))}</span><strong class="${job.saving_percent >= job.minimum_saving_percent ? 'positive' : ''}">${formatSaving(job.saving_percent)}</strong></div>
    </div>
    ${actions.length ? `<div class="drawer-actions" style="margin-top:12px">${actions.join('')}</div>` : ''}</section>`);
  if (job.error || job.unsupported_reason) {
    sections.push(`<section class="drawer-section">${job.error ? `<div class="alert">${escapeHtml(job.error)}</div>` : ''}${job.unsupported_reason ? `<div class="alert warning" style="margin-top:${job.error ? 8 : 0}px">${escapeHtml(job.unsupported_reason)}</div>` : ''}</section>`);
  }
  if (job.validation) {
    sections.push(`<section class="drawer-section"><h3>${escapeHtml(tr('job.checks'))}</h3>${validationList(job, job.validation)}</section>`);
  } else if (['queued', 'running'].includes(job.status)) {
    sections.push(`<section class="drawer-section"><h3>${escapeHtml(tr('job.checks'))}</h3><p class="muted">${escapeHtml(tr('job.checks_pending'))}</p></section>`);
  }
  const facts = [
    [tr('job.profile'), profileText(job)],
    [tr('job.created'), formatDate(job.created_at)],
    [tr('job.started'), formatDate(job.started_at)],
    [tr('job.finished'), formatDate(job.finished_at)],
  ];
  if (job.replaced_at) facts.push([tr('job.replaced'), formatDate(job.replaced_at)]);
  if (job.restored_at) facts.push([tr('job.restored'), formatDate(job.restored_at)]);
  if (job.attempt_count > 1) facts.push([tr('job.attempts'), String(job.attempt_count)]);
  facts.push([tr('job.source_path'), job.source_path]);
  if (job.output_path && job.status !== 'replaced') facts.push([tr('job.output_path'), job.output_path]);
  if (job.status === 'replaced') {
    const expiry = backupExpiry(job, settings);
    const backup = !job.backup_path ? tr('backup.deleted')
      : !expiry ? `${job.backup_path}\n${tr('backup.kept_long')}`
        : expiry.getTime() <= Date.now() ? `${job.backup_path}\n${tr('backup.auto_delete_due', {date: formatDay(expiry)})}`
          : `${job.backup_path}\n${tr('backup.auto_delete', {date: formatDay(expiry), relative: relativeTime(expiry)})}`;
    facts.push([tr('job.backup'), backup]);
  }
  sections.push(`<section class="drawer-section"><h3>${escapeHtml(tr('job.details'))}</h3><dl class="kv">${facts.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd style="white-space:pre-line">${escapeHtml(value)}</dd>`).join('')}</dl></section>`);
  if (logTail) {
    sections.push(`<section class="drawer-section"><details class="log"><summary>${escapeHtml(tr('job.log'))}</summary><pre>${escapeHtml(logTail)}</pre></details></section>`);
  }
  $('#job-drawer-body').innerHTML = sections.join('');
}

async function openJob(id) {
  drawerJobId = String(id);
  if (location.hash !== `#job=${id}`) history.replaceState(null, '', `#job=${id}`);
  $('#job-drawer-body').innerHTML = `<p class="drawer-section muted">${escapeHtml(tr('table.loading'))}</p>`;
  if (!drawer.open) drawer.showModal();
  try {
    const [data, settings] = await Promise.all([apiRequest(`/api/jobs/${id}`), schedulerSettings()]);
    if (drawerJobId === String(id)) renderDrawer(data.job, data.log_tail, settings);
  } catch (error) {
    $('#job-drawer-body').innerHTML = `<div class="drawer-section"><div class="alert">${escapeHtml(error.message)}</div></div>`;
  }
}

drawer.addEventListener('close', () => {
  drawerJobId = null;
  if (location.hash.startsWith('#job=')) history.replaceState(null, '', location.pathname + location.search);
});

document.addEventListener('click', (event) => {
  const action = event.target.closest('[data-job-action]');
  if (action) {
    performJobAction(action, () => { refreshPage(); if (drawerJobId) openJob(drawerJobId); });
    return;
  }
  const compare = event.target.closest('[data-compare-job]');
  if (compare) { const job = jobCache.get(compare.dataset.compareJob); if (job) openCompare(job); return; }
  const original = event.target.closest('[data-play-original]');
  if (original) {
    const job = jobCache.get(original.dataset.playOriginal);
    openVideo(tr('job.original_of', {name: job ? job.relative_path.split('/').pop() : `#${original.dataset.playOriginal}`}), {url: `/watch-original/${original.dataset.playOriginal}`});
    return;
  }
  const watch = event.target.closest('[data-watch-url]');
  if (watch) { openVideo(watch.dataset.name, {url: watch.dataset.watchUrl}); return; }
  const open = event.target.closest('[data-open-job]');
  if (open) { openJob(open.dataset.openJob); return; }
  const row = event.target.closest('tr[data-job-row]');
  if (row && !event.target.closest('button, a, input')) openJob(row.dataset.jobRow);
});

// Overview ---------------------------------------------------------------

function schedulerRules(settings) {
  const rules = [];
  if (settings.minimum_size_enabled) {
    const unit = settings.minimum_size_unit || 'gb';
    const value = unit === 'mb' ? settings.minimum_size_gb * 1024 : settings.minimum_size_gb;
    rules.push(tr('rule.size', {value: number(value, 2), unit: tr(`unit.${unit}`)}));
  }
  if (settings.minimum_bitrate_enabled) rules.push(tr('rule.bitrate', {value: number(settings.minimum_bitrate_mbps)}));
  if (settings.minimum_duration_enabled) rules.push(tr('rule.duration', {value: settings.minimum_duration_seconds}));
  if (settings.resolution_enabled) rules.push(tr('rule.resolution', {width: settings.minimum_width, height: settings.minimum_height}));
  if (settings.codec_enabled) rules.push(settings.codecs.map((codec) => codec.toUpperCase()).join('/'));
  if (settings.minimum_age_enabled) rules.push(tr('rule.age', {value: settings.minimum_age_hours}));
  if (settings.path_enabled && settings.path_contains) rules.push(tr('rule.path', {value: settings.path_contains}));
  return rules.join(' · ') || tr('rule.none');
}

function formatSlot(value, timezone) {
  const date = toDate(value);
  if (!date) return '—';
  try {
    return date.toLocaleString(locale, {weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: timezone});
  } catch (_) { return formatDate(date); }
}

function collectAttention(data) {
  const items = [];
  const add = (level, html, extra = '') => items.push({level, html, extra});
  Object.entries(data.facts.attention_jobs).forEach(([status, count]) => {
    const target = ['failed', 'interrupted', 'replacement_interrupted'].includes(status) ? '/queue' : '/journal';
    add('error', `<a href="${target}">${escapeHtml(statusLabel(status))}: ${count}</a>`);
  });
  if (data.facts.ready_with_errors) add('error', `<a href="/queue">${escapeHtml(tr('attention.ready_errors', {count: data.facts.ready_with_errors}))}</a>`);
  const lastScan = data.facts.last_scan;
  const days = lastScan ? ageDays(lastScan.finished_at) : null;
  if (data.scheduler.enabled && !data.scheduler.scan_before_run && (days === null || days >= 2)) {
    add('warning', escapeHtml(tr('attention.stale_index', {days: days === null ? '∞' : Math.floor(days)})));
  }
  if (data.backups.unattached) add('warning', escapeHtml(tr('attention.unattached', {count: data.backups.unattached, size: formatBytes(data.backups.unattached_bytes)})));
  const run = data.facts.last_scheduler_run;
  if (run && !['completed', 'running'].includes(run.status)) add('error', escapeHtml(tr('attention.run_failed', {status: statusLabel(run.status), error: run.error || ''})));
  data.events.filter((event) => event.level !== 'info' || event.source === 'recovery').forEach((event) => add(
    event.level === 'error' ? 'error' : event.level === 'warning' ? 'warning' : 'info',
    `${escapeHtml(event.message)}<small>${formatDate(event.created_at)}</small>`,
    `<button class="button small ghost" type="button" data-dismiss-event="${event.id}">${escapeHtml(tr('attention.dismiss'))}</button>`,
  ));
  return items;
}

function renderActivity(data) {
  const encode = data.encode;
  const queued = Number(data.jobs.queued || 0);
  $('#encode-activity').className = `activity-row${encode.running ? '' : ' idle'}`;
  $('#encode-activity').innerHTML = encode.running
    ? `<div class="row-head"><strong title="${escapeHtml(encode.input)}">${escapeHtml(encode.input.split('/').pop() || tr('js.job_number', {value: encode.job_id}))}</strong><span class="meta">${number(encode.progress)}%${encode.eta ? ` · ${escapeHtml(encode.eta)}` : ''}</span></div>
       <div class="bar"><i style="width:${Math.min(100, Number(encode.progress || 0))}%"></i></div>
       <p>${escapeHtml(phaseLabel(encode.phase))} · ${escapeHtml(runtimeMessage(encode.message, encode.phase, 'overview.encoding'))}${queued ? ` · ${escapeHtml(tr('overview.queued_more', {count: queued}))}` : ''}</p>`
    : `<div class="row-head"><strong>${escapeHtml(tr('overview.encoder_idle'))}</strong></div><p>${escapeHtml(queued ? tr('overview.queued_more', {count: queued}) : tr('overview.queue_empty'))}</p>`;
  const scan = data.scan;
  const lastScan = data.facts.last_scan;
  $('#scan-activity').className = `activity-row${scan.running ? '' : ' idle'}`;
  $('#scan-activity').innerHTML = scan.running
    ? `<div class="row-head"><strong>${escapeHtml(tr('overview.scanning'))}</strong><span class="meta">${scan.current} / ${scan.total || '…'}</span></div>
       <div class="bar"><i style="width:${scan.total ? Math.min(100, (scan.current / scan.total) * 100) : 0}%"></i></div>
       <p>${escapeHtml(runtimeMessage(scan.message, scan.phase, 'phase.discovering'))}</p>`
    : `<div class="row-head"><strong>${escapeHtml(tr('overview.scanner_idle'))}</strong></div><p>${escapeHtml(lastScan ? tr('overview.last_scan_line', {date: formatDate(lastScan.finished_at), relative: relativeTime(lastScan.finished_at)}) : tr('overview.never_scanned'))}</p>`;
}

function renderPlan(plan, overview) {
  const settings = plan.settings;
  const steps = $('#plan-steps');
  const lastRun = overview?.facts.last_scheduler_run;
  $('#plan-last').innerHTML = lastRun
    ? escapeHtml(tr('plan.last_run', {date: formatDate(lastRun.started_at), relative: relativeTime(lastRun.started_at), status: statusLabel(lastRun.status), added: lastRun.added, candidates: lastRun.candidates}))
    : escapeHtml(tr('plan.never_run'));
  if (!settings.enabled) {
    $('#plan-title').textContent = tr('plan.scheduler');
    $('#plan-when').textContent = tr('plan.disabled');
    steps.innerHTML = `<li class="empty-step"><span>${escapeHtml(tr('plan.disabled_hint'))} <a href="/settings#scheduler">${escapeHtml(tr('plan.enable'))}</a></span></li>`;
    return;
  }
  const next = toDate(plan.next_run);
  $('#plan-title').textContent = tr('plan.next_run');
  $('#plan-when').textContent = next ? `${formatSlot(plan.next_run, settings.timezone)} · ${relativeTime(next)} · ${settings.timezone}` : tr('plan.no_days');
  const items = [];
  const step = (state, title, text = '', extra = '') => items.push(`<li class="${state}"><strong>${escapeHtml(title)}</strong>${text ? `<p class="${state === 'warn' ? 'warn-text' : ''}">${escapeHtml(text)}</p>` : ''}${extra}</li>`);
  const list = (rows) => (rows.length ? `<div class="step-items">${rows.map((row) => `<span title="${escapeHtml(row)}">${escapeHtml(row)}</span>`).join('')}</div>` : '');

  const lastScan = overview?.facts.last_scan;
  if (settings.scan_before_run) step('ok', tr('plan.scan'), tr('plan.scan_hint'));
  else step('warn', tr('plan.no_scan'), lastScan ? tr('plan.no_scan_hint', {date: formatDay(lastScan.finished_at), relative: relativeTime(lastScan.finished_at)}) : tr('plan.no_scan_never'));

  const due = plan.backups.filter((item) => item.due);
  if (!settings.cleanup_backups_enabled) {
    step('off', tr('plan.cleanup_off'), plan.backups.length ? tr('plan.backups_kept', {count: plan.backups.length, size: formatBytes(plan.backups.reduce((sum, item) => sum + item.bytes, 0))}) : '');
  } else if (due.length) {
    step('danger', tr('plan.cleanup_due', {count: due.length, size: formatBytes(due.reduce((sum, item) => sum + item.bytes, 0))}),
      tr('plan.cleanup_due_hint', {days: settings.backup_retention_days}), list(due.map((item) => `#${item.job_id} ${item.relative_path}`)));
  } else {
    const upcoming = plan.backups.filter((item) => item.expires_at).sort((a, b) => a.expires_at.localeCompare(b.expires_at))[0];
    step('ok', tr('plan.cleanup_on', {days: settings.backup_retention_days}),
      upcoming ? tr('plan.cleanup_next', {date: formatDay(upcoming.expires_at)}) : tr('plan.cleanup_none'));
  }

  const candidates = plan.candidates;
  step(candidates.total ? 'ok' : 'off', tr('plan.select', {limit: settings.maximum_jobs_per_run}),
    `${schedulerRules(settings)}. ${candidates.total ? tr('plan.select_now', {total: candidates.total, added: candidates.will_add}) : tr('plan.select_none')}`,
    list(candidates.items.map((item) => `${formatBytes(item.size_bytes)} · ${item.relative_path}`)));

  const queued = Number(overview?.jobs.queued || 0);
  step(candidates.total || queued ? 'ok' : 'off', tr('plan.encode'), queued ? tr('plan.encode_queued', {count: queued}) : tr('plan.encode_hint'));

  if (settings.auto_replace_ready && plan.replacement_enabled) {
    step('warn', tr('plan.auto_replace'), settings.cleanup_backups_enabled ? tr('plan.auto_replace_hint_days', {days: settings.backup_retention_days}) : tr('plan.auto_replace_hint'));
  } else if (settings.auto_replace_ready) step('off', tr('plan.auto_replace'), tr('plan.auto_replace_blocked'));
  else step('ok', tr('plan.manual_replace'), tr('plan.manual_replace_hint'));
  steps.innerHTML = items.join('');
}

async function initOverview() {
  let overview = null;
  let plan = null;
  let backups = [];
  const load = async () => {
    try {
      const data = await apiRequest('/api/overview');
      overview = data;
      updateNavBadges(data.jobs);
      const saved = data.facts.saved;
      $('#kpi-saved').textContent = formatBytes(saved.bytes);
      $('#kpi-saved-detail').textContent = saved.count
        ? tr('overview.saved_detail', {count: saved.count, percent: number(saved.source_bytes ? (saved.bytes / saved.source_bytes) * 100 : 0, 0)})
        : tr('overview.saved_none');
      const attention = collectAttention(data);
      const attentionKpi = $('#kpi-attention');
      attentionKpi.textContent = String(attention.length);
      attentionKpi.closest('.kpi').classList.toggle('is-danger', attention.some((item) => item.level === 'error'));
      attentionKpi.closest('.kpi').classList.toggle('is-warning', attention.length > 0 && !attention.some((item) => item.level === 'error'));
      attentionKpi.classList.toggle('positive', !attention.length);
      $('#kpi-attention-detail').textContent = attention.length ? tr('overview.attention_below') : tr('overview.all_good');
      $('#attention').hidden = !attention.length;
      $('#attention-items').innerHTML = attention.map((item) => `<li class="level-${item.level}"><span>${item.html}</span>${item.extra}</li>`).join('');
      $('#dismiss-events').hidden = !data.events.length;
      $('#kpi-ready').textContent = String(data.jobs.ready || 0);
      $('#kpi-ready-detail').textContent = tr('overview.ready_detail', {queued: data.jobs.queued || 0, running: data.jobs.running || 0});
      const lastScan = data.facts.last_scan;
      const days = lastScan ? ageDays(lastScan.finished_at) : null;
      $('#kpi-index').textContent = lastScan ? relativeTime(lastScan.finished_at) : tr('overview.never');
      $('#kpi-index-detail').textContent = tr('overview.index_detail', {count: data.summary.count, date: lastScan ? formatDay(lastScan.finished_at) : '—'});
      $('#kpi-index').closest('.kpi').classList.toggle('is-warning', days === null || (days >= 2 && data.scheduler.enabled && !data.scheduler.scan_before_run));
      renderActivity(data);
      $('#fact-disk').textContent = tr('overview.disk_detail', {free: formatBytes(data.disk.free, 2), total: formatBytes(data.disk.total, 2)});
      $('#fact-volume').textContent = tr('overview.volume_detail', {size: formatBytes(data.summary.bytes), count: data.summary.count});
      $('#fact-backups').innerHTML = data.backups.count
        ? `${escapeHtml(tr('overview.backups_detail', {count: data.backups.count, size: formatBytes(data.backups.bytes)}))}${data.backups.unattached ? ` <span class="warn-text">${escapeHtml(tr('overview.unattached_short', {count: data.backups.unattached}))}</span>` : ''}`
        : escapeHtml(tr('overview.no_backups'));
      renderCleanupFact(data.scheduler);
      if (plan) renderPlan(plan, overview);
    } catch (error) { toast(error.message, true); }
  };
  const renderCleanupFact = (settings) => {
    const next = backups.filter((item) => item.expires_at).sort((a, b) => a.expires_at.localeCompare(b.expires_at))[0];
    $('#fact-cleanup').textContent = !settings.enabled || !settings.cleanup_backups_enabled ? tr('overview.cleanup_off')
      : next ? `${formatDay(next.expires_at)} · #${next.job_id}` : tr('overview.cleanup_nothing');
  };
  const loadPlan = async () => {
    try {
      plan = await apiRequest('/api/plan');
      backups = plan.backups;
      renderPlan(plan, overview);
      if (overview) renderCleanupFact(overview.scheduler);
    } catch (error) { toast(error.message, true); }
  };
  $('#scan-button').addEventListener('click', async () => {
    try { await apiRequest('/api/scan', {method: 'POST'}); toast(tr('js.scan_started')); load(); }
    catch (error) { toast(error.message, true); }
  });
  $('#run-scheduler-now').addEventListener('click', async () => {
    if (!await confirmAction(tr('js.run_now_title'), tr('js.run_now_message'), false)) return;
    try { await apiRequest('/api/scheduler/run-now', {method: 'POST'}); toast(tr('js.run_requested')); window.setTimeout(() => { load(); loadPlan(); }, 3000); }
    catch (error) { toast(error.message, true); }
  });
  $('#dismiss-events').addEventListener('click', async () => {
    try { await apiRequest('/api/events/dismiss-all', {method: 'POST'}); load(); }
    catch (error) { toast(error.message, true); }
  });
  $('#attention-items').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-dismiss-event]');
    if (!button) return;
    try { await apiRequest(`/api/events/${button.dataset.dismissEvent}/dismiss`, {method: 'POST'}); load(); }
    catch (error) { toast(error.message, true); }
  });
  await Promise.all([poll(load, 3000), poll(loadPlan, 60000)]);
}

// Queue ------------------------------------------------------------------

function renderCurrentJob(encode, jobs) {
  const card = $('#current-job');
  card.classList.toggle('idle', !encode.running);
  if (!encode.running) {
    const queued = Number(jobs.queued || 0);
    card.innerHTML = `<div class="now-line"><span>${escapeHtml(tr('queue.idle'))}${queued ? ` · ${escapeHtml(tr('overview.queued_more', {count: queued}))}` : ''}</span></div>`;
    return;
  }
  const name = encode.input.split('/').pop() || tr('js.job_number', {value: encode.job_id});
  card.innerHTML = `<div class="now-line">${statusBadge('running')}<strong title="${escapeHtml(encode.input)}">${escapeHtml(name)}</strong>
    <span class="meta">${escapeHtml(phaseLabel(encode.phase))} · ${number(encode.progress)}%${encode.eta ? ` · ETA ${escapeHtml(encode.eta)}` : ''}</span>
    <button class="button small" type="button" data-open-job="${encode.job_id}">${escapeHtml(tr('queue.details'))}</button>
    <button class="button small danger" type="button" data-job-action data-action="stop" data-id="${encode.job_id}" data-name="${escapeHtml(name)}">${escapeHtml(tr('action.stop'))}</button></div>
    <div class="bar"><i style="width:${Math.min(100, Number(encode.progress || 0))}%"></i></div>
    <p class="meta" style="margin-top:6px">${escapeHtml(runtimeMessage(encode.message, encode.phase, 'overview.encoding'))}</p>`;
}

async function initQueue() {
  const state = {page: 1, page_size: 50, sort: 'id', order: 'asc', status: ''};
  const table = $('.jobs-table');
  const load = async () => {
    try {
      const params = new URLSearchParams({...state, view: 'queue'});
      if (!state.status) params.delete('status');
      const [jobs, runtime] = await Promise.all([apiRequest(`/api/jobs?${params}`), apiRequest('/api/runtime-status')]);
      rememberJobs(jobs.items);
      updateNavBadges(runtime.jobs);
      renderCurrentJob(runtime.encode, runtime.jobs);
      $('#queue-total').textContent = tr('table.found', {value: jobs.pagination.total_items});
      const ready = Number(runtime.jobs.ready || 0);
      const replaceAll = $('#replace-ready');
      if (replaceAll) { replaceAll.hidden = !ready; replaceAll.textContent = tr('queue.replace_all', {count: ready}); }
      $('#queue-rows').innerHTML = jobs.items.length ? jobs.items.map((job) => `<tr data-job-row="${job.id}" class="clickable">
        <td class="num-col">${job.id}</td><td data-label="${escapeHtml(tr('table.status'))}">${statusBadge(job.status)}</td>
        <td class="file-col">${jobFileCell(job)}</td><td class="right" data-label="${escapeHtml(tr('table.size'))}">${sizeCell(job.source_size, job.optimized_size, job.saving_percent)}</td>
        <td class="nowrap" data-label="${escapeHtml(tr('table.added_by'))}">${escapeHtml(sourceLabel(job.created_by))}<small class="profile">${escapeHtml(profileText(job))}</small></td>
        <td class="actions"><div class="button-row">${jobActionButtons(job).join('')}</div></td></tr>`).join('')
        : `<tr><td colspan="6" class="empty">${escapeHtml(state.status ? tr('table.nothing_found') : tr('queue.empty'))}</td></tr>`;
      updateSortHeaders(table, state);
      renderPagination($('#queue-pagination'), jobs.pagination, (value) => { state.page = value; load(); });
    } catch (error) { toast(error.message, true); }
  };
  refreshPage = load;
  bindSortableHeaders(table, state, load);
  bindChips($('#queue-filter'), (value) => { state.status = value; state.page = 1; load(); });
  $('#replace-ready')?.addEventListener('click', async () => {
    if (!await confirmAction(tr('js.replace_all_title'), tr('js.replace_all_message'), false)) return;
    try {
      const result = await apiRequest('/api/jobs/replace-ready', {method: 'POST'});
      toast(tr('js.replace_accepted', {accepted: result.accepted.length, failed: result.failed.length}), Boolean(result.failed.length));
      load();
    } catch (error) { toast(error.message, true); }
  });
  await poll(load, 3000);
}

// Library ----------------------------------------------------------------

function unsupportedReason(video) {
  if (!String(video.container || '').startsWith('mov,mp4')) return tr('library.unsupported_container', {value: video.container || '—'});
  if (Number(video.subtitle_streams) || Number(video.data_streams) || Number(video.audio_streams) > 1) return tr('library.unsupported_streams');
  return '';
}

function videoStatus(video) {
  if (video.probe_error) return `<span class="status status-failed" title="${escapeHtml(video.probe_error)}">${escapeHtml(tr('library.probe_error'))}</span>`;
  if (video.latest_job_status) return statusBadge(video.latest_job_status);
  const unsupported = unsupportedReason(video);
  if (unsupported) return `<span class="status" title="${escapeHtml(unsupported)}">${escapeHtml(tr('library.unsupported'))}</span>`;
  if (video.classification === 'historical_handbrake') return `<span class="status">${escapeHtml(tr('library.handbrake_tag'))}</span>`;
  return `<span class="status">${escapeHtml(tr('library.untracked'))}</span>`;
}

async function initLibrary() {
  const state = {page: 1, page_size: 50, sort: 'size', order: 'desc'};
  const selected = new Set();
  const table = $('.library-table');
  const form = $('#library-filters');
  let currentItems = [];
  let currentTotal = 0;
  let scanning = false;

  const filters = () => Object.fromEntries([...new FormData(form).entries()].filter(([, value]) => value !== ''));
  const isBlocked = (video) => Boolean(video.probe_error || unsupportedReason(video) || statusGroups.blocking.includes(video.latest_job_status));
  const updateSelected = () => {
    $('#selection-bar').hidden = !selected.size;
    $('#selected-count').textContent = tr('library.selected', {value: selected.size});
    const eligible = currentItems.filter((item) => !isBlocked(item));
    const chosen = eligible.filter((item) => selected.has(item.id)).length;
    const all = $('#select-page');
    all.checked = eligible.length > 0 && chosen === eligible.length;
    all.indeterminate = chosen > 0 && chosen < eligible.length;
    all.disabled = !eligible.length;
  };
  const load = async () => {
    try {
      const data = await apiRequest(`/api/videos?${new URLSearchParams({...filters(), ...state})}`);
      currentItems = data.items;
      currentTotal = data.pagination.total_items;
      $('#library-total').textContent = tr('table.found', {value: currentTotal});
      $('#enqueue-filtered').disabled = !currentTotal;
      $('#library-rows').innerHTML = data.items.length ? data.items.map((video) => {
        const size = video.latest_job_status === 'replaced' && video.latest_source_size
          ? sizeCell(video.latest_source_size, video.size_bytes, video.latest_saving_percent) : formatBytes(video.size_bytes);
        const specs = [(video.video_codec || '—').toUpperCase(), `${video.width || '—'}×${video.height || '—'}`];
        const details = [tr('library.audio', {codec: (video.audio_codec || '—').toUpperCase()})];
        if (video.fps) details.unshift(`${Math.round(video.fps)} ${tr('unit.fps')}`);
        return `<tr><td class="check-col">${isBlocked(video) ? '' : `<input class="video-select" type="checkbox" value="${video.id}" aria-label="${escapeHtml(tr('library.select'))}" ${selected.has(video.id) ? 'checked' : ''}>`}</td>
          <td class="file-col"><div class="file-cell"><button class="play-button" type="button" data-watch-url="/watch/${video.id}" data-name="${escapeHtml(video.relative_path)}" aria-label="${escapeHtml(tr('library.watch'))}">${svgIcon('play')}</button><a class="file-name" href="/download/${video.id}" title="${escapeHtml(tr('library.download', {name: video.relative_path}))}">${escapeHtml(video.relative_path)}</a></div></td>
          <td class="right" data-label="${escapeHtml(tr('table.size'))}">${size}</td>
          <td class="nowrap" data-label="${escapeHtml(tr('table.video'))}">${escapeHtml(specs.join(' · '))}<small>${escapeHtml(details.join(' · '))}</small></td>
          <td class="right nowrap" data-label="${escapeHtml(tr('table.bitrate'))}">${video.bit_rate ? `${number(video.bit_rate / 1e6)}<small>${escapeHtml(tr('unit.mbps'))}</small>` : '—'}</td>
          <td class="right" data-label="${escapeHtml(tr('table.duration'))}">${formatDuration(video.duration_seconds)}</td>
          <td data-label="${escapeHtml(tr('table.status'))}">${videoStatus(video)}</td></tr>`;
      }).join('') : `<tr><td colspan="7" class="empty">${escapeHtml(tr('table.nothing_found'))}</td></tr>`;
      $$('.video-select').forEach((checkbox) => checkbox.addEventListener('change', () => {
        const id = Number(checkbox.value);
        if (checkbox.checked) selected.add(id); else selected.delete(id);
        updateSelected();
      }));
      updateSelected();
      updateSortHeaders(table, state);
      renderPagination($('#library-pagination'), data.pagination, (value) => { state.page = value; load(); });
    } catch (error) { toast(error.message, true); }
  };
  const loadIndex = async () => {
    try {
      const data = await apiRequest('/api/overview');
      const lastScan = data.facts.last_scan;
      const wasScanning = scanning;
      scanning = data.scan.running;
      $('#library-scan-button').disabled = scanning;
      $('#library-index').textContent = scanning
        ? tr('library.scanning', {current: data.scan.current, total: data.scan.total || '…'})
        : lastScan ? tr('library.index_line', {date: formatDate(lastScan.finished_at), relative: relativeTime(lastScan.finished_at), count: data.summary.count, size: formatBytes(data.summary.bytes)}) : tr('overview.never_scanned');
      if (wasScanning && !scanning) load();
    } catch (_) {}
  };
  const reset = () => { state.page = 1; selected.clear(); load(); };
  form.addEventListener('submit', (event) => { event.preventDefault(); reset(); });
  form.addEventListener('input', debounce(reset));
  $('#reset-filters').addEventListener('click', () => { form.reset(); reset(); });
  $('#select-page').addEventListener('change', (event) => {
    currentItems.forEach((item) => { if (!isBlocked(item)) { if (event.target.checked) selected.add(item.id); else selected.delete(item.id); } });
    $$('.video-select').forEach((checkbox) => { checkbox.checked = selected.has(Number(checkbox.value)); });
    updateSelected();
  });
  $('#clear-selection').addEventListener('click', () => { selected.clear(); $$('.video-select').forEach((checkbox) => { checkbox.checked = false; }); updateSelected(); });
  const enqueue = async (body, count) => {
    if (!await confirmAction(tr('js.enqueue_title'), tr('js.enqueue_message', {value: count}), false)) return;
    try {
      const result = await apiRequest('/api/jobs', {method: 'POST', body});
      toast(tr('js.enqueue_result', {created: result.created.length, skipped: result.skipped.length}));
      selected.clear();
      load();
      loadNavCounts();
    } catch (error) { toast(error.message, true); }
  };
  $('#enqueue-selected').addEventListener('click', () => enqueue({video_ids: [...selected]}, selected.size));
  $('#enqueue-filtered').addEventListener('click', () => enqueue({filters: filters()}, currentTotal));
  $('#library-scan-button').addEventListener('click', async () => {
    try { await apiRequest('/api/scan', {method: 'POST'}); toast(tr('js.scan_started')); loadIndex(); }
    catch (error) { toast(error.message, true); }
  });
  bindSortableHeaders(table, state, load);
  loadNavCounts();
  await Promise.all([load(), poll(loadIndex, 4000)]);
}

// Journal ----------------------------------------------------------------

async function initJournal() {
  const state = {page: 1, page_size: 50, sort: 'id', order: 'desc', filter: ''};
  const runState = {page: 1, page_size: 25};
  const table = $('.jobs-table');
  const form = $('#journal-filters');
  let settings = await schedulerSettings();

  const params = () => {
    const values = {page: state.page, page_size: state.page_size, sort: state.sort, order: state.order, view: 'journal'};
    if (state.filter === 'backup') Object.assign(values, {status: 'replaced', has_backup: '1'});
    else if (state.filter) values.status = state.filter;
    if (form.elements.created_by.value) values.created_by = form.elements.created_by.value;
    if (form.elements.query.value.trim()) values.query = form.elements.query.value.trim();
    return new URLSearchParams(values);
  };
  const loadJobs = async () => {
    if ($('dialog[open]:not(#job-drawer)')) return;
    try {
      const data = await apiRequest(`/api/jobs?${params()}`);
      rememberJobs(data.items);
      const total = data.pagination.total_items;
      $('#journal-total').textContent = tr('table.found', {value: total});
      if (!state.filter && !form.elements.created_by.value && !form.elements.query.value.trim()) $('#jobs-count').textContent = total;
      const deleteAll = $('#delete-all-backups');
      deleteAll.hidden = state.filter !== 'backup' || !total;
      deleteAll.textContent = tr('journal.delete_all_backups', {count: total});
      $('#journal-rows').innerHTML = data.items.length ? data.items.map((job) => `<tr data-job-row="${job.id}" class="clickable">
        <td class="num-col">${job.id}</td><td data-label="${escapeHtml(tr('table.status'))}">${statusBadge(job.status)}</td>
        <td class="file-col">${jobFileCell(job)}</td><td class="right" data-label="${escapeHtml(tr('table.size'))}">${sizeCell(job.source_size, job.optimized_size, job.saving_percent)}</td>
        <td data-label="${escapeHtml(tr('table.added_by'))}">${escapeHtml(sourceLabel(job.created_by))}</td>
        <td data-label="${escapeHtml(tr('table.completed'))}">${formatDate(job.restored_at || job.replaced_at || job.finished_at || job.created_at)}</td>
        <td data-label="${escapeHtml(tr('table.backup'))}">${backupCell(job, settings)}</td></tr>`).join('')
        : `<tr><td colspan="7" class="empty">${escapeHtml(tr('journal.empty'))}</td></tr>`;
      updateSortHeaders(table, state);
      renderPagination($('#journal-pagination'), data.pagination, (value) => { state.page = value; loadJobs(); });
    } catch (error) { toast(error.message, true); }
  };
  const loadRuns = async () => {
    try {
      const data = await apiRequest(`/api/scheduler-runs?${new URLSearchParams(runState)}`);
      $('#runs-count').textContent = data.pagination.total_items;
      $('#scheduler-run-rows').innerHTML = data.items.length ? data.items.map((run) => `<tr class="${run.added ? '' : 'quiet'}">
        <td class="num-col">${run.id}</td><td data-label="${escapeHtml(tr('journal.started'))}">${formatDate(run.started_at)}</td>
        <td data-label="${escapeHtml(tr('journal.run'))}">${escapeHtml(run.trigger === 'schedule' ? tr('journal.trigger_schedule') : tr('journal.trigger_manual'))}</td>
        <td data-label="${escapeHtml(tr('table.status'))}">${statusBadge(run.status)}</td>
        <td class="right" data-label="${escapeHtml(tr('journal.candidates'))}">${run.candidates}</td>
        <td class="right ${run.added ? 'positive' : 'muted'}" data-label="${escapeHtml(tr('journal.added'))}">${run.added}</td>
        <td class="file-col" data-label="${escapeHtml(tr('journal.message'))}">${run.error ? `<span class="error-line" title="${escapeHtml(run.error)}">${escapeHtml(run.error)}</span>` : '<span class="muted">—</span>'}</td></tr>`).join('')
        : `<tr><td colspan="7" class="empty">${escapeHtml(tr('journal.never_run'))}</td></tr>`;
      renderPagination($('#scheduler-runs-pagination'), data.pagination, (value) => { runState.page = value; loadRuns(); });
    } catch (error) { toast(error.message, true); }
  };
  const showTab = (tab) => {
    $$('.tabs a').forEach((link) => link.classList.toggle('active', link.dataset.tab === tab));
    $$('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== tab; });
  };
  $$('.tabs a').forEach((link) => link.addEventListener('click', (event) => {
    event.preventDefault();
    history.replaceState(null, '', `#${link.dataset.tab}`);
    showTab(link.dataset.tab);
  }));
  showTab(location.hash === '#runs' ? 'runs' : 'jobs');
  const reload = () => { state.page = 1; loadJobs(); };
  refreshPage = () => { schedulerSettingsPromise = null; schedulerSettings().then((value) => { settings = value; loadJobs(); }); };
  bindSortableHeaders(table, state, loadJobs);
  bindChips($('#journal-filter'), (value) => { state.filter = value; reload(); });
  form.addEventListener('submit', (event) => event.preventDefault());
  form.elements.created_by.addEventListener('change', reload);
  form.elements.query.addEventListener('input', debounce(reload));
  $('#delete-all-backups').addEventListener('click', async () => {
    if (!await confirmAction(tr('js.delete_all_title'), tr('js.delete_all_message'), true)) return;
    try {
      const result = await apiRequest('/api/backups/delete-all', {method: 'POST'});
      toast(tr('js.delete_all_result', {deleted: result.deleted.length, failed: result.failed.length}), Boolean(result.failed.length));
      loadJobs();
    } catch (error) { toast(error.message, true); }
  });
  loadNavCounts();
  await Promise.all([poll(loadJobs, 5000), loadRuns()]);
}

// Settings ---------------------------------------------------------------

function trackDirty(form, values) {
  let saved = JSON.stringify(values());
  const note = $('.dirty-note', form);
  const revert = $('#reset-scheduler', form);
  const update = () => {
    const dirty = JSON.stringify(values()) !== saved;
    note.hidden = !dirty;
    if (revert) revert.hidden = !dirty;
    form.dataset.dirty = dirty ? 'true' : '';
  };
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  return {markSaved: () => { saved = JSON.stringify(values()); update(); }};
}

function schedulerFormValues() {
  const form = $('#scheduler-settings');
  const checked = (name) => form.elements[name].checked;
  const value = (name) => Number(form.elements[name].value || 0);
  return {
    enabled: checked('enabled'), time: form.elements.time.value,
    timezone: form.elements.timezone.value.trim(),
    days: $$('[name="days"]:checked', form).map((item) => Number(item.value)),
    scan_before_run: checked('scan_before_run'),
    minimum_size_enabled: checked('minimum_size_enabled'),
    minimum_size_gb: form.elements.minimum_size_unit.value === 'mb' ? value('minimum_size_value') / 1024 : value('minimum_size_value'),
    minimum_size_unit: form.elements.minimum_size_unit.value,
    minimum_bitrate_enabled: checked('minimum_bitrate_enabled'), minimum_bitrate_mbps: value('minimum_bitrate_mbps'),
    minimum_duration_enabled: checked('minimum_duration_enabled'), minimum_duration_seconds: value('minimum_duration_seconds'),
    codec_enabled: checked('codec_enabled'), codecs: $$('[name="codecs"]:checked', form).map((item) => item.value),
    resolution_enabled: checked('resolution_enabled'), minimum_width: value('minimum_width'), minimum_height: value('minimum_height'),
    minimum_age_enabled: checked('minimum_age_enabled'), minimum_age_hours: value('minimum_age_hours'),
    path_enabled: checked('path_enabled'), path_contains: form.elements.path_contains.value,
    exclude_handbrake_tagged: checked('exclude_handbrake_tagged'),
    maximum_jobs_per_run: value('maximum_jobs_per_run'),
    missed_run_grace_minutes: value('missed_run_grace_minutes'),
    auto_replace_ready: checked('auto_replace_ready'),
    cleanup_backups_enabled: checked('cleanup_backups_enabled'),
    backup_retention_days: value('backup_retention_days'),
  };
}

function updateSchedulerFieldState() {
  const form = $('#scheduler-settings');
  const enabled = form.elements.enabled.checked;
  $$('[data-scheduler-part]', form).forEach((fieldset) => { fieldset.disabled = !enabled; });
  form.elements.backup_retention_days.disabled = !enabled || !form.elements.cleanup_backups_enabled.checked;
  $$('[data-state-for]', form).forEach((badge) => {
    const on = form.elements[badge.dataset.stateFor].checked;
    badge.textContent = on ? tr('settings.state_on') : tr('settings.state_off');
    badge.classList.toggle('on', on && enabled);
  });
}

function fillSchedulerForm(settings) {
  const form = $('#scheduler-settings');
  Object.entries(settings).forEach(([name, value]) => {
    if (name === 'days') $$('[name="days"]', form).forEach((item) => { item.checked = value.includes(Number(item.value)); });
    else if (name === 'codecs') $$('[name="codecs"]', form).forEach((item) => { item.checked = value.includes(item.value); });
    else if (form.elements[name]) {
      if (typeof value === 'boolean') form.elements[name].checked = value;
      else form.elements[name].value = value;
    }
  });
  const unit = settings.minimum_size_unit || 'gb';
  form.elements.minimum_size_unit.value = unit;
  form.elements.minimum_size_value.value = unit === 'mb' ? Number((settings.minimum_size_gb * 1024).toFixed(3)) : settings.minimum_size_gb;
  updateSchedulerFieldState();
}

const resolutionDimensions = {
  '720p': ['1280×720', '720×1280'], '1080p': ['1920×1080', '1080×1920'],
  '1440p': ['2560×1440', '1440×2560'], '2160p': ['3840×2160', '2160×3840'],
};

function encodingFormValues() {
  const form = $('#encoding-profile');
  return {
    encoder: form.elements.encoder.value, quality: Number(form.elements.quality.value),
    resolution: form.elements.resolution.value, audio_bitrate: Number(form.elements.audio_bitrate.value),
    cpu_count: Number(form.elements.cpu_count.value), priority: form.elements.priority.value,
  };
}

function updateEncodingHints() {
  const form = $('#encoding-profile');
  $('#quality-value').textContent = `RF ${form.elements.quality.value}`;
  const [landscape, portrait] = resolutionDimensions[form.elements.resolution.value];
  $('#resolution-hint').textContent = tr('settings.resolution_hint', {landscape, portrait});
}

function fillEncodingForm(settings, options) {
  const form = $('#encoding-profile');
  const cpu = form.elements.cpu_count;
  cpu.innerHTML = '';
  for (let value = 1; value <= Number(options.available_cpus); value += 1) cpu.add(new Option(tr('settings.cpu_option', {value}), String(value)));
  Object.entries(settings).forEach(([name, value]) => { if (form.elements[name]) form.elements[name].value = value; });
  $('#cpu-hint').textContent = tr('settings.cpu_available', {value: options.available_cpus});
  updateEncodingHints();
}

async function initSettings() {
  const encodingForm = $('#encoding-profile');
  const form = $('#scheduler-settings');
  const encodingDirty = trackDirty(encodingForm, encodingFormValues);
  const schedulerDirty = trackDirty(form, schedulerFormValues);
  let savedScheduler = null;
  try { const data = await apiRequest('/api/settings/encoding'); fillEncodingForm(data.settings, data.options); encodingDirty.markSaved(); }
  catch (error) { toast(error.message, true); }
  try { const data = await apiRequest('/api/settings/scheduler'); savedScheduler = data.settings; fillSchedulerForm(savedScheduler); schedulerDirty.markSaved(); }
  catch (error) { toast(error.message, true); }
  encodingForm.addEventListener('input', updateEncodingHints);
  encodingForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const data = await apiRequest('/api/settings/encoding', {method: 'PUT', body: encodingFormValues()});
      fillEncodingForm(data.settings, data.options);
      encodingDirty.markSaved();
      toast(tr('settings.profile_saved'));
    } catch (error) { toast(error.message, true); }
  });
  form.addEventListener('change', updateSchedulerFieldState);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const data = await apiRequest('/api/settings/scheduler', {method: 'PUT', body: schedulerFormValues()});
      savedScheduler = data.settings;
      fillSchedulerForm(savedScheduler);
      schedulerDirty.markSaved();
      toast(tr('settings.saved'));
    } catch (error) { toast(error.message, true); }
  });
  $('#reset-scheduler').addEventListener('click', () => { if (savedScheduler) { fillSchedulerForm(savedScheduler); schedulerDirty.markSaved(); } });
  $('#preview-scheduler').addEventListener('click', async () => {
    try {
      const {preview} = await apiRequest('/api/scheduler/preview', {method: 'POST', body: schedulerFormValues()});
      $('#preview-summary').textContent = tr('settings.preview_summary', {total: preview.total, added: Math.min(preview.total, Number(form.elements.maximum_jobs_per_run.value || 0))});
      const list = $('#preview-items');
      list.hidden = !preview.items.length;
      list.innerHTML = preview.items.map((item) => `<div><span title="${escapeHtml(item.relative_path)}">${escapeHtml(item.relative_path)}</span><strong>${formatBytes(item.size_bytes)} · ${number(item.bit_rate / 1e6)} ${escapeHtml(tr('unit.mbps'))}</strong></div>`).join('');
    } catch (error) { toast(error.message, true); }
  });
  window.addEventListener('beforeunload', (event) => {
    if (encodingForm.dataset.dirty || form.dataset.dirty) event.preventDefault();
  });
}

// Start ------------------------------------------------------------------

initShell();
if (page === 'overview') initOverview();
else if (page === 'queue') initQueue();
else if (page === 'library') initLibrary();
else if (page === 'journal') initJournal();
else if (page === 'settings') { initSettings(); loadNavCounts(); }
const hashJob = location.hash.match(/^#job=(\d+)$/);
if (hashJob) openJob(hashJob[1]);
