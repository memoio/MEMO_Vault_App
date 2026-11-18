import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ReviewPending } from '../src/reviewPending.ts';

test('different candidates can be processed independently without duplicate submissions', () => {
  const pending = new ReviewPending();
  assert.equal(pending.start('a'), true);
  assert.equal(pending.start('a'), false);
  assert.equal(pending.start('b'), true);
  pending.finish('a');
  assert.equal(pending.has('a'), false);
  assert.equal(pending.has('b'), true);
  assert.equal(pending.start('a'), true);
});
