import { bytes, filterGroups, totals, markExtras, selectFile, pruneSelection, selectionPlans, runDeletionPlans } from './model.mjs';

const $ = id => document.getElementById(id);
const state = { groups: [], filtered: [], revision: null, selected: null, page: 0, view: 0, controller: null, urls: [], mode: 'all', scope: null, loadId: 0, savedFilters: {}, selections: { all: new Map(), folder: new Map() }, selectionRevisions: {} };
const filterIds = ['search', 'type', 'min', 'max', 'copies', 'sort'];
const folder = { path: '', loaded: false, browseId: 0, running: false, poll: null };
const scans = { all: { info: { status: 'idle' }, timer: null, note: '', noteError: false }, folder: { info: { status: 'idle' }, timer: null, note: '', noteError: false } };
const isScanning = (mode = state.mode) => scans[mode].info.status === 'running';
const pageSize = 30;
const formatDate = value => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? value || 'Unavailable' : date.toLocaleString(); };
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
const svgIcon = paths => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const ICONS = {
  image: svgIcon('<rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="9" cy="9" r="1.9"/><path d="m21 15-3.4-3.4a2 2 0 0 0-2.8 0L7 19.4"/>'),
  video: svgIcon('<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m10.2 9.4 4.8 2.6-4.8 2.6z" fill="currentColor" stroke="none"/>'),
  document: svgIcon('<path d="M14 2.5H6.5a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V8z"/><path d="M14 2.5V8h5.5"/><path d="M9 13h6M9 16.6h6"/>'),
  file: svgIcon('<path d="M14 2.5H6.5a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V8z"/><path d="M14 2.5V8h5.5"/>'),
  copies: svgIcon('<rect x="3.5" y="3.5" width="10.5" height="10.5" rx="2.8" stroke-opacity=".6"/><rect x="10" y="10" width="10.5" height="10.5" rx="2.8" fill="currentColor" stroke="none" fill-opacity=".18"/><path d="m13.2 15.7 2 2 3.6-3.7"/>'),
  folder: svgIcon('<path d="M3.5 6.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.6.8l1.2 1.5a2 2 0 0 0 1.6.7h5a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>'),
};
function iconNode(name, className = 'icon') { const node = element('span', className); node.innerHTML = ICONS[name]; return node; }
function notify(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
const selection = () => state.selections[state.mode];
let refreshCurrentSelection = () => {};

function updateBulkToolbar() {
  const plans = selectionPlans(state.groups, selection());
  const files = plans.flatMap(plan => plan.remove);
  const visible = new Set(state.filtered.map(group => group.id));
  const hidden = plans.filter(plan => !visible.has(plan.group)).reduce((sum, plan) => sum + plan.remove.length, 0);
  $('bulk-count').textContent = files.length ? `${files.length.toLocaleString()} copies selected · ${bytes(files.reduce((sum, file) => sum + file.size, 0))} · ${plans.length.toLocaleString()} groups` : 'No copies selected';
  $('bulk-scope').textContent = hidden ? `${hidden.toLocaleString()} selected copies are outside the current filters. Review includes every selected copy in this tab.` : 'Selections stay marked across groups and filters in this tab.';
  $('clear-selection').disabled = !files.length;
  $('review-bulk').disabled = !files.length || isScanning();
  $('review-bulk').textContent = files.length ? `Review selected copies (${files.length.toLocaleString()})` : 'Review selected copies';
  $('mark-page').disabled = !state.filtered.length;
  $('mark-filtered').disabled = !state.filtered.length;
  $('mark-filtered').textContent = `Mark extras in filtered results (${state.filtered.length.toLocaleString()} groups)`;
}

function selectionChanged() { refreshCurrentSelection(); renderList(); }

function markGroups(groups) {
  const result = markExtras(groups, selection(), $('keep-rule').value);
  selectionChanged();
  notify(`Extra copies marked in ${result.marked.toLocaleString()} groups.${result.skipped ? ` ${result.skipped} groups could not use this keep rule and were skipped.` : ''} Review the paths before deleting.`);
}

function reviewSelection(groups = state.groups) {
  const plans = selectionPlans(groups, selection());
  if (!plans.length) return;
  const mode = state.mode, scope = state.scope, revision = state.revision, queue = selection();
  const count = plans.reduce((sum, plan) => sum + plan.remove.length, 0);
  const dialog = element('dialog', 'delete-dialog');
  const title = element('h2', '', `Move ${count.toLocaleString()} copies to the Recycle Bin?`);
  title.id = 'delete-dialog-title'; dialog.setAttribute('aria-labelledby', title.id);
  const explanation = element('p', 'dialog-intro', `${plans.length.toLocaleString()} groups · ${bytes(plans.reduce((sum, plan) => sum + plan.remove.reduce((size, file) => size + file.size, 0), 0))}. Selected copies are removed from OneDrive and synced devices. At least one copy in every group will be kept. Keep this window open while deletion runs.`);
  const review = element('div', 'bulk-review-groups');
  let shown = 0;
  const more = element('button', 'secondary', 'Show more groups');
  function addReviews() {
    const end = Math.min(shown + 50, plans.length);
    for (; shown < end; shown++) {
      const plan = plans[shown];
      const details = element('details', 'review-group');
      const summary = element('summary', '', `${plan.remove.length} to remove · ${plan.keep.length} to keep · ${plan.keep[0].name}`);
      details.append(summary);
      let populated = false;
      function populate() {
        if (populated || !details.open) return;
        populated = true;
        for (const [heading, files, css] of [['Remove these copies', plan.remove, 'remove-paths'], ['Keep these copies', plan.keep, 'keep-paths']]) {
          const list = element('ul', 'review-paths ' + css);
          for (const file of files) list.append(element('li', '', `${file.path} · ${bytes(file.size)}`));
          details.append(element('h3', '', heading), list);
        }
      }
      details.addEventListener('toggle', populate);
      if (plans.length === 1) { details.open = true; populate(); }
      review.append(details);
    }
    more.hidden = shown >= plans.length;
    more.textContent = `Show more groups (${plans.length - shown} remaining)`;
  }
  more.addEventListener('click', addReviews); addReviews();
  const status = element('p', 'dialog-status', 'Expand a group to review the exact paths to remove and keep. Files are rechecked before deletion.'); status.setAttribute('role', 'status');
  const progress = element('progress'); progress.max = count; progress.value = 0; progress.hidden = true; progress.setAttribute('aria-label', 'Copies moved to Recycle Bin');
  const actions = element('div', 'dialog-actions');
  const cancel = element('button', 'secondary', 'Cancel');
  const confirm = element('button', 'danger-button', `Move ${count.toLocaleString()} selected copies to Recycle Bin`);
  actions.append(cancel, confirm); dialog.append(title, explanation, review, more, status, progress, actions);
  let deleting = false, stopRequested = false;
  cancel.addEventListener('click', () => {
    if (deleting) { stopRequested = true; cancel.disabled = true; status.textContent += ' Stopping after the current group finishes.'; }
    else dialog.close();
  });
  dialog.addEventListener('cancel', event => { if (deleting) event.preventDefault(); });
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  confirm.addEventListener('click', async () => {
    if (deleting) return;
    deleting = true; confirm.disabled = true; cancel.textContent = 'Stop after this group'; progress.hidden = false;
    const result = await runDeletionPlans(plans, {
      shouldStop: () => stopRequested,
      send: async (plan, completed) => {
        status.textContent = `${progress.value} of ${count} copies moved. Checking group ${completed + 1} of ${plans.length}…`;
        return (await request('/api/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ view: mode, revision, group: plan.group, files: plan.remove.map(file => file.id), confirmRecycleBin: true }) })).json();
      },
      onResult: (plan, result, removed) => {
        for (const file of result.deleted) queue.get(plan.group)?.delete(file.id);
        if (!queue.get(plan.group)?.size) queue.delete(plan.group);
        progress.value = removed;
      },
    });
    const outcome = `${result.deleted.length.toLocaleString()} of ${count.toLocaleString()} copies moved to OneDrive’s Recycle Bin.${result.stopped ? ' Stopped before the next group. Remaining copies were left untouched.' : ''}${result.error ? ' ' + result.error : ' The other copies were kept.'}`;
    deleting = false; dialog.close();
    await reload();
    await startRescan(mode, scope, outcome, Boolean(result.error));
  });
  document.body.append(dialog); dialog.showModal(); cancel.focus();
}

async function request(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || 'Could not load this preview. Try again.');
  }
  return response;
}

function applyFilters() {
  const filters = Object.fromEntries(filterIds.map(id => [id, $(id).value]));
  const invalid = Number(filters.min) < 0 || Number(filters.max) < 0 || (filters.min !== '' && filters.max !== '' && Number(filters.min) > Number(filters.max));
  if (invalid) { notify('Use a maximum size that is at least the minimum size.', true); return; }
  notify('');
  state.filtered = filterGroups(state.groups, filters);
  state.page = 0;
  const summary = totals(state.filtered);
  $('group-count').textContent = summary.groups.toLocaleString();
  $('extra-count').textContent = summary.extra.toLocaleString();
  $('savings').textContent = bytes(summary.savings);
  $('result-count').textContent = `${summary.groups.toLocaleString()} groups`;
  if (state.selected && !state.filtered.some(group => group.id === state.selected)) clearComparison();
  renderList();
}

function renderList() {
  const list = $('group-list');
  list.replaceChildren();
  const start = state.page * pageSize;
  for (const group of state.filtered.slice(start, start + pageSize)) {
    const first = group.files[0];
    const row = element('div', 'group-row');
    const mark = element('input', 'group-mark'); mark.type = 'checkbox';
    const selectedCount = selection().get(group.id)?.size || 0;
    mark.checked = selectedCount === group.files.length - 1; mark.indeterminate = selectedCount > 0 && !mark.checked; mark.disabled = !group.sameSize;
    mark.setAttribute('aria-label', `Mark extra copies in group: ${first.name}`);
    mark.addEventListener('change', () => { if (mark.checked) markGroups([group]); else { selection().delete(group.id); selectionChanged(); } });
    const button = element('button', 'group-item');
    button.classList.toggle('selected', state.selected === group.id);
    button.setAttribute('aria-pressed', String(state.selected === group.id));
    const icon = element('span', `file-icon ${first.type}`); icon.innerHTML = ICONS[first.type] || ICONS.file;
    const details = element('span', 'group-label');
    const name = element('strong', '', first.name); name.title = first.path;
    details.append(name, element('span', 'group-meta', `${group.files.length} copies · ${bytes(group.size)} each`));
    const potential = element('span', 'group-saving', `${bytes(group.savings)} extra${selectedCount ? ` · ${selectedCount} selected` : ''}`);
    button.append(icon, details, potential);
    button.addEventListener('click', () => selectGroup(group));
    row.append(mark, button); list.append(row);
  }
  if (!state.filtered.length) { const empty = element('div', 'no-results'); empty.append(element('h3', '', 'No groups match these filters'), element('p', '', 'Try a broader size range or clear the search.')); list.append(empty); }
  const pages = Math.max(1, Math.ceil(state.filtered.length / pageSize));
  $('page-count').textContent = `${state.page + 1} / ${pages}`;
  $('previous').disabled = state.page === 0;
  $('next').disabled = state.page + 1 >= pages;
  updateBulkToolbar();
}

function clearComparison() {
  state.controller?.abort(); state.controller = null;
  state.urls.forEach(url => URL.revokeObjectURL(url)); state.urls = [];
  state.selected = null; state.view++;
  refreshCurrentSelection = () => {};
  const empty = element('div', 'empty-state');
  const emptyIcon = element('div', 'empty-icon'); emptyIcon.innerHTML = ICONS.copies;
  empty.append(emptyIcon, element('h2', '', 'Choose a group to compare'), element('p', '', 'Previews load only for the copies you open.'));
  $('comparison').replaceChildren(empty);
}

function addDetail(list, label, value) {
  const row = element('div', 'detail-row'); row.append(element('dt', '', label), element('dd', '', value || 'Unavailable')); list.append(row);
}

async function copyPath(path, button) {
  try { await navigator.clipboard.writeText(path); button.textContent = 'Copied'; setTimeout(() => { button.textContent = 'Copy path'; }, 1500); }
  catch { button.textContent = 'Select the path above to copy'; }
}

function selectGroup(group) {
  clearComparison();
  state.selected = group.id;
  state.controller = new AbortController();
  const view = state.view;
  const mode = state.mode;
  renderList();
  const header = element('div', 'comparison-heading');
  const title = element('div'); title.append(element('div', 'eyebrow', 'COMPARE COPIES'), element('h2', '', group.files[0].name));
  const badge = element('span', group.sameSize ? 'match-badge' : 'match-badge warning', group.sameSize ? '✓ Same size + checksum' : 'Different sizes · review carefully');
  header.append(title, badge);
  const helper = element('p', 'comparison-help', `${group.files.length} copies · ${bytes(group.savings)} in extra copies. Previews are reduced-size thumbnails, not the original image quality.`);
  const comparisonResult = element('p', 'resolution-status', 'Load previews to compare the original resolutions reported by OneDrive.');
  const grid = element('div', 'copy-grid');
  const compare = $('comparison'); compare.replaceChildren(header, helper, comparisonResult, grid);
  const loaded = new Map();
  const queue = state.selections[mode];
  const selectedIds = () => queue.get(group.id) || new Set();
  const checkboxes = new Map();
  const selectionBar = element('div', 'selection-bar');
  const selectionText = element('p', '', 'Keep the copy in the folder you prefer. Select individual copies below to remove.');
  const reviewButton = element('button', 'danger-button', 'Review deletion'); reviewButton.disabled = true;
  selectionBar.append(selectionText, reviewButton); compare.insertBefore(selectionBar, grid);
  function updateSelection() {
    const selected = selectedIds();
    selectionText.textContent = selected.size ? `${selected.size} selected in this group · ${group.files.length - selected.size} will be kept` : 'Choose copies below, or use “Keep this copy” to mark the others.';
    reviewButton.disabled = !selected.size || isScanning(mode);
    reviewButton.textContent = selected.size ? `Review this group (${selected.size})` : 'Review this group';
    for (const [id, checkbox] of checkboxes) {
      checkbox.checked = selected.has(id);
      checkbox.disabled = !group.sameSize || (!selected.has(id) && selected.size >= group.files.length - 1);
      checkbox.closest('article').classList.toggle('marked-for-deletion', selected.has(id));
    }
  }
  refreshCurrentSelection = updateSelection;
  const markGroup = element('button', 'secondary', 'Mark extras in this group'); markGroup.addEventListener('click', () => markGroups([group]));
  const clearGroup = element('button', 'text-button', 'Clear group'); clearGroup.addEventListener('click', () => { queue.delete(group.id); selectionChanged(); });
  selectionBar.append(markGroup, clearGroup);
  reviewButton.addEventListener('click', () => reviewSelection([group]));
  let shown = 0;
  const loadMore = element('button', 'secondary more-copies', 'Show more copies');
  function addCopies(count) {
    const end = Math.min(group.files.length, shown + count);
    for (; shown < end; shown++) {
      const index = shown, file = group.files[index];
      const card = element('article', 'copy-card');
      const top = element('div', 'copy-heading'); top.append(element('span', 'copy-label', `Copy ${index + 1}`), element('span', 'file-type', file.name.includes('.') ? file.name.split('.').pop().toUpperCase() : 'FILE'));
      const preview = element('div', 'preview'); preview.append(element('span', 'preview-placeholder loading', 'Loading OneDrive preview…'));
      const info = element('div', 'copy-info');
      const name = element('h3', '', file.name);
      const path = element('p', 'file-path', file.path); path.title = file.path;
      const details = element('dl', 'details'); addDetail(details, 'File size', `${bytes(file.size)} · ${file.size.toLocaleString()} bytes`); addDetail(details, 'Modified at scan', formatDate(file.modified));
      const more = element('dl', 'details live-details');
      const status = element('p', 'file-status', 'Fetching metadata only…'); status.setAttribute('role', 'status');
      const buttons = element('div', 'copy-actions');
      const copy = element('button', 'secondary', 'Copy path'); copy.addEventListener('click', () => copyPath(file.path, copy));
      const retry = element('button', 'text-button', 'Retry preview'); retry.hidden = true;
      const keep = element('button', 'secondary', 'Keep this copy');
      keep.disabled = !group.sameSize;
      keep.addEventListener('click', () => { queue.set(group.id, new Set(group.files.filter(other => other.id !== file.id).map(other => other.id))); selectionChanged(); });
      buttons.append(copy, keep, retry);
      const markLabel = element('label', 'delete-choice');
      const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.disabled = true;
      checkbox.setAttribute('aria-label', `Move copy ${index + 1} to Recycle Bin: ${file.path}`);
      checkbox.addEventListener('change', () => { selectFile(group, queue, file.id, checkbox.checked); selectionChanged(); });
      markLabel.append(checkbox, element('span', '', 'Move this copy to Recycle Bin'));
      checkboxes.set(file.id, checkbox);
      info.append(name, path, details, more, status, buttons); card.append(top, preview, markLabel, info); grid.append(card); updateSelection();
      let busy = false;
      async function load() {
        if (busy || state.view !== view) return;
        busy = true; retry.hidden = true; status.classList.remove('error'); status.textContent = 'Fetching metadata only…';
        preview.replaceChildren(element('span', 'preview-placeholder loading', 'Loading OneDrive preview…'));
        const params = new URLSearchParams({ group: group.id, file: file.id, view: mode });
        try {
          const data = await (await request(`/api/file?${params}`, { signal: state.controller.signal })).json();
          if (state.view !== view) return;
          more.replaceChildren();
          addDetail(more, 'Original resolution', data.width && data.height ? `${data.width.toLocaleString()} × ${data.height.toLocaleString()} px` : 'Not supplied by OneDrive');
          if (data.duration) addDetail(more, 'Video duration', `${(data.duration / 1000).toFixed(1)} seconds`);
          if (data.bitrate) addDetail(more, 'Video bitrate', `${(data.bitrate / 1e6).toFixed(2)} Mbps`);
          if (data.taken) addDetail(more, 'Captured', formatDate(data.taken));
          if (data.camera) addDetail(more, 'Camera', data.camera);
          if (data.mime) addDetail(more, 'Format', data.mime);
          if (data.modified) addDetail(more, 'Current modified date', formatDate(data.modified));
          status.textContent = data.changedSinceScan ? 'File changed since the scan. Run a fresh scan before deciding.' : 'Current file still matches the scan size and available checksum.';
          status.classList.toggle('error', data.changedSinceScan);
          if (data.webUrl && !buttons.querySelector('a')) { const link = element('a', 'text-button', 'Open in OneDrive ↗'); link.href = data.webUrl; link.target = '_blank'; link.rel = 'noopener noreferrer'; buttons.append(link); }
          loaded.set(file.id, data); updateSelection();
          const loadedValues = [...loaded.values()];
          const dimensions = loadedValues.filter(item => item.width && item.height);
          if (loadedValues.some(item => item.changedSinceScan)) comparisonResult.textContent = 'At least one file has changed since the scan. Refresh the duplicate scan before deciding.';
          else if (dimensions.length >= 2) { const same = dimensions.every(item => item.width === dimensions[0].width && item.height === dimensions[0].height); comparisonResult.textContent = same ? `The ${dimensions.length} loaded copies have the same reported original resolution. Size and checksum also match in this scan.` : 'The loaded copies have different reported resolutions. Review them carefully in OneDrive.'; }
          else comparisonResult.textContent = 'Original resolution is shown where OneDrive provides it. A thumbnail cannot prove full image quality.';
          if (data.hasThumbnail) {
            const response = await request(`/api/thumbnail?${params}`, { signal: state.controller.signal });
            const blob = await response.blob();
            if (state.view !== view) return;
            const url = URL.createObjectURL(blob); state.urls.push(url);
            const image = element('img'); image.src = url; image.alt = `OneDrive thumbnail of ${file.name}`;
            const caption = element('span', 'preview-caption', data.thumbnailDimensions?.width ? `${data.thumbnailDimensions.width} × ${data.thumbnailDimensions.height} preview` : 'OneDrive thumbnail');
            if (data.webUrl) {
              const link = element('a', 'preview-link'); link.href = data.webUrl; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.title = 'Open this copy in OneDrive'; link.setAttribute('aria-label', `Open copy ${index + 1} in OneDrive: ${file.name}`); link.append(image, caption); preview.replaceChildren(link);
            } else preview.replaceChildren(image, caption);
          } else {
            const placeholder = element('span', 'preview-placeholder', 'No thumbnail supplied by OneDrive');
            if (data.webUrl) { const link = element('a', 'preview-link', 'Open this copy in OneDrive ↗'); link.href = data.webUrl; link.target = '_blank'; link.rel = 'noopener noreferrer'; preview.replaceChildren(link); }
            else preview.replaceChildren(placeholder);
          }
        } catch (error) {
          if (error.name === 'AbortError' || state.view !== view) return;
          status.textContent = error.message; status.classList.add('error'); retry.hidden = false;
          preview.replaceChildren(element('span', 'preview-placeholder', 'Preview unavailable'));
        } finally { busy = false; }
      }
      retry.addEventListener('click', load);
      load();
    }
    loadMore.hidden = shown >= group.files.length;
    loadMore.textContent = `Show 2 more copies (${group.files.length - shown} remaining)`;
  }
  loadMore.addEventListener('click', () => addCopies(2)); compare.append(loadMore); addCopies(2); updateSelection();
}

async function reload() {
  const mode = state.mode, loadId = ++state.loadId;
  $('reload').disabled = true; notify('Loading your saved scan…');
  try {
    const data = await (await request('/api/groups?' + new URLSearchParams({ view: mode }))).json();
    if (mode !== state.mode || loadId !== state.loadId) return;
    if (state.selectionRevisions[mode] && state.selectionRevisions[mode] !== data.revision) state.selections[mode].clear();
    state.selectionRevisions[mode] = data.revision;
    pruneSelection(data.groups, state.selections[mode]);
    clearComparison(); state.groups = data.groups; state.revision = data.revision; state.scope = data.scope || null;
    $('scanned-at').textContent = data.scannedAt ? new Date(data.scannedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'No folder scan yet';
    $('folder-results-scope').textContent = data.scope ? `Results for: OneDrive / ${data.scope} · includes subfolders` : 'No completed folder scan yet.';
    applyFilters();
    if (state.filtered.length) selectGroup(state.filtered[0]);
    else if (mode === 'folder') {
      $('comparison').replaceChildren(element('div', 'empty-state', data.scope ? 'No duplicate groups in this folder match the current filters.' : 'Choose a folder above, then click Scan this folder.'));
    }
  } catch (error) { if (mode === state.mode && loadId === state.loadId) notify(error.message, true); }
  finally { if (loadId === state.loadId) $('reload').disabled = isScanning() || (state.mode === 'folder' && !state.scope); }
}

async function switchView(mode) {
  if (mode === state.mode) return;
  state.savedFilters[state.mode] = Object.fromEntries(filterIds.map(id => [id, $(id).value]));
  clearComparison(); state.mode = mode;
  $('filters').reset();
  for (const [id, value] of Object.entries(state.savedFilters[mode] || {})) $(id).value = value;
  state.groups = []; state.filtered = []; state.revision = null; applyFilters(); $('scanned-at').textContent = '—';
  for (const name of ['all', 'folder']) {
    $('tab-' + name).setAttribute('aria-selected', String(name === mode));
    $('tab-' + name).tabIndex = name === mode ? 0 : -1;
  }
  $('results-panel').setAttribute('aria-labelledby', 'tab-' + mode);
  $('folder-controls').hidden = mode !== 'folder';
  $('view-intro').textContent = mode === 'folder' ? 'Run a fresh scan inside one OneDrive folder.' : 'Compare copies side by side before deciding what to keep.';
  if (mode === 'folder') {
    if (!folder.loaded) browseFolders('');
    watchFolderScan();
  } else watchRefresh('all');
  renderRefreshStatus();
  await reload();
}

async function browseFolders(path) {
  const browseId = ++folder.browseId;
  $('folder-list').replaceChildren(element('p', '', 'Loading OneDrive folders…'));
  $('start-folder-scan').disabled = true;
  try {
    const data = await (await request('/api/folders?' + new URLSearchParams({ path }))).json();
    if (browseId !== folder.browseId) return;
    folder.path = data.path; folder.loaded = true;
    $('folder-location').textContent = data.path ? '/ ' + data.path : 'Open a folder to select it';
    $('folder-up').disabled = !data.path;
    $('folder-list').replaceChildren();
    for (const child of data.folders) {
      const button = element('button', 'folder-option');
      button.append(iconNode('folder', 'opt-icon'), document.createTextNode(child.name), element('span', 'opt-arrow', '›'));
      button.setAttribute('aria-label', 'Open folder: ' + child.name); button.title = child.path;
      button.addEventListener('click', () => browseFolders(child.path)); $('folder-list').append(button);
    }
    if (!data.folders.length) $('folder-list').append(element('p', '', 'No subfolders. You can scan the folder currently selected.'));
    $('start-folder-scan').disabled = !folder.path || folder.running;
  } catch (error) {
    if (browseId !== folder.browseId) return;
    const retry = element('button', 'secondary', 'Retry folder list'); retry.addEventListener('click', () => browseFolders(path));
    $('folder-list').replaceChildren(element('p', 'error', error.message), retry);
  }
}

function showFolderStatus(info) {
  folder.running = info.status === 'running';
  $('folder-status').textContent = `${info.path ? info.path + ': ' : ''}${info.message}${folder.running ? ` (${info.elapsed || 0}s)` : ''}`;
  $('folder-status').classList.toggle('error', info.status === 'failed');
  $('cancel-folder-scan').hidden = !folder.running;
  $('start-folder-scan').disabled = folder.running || !folder.path;
}

function renderRefreshStatus() {
  const scan = scans[state.mode], info = scan.info;
  const running = info.status === 'running';
  const failed = ['failed', 'cancelled'].includes(info.status);
  $('refresh-banner').hidden = info.status === 'idle' && !scan.note;
  $('refresh-banner').classList.toggle('error', failed || scan.noteError);
  $('refresh-message').textContent = `${scan.note ? scan.note + ' ' : ''}${info.path ? info.path + ': ' : ''}${info.message || ''}${running ? ` (${info.elapsed || 0}s)` : ''}${failed ? ' Showing saved results; a fresh cloud scan has not completed.' : ''}`;
  $('retry-refresh').hidden = !failed;
  $('stop-refresh').hidden = !running;
  $('reload').disabled = running || (state.mode === 'folder' && !state.scope);
  refreshCurrentSelection(); updateBulkToolbar();
}

function updateScanInfo(mode, info) {
  scans[mode].info = info;
  if (mode === 'folder') showFolderStatus(info);
  if (mode === state.mode) renderRefreshStatus();
}

async function watchRefresh(mode) {
  const scan = scans[mode];
  clearTimeout(scan.timer); scan.timer = null;
  try {
    const wasRunning = isScanning(mode);
    const info = await (await request('/api/rescan?' + new URLSearchParams({ view: mode }))).json();
    updateScanInfo(mode, info);
    if (wasRunning && info.status === 'completed' && state.mode === mode) await reload();
    if (info.status === 'running') scan.timer = setTimeout(() => watchRefresh(mode), 1500);
  } catch (error) { updateScanInfo(mode, { status: 'failed', message: error.message }); }
}

function watchFolderScan() { return watchRefresh('folder'); }

async function startRescan(mode = state.mode, scope = state.scope, note = '', noteError = false) {
  if (mode === 'folder' && !scope) { notify('Choose a folder and run its first scan before refreshing.', true); return; }
  scans[mode].note = note; scans[mode].noteError = noteError;
  updateScanInfo(mode, { status: 'running', path: scope || '', message: 'Starting a fresh cloud scan…' });
  try {
    const info = await (await request('/api/rescan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ view: mode, scope }) })).json();
    updateScanInfo(mode, info);
    if (info.status === 'completed') { if (state.mode === mode) await reload(); }
    else if (info.status === 'running') watchRefresh(mode);
  } catch (error) { updateScanInfo(mode, { status: 'failed', message: 'Could not start the fresh scan. ' + error.message }); }
}

for (const mode of ['all', 'folder']) {
  $('tab-' + mode).addEventListener('click', () => switchView(mode));
  $('tab-' + mode).addEventListener('keydown', event => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault(); const next = event.key === 'Home' ? 'all' : event.key === 'End' ? 'folder' : mode === 'all' ? 'folder' : 'all';
      $('tab-' + next).focus(); switchView(next);
    }
  });
}
$('folder-root').addEventListener('click', () => browseFolders(''));
$('folder-up').addEventListener('click', () => browseFolders(folder.path.split('/').slice(0, -1).join('/')));
$('start-folder-scan').addEventListener('click', async () => {
  $('start-folder-scan').disabled = true;
  try {
    const info = await (await request('/api/folder-scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: folder.path }) })).json();
    scans.folder.note = ''; scans.folder.noteError = false;
    updateScanInfo('folder', info); $('folder-browser').open = false;
    if (info.status === 'completed') await reload(); else watchFolderScan();
  } catch (error) { $('folder-status').textContent = error.message; $('folder-status').classList.add('error'); $('start-folder-scan').disabled = !folder.path || folder.running; }
});
$('cancel-folder-scan').addEventListener('click', async () => {
  try { await request('/api/folder-scan/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); watchFolderScan(); }
  catch (error) { $('folder-status').textContent = error.message; }
});

$('filters').addEventListener('submit', event => event.preventDefault());
$('mark-page').addEventListener('click', () => markGroups(state.filtered.slice(state.page * pageSize, (state.page + 1) * pageSize)));
$('mark-filtered').addEventListener('click', () => markGroups(state.filtered));
$('clear-selection').addEventListener('click', () => { selection().clear(); selectionChanged(); });
$('review-bulk').addEventListener('click', () => reviewSelection());
$('filters').addEventListener('input', applyFilters);
$('reset-filters').addEventListener('click', () => { $('filters').reset(); applyFilters(); });
$('reload').addEventListener('click', () => startRescan());
$('retry-refresh').addEventListener('click', () => startRescan(state.mode, state.scope, scans[state.mode].note, scans[state.mode].noteError));
$('stop-refresh').addEventListener('click', async () => {
  try { await request('/api/rescan/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ view: state.mode }) }); watchRefresh(state.mode); }
  catch (error) { notify(error.message, true); }
});
$('previous').addEventListener('click', () => { state.page--; renderList(); $('group-list').scrollTop = 0; });
$('next').addEventListener('click', () => { state.page++; renderList(); $('group-list').scrollTop = 0; });
reload();
watchRefresh('all');
