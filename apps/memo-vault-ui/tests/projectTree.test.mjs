import { test } from 'node:test';
import assert from 'node:assert/strict';
import { projectSubtree, subtreeCount } from '../src/projectTree.ts';

const projects = [
  { project_id: 'other', parent_project_id: null },
  { project_id: 'personal', parent_project_id: null },
  { project_id: 'child', parent_project_id: 'personal' },
  { project_id: 'grandchild', parent_project_id: 'child' },
];
const counts = [
  { project_id: 'other', visible_valid_count: 4, syncing_count: 0 },
  { project_id: 'personal', visible_valid_count: 3, syncing_count: 1 },
  { project_id: 'child', visible_valid_count: 1, syncing_count: 2 },
  { project_id: 'grandchild', visible_valid_count: 0, syncing_count: 0 },
];
test('parent selection and counts include descendants but exclude other roots', () => {
  assert.deepEqual(projectSubtree(projects, 'personal').map(p => p.project_id), ['personal', 'child', 'grandchild']);
  assert.deepEqual(subtreeCount(projects, counts, 'personal'), { visible_valid_count: 4, syncing_count: 3 });
  assert.deepEqual(subtreeCount(projects, counts, 'other'), { visible_valid_count: 4, syncing_count: 0 });
});
