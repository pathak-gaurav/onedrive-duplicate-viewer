import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bytes, filterGroups, totals, keeperFile, markExtras, selectFile, pruneSelection, selectionPlans, runDeletionPlans } from './model.mjs';

const groups = [
  { id: 'a', size: 5e6, savings: 5e6, files: [{ name: '旅行.jpg', path: 'Photos/旅行.jpg', type: 'image' }, { name: 'copy.jpg', path: 'Copy/copy.jpg', type: 'image' }] },
  { id: 'b', size: 100e6, savings: 200e6, files: Array.from({ length: 3 }, () => ({ name: 'clip.mov', path: 'Movies/clip.mov', type: 'video' })) },
  { id: 'c', size: 0, savings: 0, files: [{ name: 'empty.txt', path: 'Notes/empty.txt', type: 'document' }, { name: 'empty.txt', path: 'Copy/empty.txt', type: 'document' }] },
];
const defaults = { search: '', type: 'all', min: '', max: '', copies: '2', sort: 'savings' };

test('size range uses MB, includes boundaries, and accepts an explicit zero maximum', () => {
  assert.deepEqual(filterGroups(groups, { ...defaults, min: '5', max: '5' }).map(g => g.id), ['a']);
  assert.deepEqual(filterGroups(groups, { ...defaults, max: '0' }).map(g => g.id), ['c']);
});
test('search, type and copy filters combine without mutating the input', () => {
  assert.deepEqual(filterGroups(groups, { ...defaults, search: '旅行', type: 'image' }).map(g => g.id), ['a']);
  assert.deepEqual(filterGroups(groups, { ...defaults, copies: '3', type: 'video' }).map(g => g.id), ['b']);
  assert.deepEqual(filterGroups(groups, { ...defaults, search: 'not found' }), []);
  assert.deepEqual(groups.map(g => g.id), ['a', 'b', 'c']);
});
test('space and extra-copy totals retain one copy in every group', () => {
  assert.deepEqual(totals(groups), { groups: 3, extra: 4, savings: 205e6 });
  assert.equal(bytes(1e9), '1 GB');
  assert.equal(bytes(0), '0 B');
});

const selectionGroups = [
  { id: 'photos', sameSize: true, files: [
    { id: 'old', path: 'Originals/old.jpg', name: 'old.jpg', size: 50, modified: '2023-01-02 12:00:00.000000000' },
    { id: 'new', path: 'New/new.jpg', name: 'new.jpg', size: 50, modified: '2025-03-04 12:00:00.000000000' },
    { id: 'short', path: 'a.jpg', name: 'a.jpg', size: 50, modified: '2024-02-03 12:00:00.000000000' },
  ] },
  { id: 'video', sameSize: true, files: [
    { id: 'v1', path: 'Movie/a.mp4', name: 'a.mp4', size: 100, modified: '2021-01-01 00:00:00.000000000' },
    { id: 'v2', path: 'Movie/copy.mp4', name: 'copy.mp4', size: 100, modified: '2022-01-01 00:00:00.000000000' },
  ] },
];

test('keep rules retain the requested copy and skip uncertain groups', () => {
  assert.equal(keeperFile(selectionGroups[0], 'oldest').id, 'old');
  assert.equal(keeperFile(selectionGroups[0], 'newest').id, 'new');
  assert.equal(keeperFile(selectionGroups[0], 'shortest').id, 'short');
  assert.equal(keeperFile({ ...selectionGroups[0], sameSize: false }, 'oldest'), null);
  assert.equal(keeperFile({ ...selectionGroups[0], files: selectionGroups[0].files.map(file => ({ ...file, modified: 'unknown' })) }, 'oldest'), null);
});

test('bulk marking operates only on the supplied page or filtered groups', () => {
  const queue = new Map();
  assert.deepEqual(markExtras([selectionGroups[0]], queue, 'oldest'), { marked: 1, skipped: 0 });
  assert.deepEqual([...queue.get('photos')], ['new', 'short']);
  assert.equal(queue.has('video'), false);
  markExtras([selectionGroups[1]], queue, 'newest');
  assert.deepEqual([...queue.get('video')], ['v1']);
  assert.equal(queue.get('photos').size, 2);
});

test('manual choices persist across groups and cannot mark the last copy', () => {
  const queue = new Map();
  assert.equal(selectFile(selectionGroups[0], queue, 'new', true), true);
  assert.equal(selectFile(selectionGroups[1], queue, 'v2', true), true);
  assert.equal(selectFile(selectionGroups[0], queue, 'short', true), true);
  assert.equal(selectFile(selectionGroups[0], queue, 'old', true), false);
  assert.equal(selectFile(selectionGroups[0], queue, 'unknown', true), false);
  assert.equal(selectFile(selectionGroups[0], queue, 'short', false), true);
  const plans = selectionPlans(selectionGroups, queue);
  assert.equal(plans.length, 2);
  assert.deepEqual(plans[0].remove.map(file => file.id), ['new']);
  assert.deepEqual(plans[0].keep.map(file => file.id), ['old', 'short']);
});

test('reloading drops stale choices and clears groups whose keeper disappeared', () => {
  const queue = new Map([['photos', new Set(['new', 'short'])], ['video', new Set(['v2'])], ['unknown', new Set(['gone'])]]);
  const changed = [{ ...selectionGroups[0], files: selectionGroups[0].files.filter(file => file.id !== 'old') }, selectionGroups[1]];
  pruneSelection(changed, queue);
  assert.equal(queue.has('photos'), false);
  assert.equal(queue.has('unknown'), false);
  assert.deepEqual([...queue.get('video')], ['v2']);
});

test('review rejects selections that would remove every copy', () => {
  const queue = new Map([['video', new Set(['v1', 'v2'])]]);
  assert.throws(() => selectionPlans(selectionGroups, queue), /keep at least one copy/);
});

test('bulk deletion acknowledges each group, then stops at a partial failure', async () => {
  const queue = new Map(); markExtras(selectionGroups, queue, 'oldest');
  const plans = selectionPlans(selectionGroups, queue);
  const sent = [], acknowledged = [];
  const result = await runDeletionPlans(plans, {
    shouldStop: () => false,
    send: async plan => { sent.push(plan.group); return plan.group === 'photos' ? { deleted: [plan.remove[0]], error: 'File changed' } : { deleted: plan.remove, error: null }; },
    onResult: (plan, result, removed) => acknowledged.push({ group: plan.group, removed }),
  });
  assert.deepEqual(sent, ['photos']);
  assert.equal(result.deleted.length, 1);
  assert.equal(result.error, 'File changed');
  assert.deepEqual(acknowledged, [{ group: 'photos', removed: 1 }]);
});

test('stop request waits for the current group and leaves the next group untouched', async () => {
  const queue = new Map(); markExtras(selectionGroups, queue, 'oldest');
  const plans = selectionPlans(selectionGroups, queue);
  let stop = false;
  const sent = [];
  const result = await runDeletionPlans(plans, {
    shouldStop: () => stop,
    send: async plan => { sent.push(plan.group); return { deleted: plan.remove, error: null }; },
    onResult: () => { stop = true; },
  });
  assert.deepEqual(sent, ['photos']);
  assert.equal(result.deleted.length, 2);
  assert.equal(result.stopped, true);
});

test('network failures are never automatically retried', async () => {
  const queue = new Map(); markExtras(selectionGroups, queue, 'oldest');
  let attempts = 0;
  const result = await runDeletionPlans(selectionPlans(selectionGroups, queue), {
    shouldStop: () => false,
    send: async () => { attempts++; throw new Error('Connection interrupted'); },
    onResult: () => assert.fail('Unknown outcomes cannot be acknowledged'),
  });
  assert.equal(attempts, 1);
  assert.equal(result.deleted.length, 0);
  assert.match(result.error, /No request will be retried automatically/);
});

test('successful bulk run covers every reviewed group and checks acknowledgments', async () => {
  const queue = new Map(); markExtras(selectionGroups, queue, 'oldest');
  const plans = selectionPlans(selectionGroups, queue);
  const progress = [];
  const result = await runDeletionPlans(plans, {
    shouldStop: () => false,
    send: async plan => ({ deleted: plan.remove, error: null }),
    onResult: (_plan, _result, removed) => progress.push(removed),
  });
  assert.equal(result.deleted.length, 3);
  assert.deepEqual(progress, [2, 3]);
  assert.equal(result.stopped, false);
  const bad = await runDeletionPlans(plans, { shouldStop: () => false, send: async () => ({ deleted: [{ id: 'not-selected' }], error: null }), onResult: () => assert.fail('Invalid acknowledgments cannot update the tray') });
  assert.equal(bad.deleted.length, 0);
  assert.match(bad.error, /Unexpected deletion result/);
});
