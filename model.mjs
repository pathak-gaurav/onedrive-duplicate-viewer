export function bytes(value) {
  if (!Number.isFinite(value)) return 'Unknown';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit++; }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: unit ? 2 : 0 })} ${units[unit]}`;
}

export function filterGroups(groups, filters) {
  const search = filters.search.trim().toLowerCase();
  const min = filters.min === '' ? 0 : Number(filters.min) * 1e6;
  const max = filters.max === '' ? Infinity : Number(filters.max) * 1e6;
  const copies = Number(filters.copies) || 2;
  return groups.filter(group => group.size >= min && group.size <= max && group.files.length >= copies &&
    (filters.type === 'all' || group.files.some(file => file.type === filters.type)) &&
    (!search || group.files.some(file => file.path.toLowerCase().includes(search))))
    .sort((a, b) => filters.sort === 'size' ? b.size - a.size : filters.sort === 'copies' ? b.files.length - a.files.length : filters.sort === 'name' ? a.files[0].name.localeCompare(b.files[0].name) : b.savings - a.savings);
}

export function totals(groups) {
  return groups.reduce((sum, group) => ({ groups: sum.groups + 1, extra: sum.extra + group.files.length - 1, savings: sum.savings + group.savings }), { groups: 0, extra: 0, savings: 0 });
}

export function keeperFile(group, rule = 'oldest') {
  if (!group.sameSize || group.files.length < 2) return null;
  const files = [...group.files];
  if (rule === 'shortest') return files.sort((a, b) => a.path.length - b.path.length || a.path.localeCompare(b.path))[0];
  if (!['oldest', 'newest'].includes(rule)) return null;
  const dated = files.map(file => ({ file, time: Date.parse(file.modified.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1')) }));
  if (dated.some(item => !Number.isFinite(item.time))) return null;
  dated.sort((a, b) => (rule === 'newest' ? b.time - a.time : a.time - b.time) || a.file.path.localeCompare(b.file.path));
  return dated[0].file;
}

export function markExtras(groups, selection, rule) {
  let marked = 0, skipped = 0;
  for (const group of groups) {
    const keeper = keeperFile(group, rule);
    if (!keeper) { skipped++; continue; }
    selection.set(group.id, new Set(group.files.filter(file => file.id !== keeper.id).map(file => file.id)));
    marked++;
  }
  return { marked, skipped };
}

export function selectFile(group, selection, id, checked) {
  if (!group.files.some(file => file.id === id)) return false;
  const files = new Set(selection.get(group.id) || []);
  if (checked) {
    if (!group.sameSize || (!files.has(id) && files.size >= group.files.length - 1)) return false;
    files.add(id);
  } else files.delete(id);
  if (files.size) selection.set(group.id, files); else selection.delete(group.id);
  return true;
}

export function pruneSelection(groups, selection) {
  const known = new Map(groups.map(group => [group.id, group]));
  for (const [id, selected] of selection) {
    const group = known.get(id);
    const surviving = group?.files.filter(file => selected.has(file.id)) || [];
    if (!group?.sameSize || !surviving.length || surviving.length >= group.files.length) selection.delete(id);
    else selection.set(id, new Set(surviving.map(file => file.id)));
  }
}

export function selectionPlans(groups, selection) {
  const plans = [];
  for (const group of groups) {
    const ids = selection.get(group.id);
    if (!ids?.size) continue;
    const remove = group.files.filter(file => ids.has(file.id));
    const keep = group.files.filter(file => !ids.has(file.id));
    if (!group.sameSize || !remove.length || !keep.length || remove.length !== ids.size) throw new Error('Selections changed. Reload the report and keep at least one copy per group.');
    plans.push({ group: group.id, remove, keep });
  }
  return plans;
}

export async function runDeletionPlans(plans, { send, onResult, shouldStop }) {
  const deleted = [];
  let error = null, completed = 0;
  for (const plan of plans) {
    if (shouldStop()) break;
    try {
      const result = await send(plan, completed);
      const ids = new Set(result.deleted?.map(file => file.id));
      if (!Array.isArray(result.deleted) || ids.size !== result.deleted.length || [...ids].some(id => !plan.remove.some(file => file.id === id))) throw new Error('Unexpected deletion result. Check OneDrive before continuing.');
      deleted.push(...result.deleted); completed++;
      onResult(plan, result, deleted.length, completed);
      if (result.error) { error = result.error; break; }
      if (ids.size !== plan.remove.length) { error = 'Not every selected copy was confirmed removed. Check OneDrive before continuing.'; break; }
    } catch (failure) {
      error = failure.message + ' No request will be retried automatically. Check OneDrive before trying again.';
      break;
    }
  }
  return { deleted, error, completed, stopped: completed < plans.length && !error };
}
