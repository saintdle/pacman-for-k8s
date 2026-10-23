import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { planConsumerPromotion } from '../tools/pacman-promotion.mjs';

const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/promotion/${name}`, import.meta.url), 'utf8'));
const current = fixture('current.json');
const newer = fixture('newer.json');

test('duplicate verified identity is an explicit no-op', () => {
  assert.deepEqual(planConsumerPromotion({ current, candidate: current }), { action: 'noop', reason: 'already-promoted' });
});

test('older release notification cannot downgrade either axis', () => {
  assert.deepEqual(planConsumerPromotion({ current, candidate: fixture('old-event.json') }), { action: 'noop', reason: 'older-release-event' });
});

test('same app version with another digest is rejected', () => {
  const candidate = structuredClone(newer);
  candidate.app.version = current.app.version;
  candidate.app.image_manifest_list_digest = `sha256:${'c'.repeat(64)}`;
  assert.throws(() => planConsumerPromotion({ current, candidate }), /conflicting image digest/);
});

test('same image digest with a changed immutable app record pin is rejected', () => {
  const candidate = structuredClone(current);
  candidate.app.sha256 = 'a'.repeat(64);
  assert.throws(() => planConsumerPromotion({ current, candidate }), /conflicting record checksum/);
});

test('new chart can keep the same verified app identity', () => {
  const candidate = structuredClone(current);
  candidate.chart.version = '0.7.18';
  candidate.chart.package_sha256 = 'a'.repeat(64);
  candidate.chart.package_reference = 'https://github.com/saintdle/helm-charts/releases/download/pacman-0.7.18/pacman-0.7.18.tgz';
  assert.equal(planConsumerPromotion({ current, candidate }).action, 'promotion');
});

test('a verified newer release creates a promotion regardless of notification order', () => {
  assert.equal(planConsumerPromotion({ current, candidate: newer }).action, 'promotion');
});

test('a delayed event cannot replace the coherent newer pending PR', () => {
  const pending = fixture('pending-newer.json');
  const candidate = fixture('delayed-intermediate.json');
  candidate.app.version = '0.7.17';
  candidate.chart.version = '0.7.19';
  assert.deepEqual(planConsumerPromotion({ current, candidate, pending }), { action: 'noop', reason: 'newer-coherent-promotion-is-pending' });
});

test('a same-version chart with a different package checksum is rejected', () => {
  const candidate = structuredClone(current);
  candidate.chart.package_sha256 = 'd'.repeat(64);
  assert.throws(() => planConsumerPromotion({ current, candidate }), /conflicting package checksum/);
});

test('explicit rollback is a proposal requiring older target and reason', () => {
  const target = fixture('old-event.json');
  const plan = planConsumerPromotion({ current, candidate: target, mode: 'rollback', reason: 'Restore known chart package' });
  assert.equal(plan.action, 'rollback-proposal');
  assert.equal(plan.rollback_reason, 'Restore known chart package');
  assert.throws(() => planConsumerPromotion({ current, candidate: target, mode: 'rollback', reason: '' }), /requires a reason/);
});
