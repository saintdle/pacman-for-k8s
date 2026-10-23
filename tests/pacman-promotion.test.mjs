import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { planConsumerPromotion, rollbackRequiresExplicitResume, validateAppReleaseMetadata } from '../tools/pacman-promotion.mjs';

const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/promotion/${name}`, import.meta.url), 'utf8'));
const initialState = JSON.parse(readFileSync(new URL('../pacman-promotion-state.json', import.meta.url), 'utf8'));
const promotionCli = fileURLToPath(new URL('../tools/pacman-promotion.mjs', import.meta.url));
const promotionWorkflow = fileURLToPath(new URL('../.github/workflows/pacman-promotion.yml', import.meta.url));
const current = fixture('current.json');
const newer = fixture('newer.json');

test('tracked initial state matches current immutable public app, chart, and image pins', () => {
  assert.equal(initialState.kind, 'pacman-k8s-promotion-state');
  assert.equal(initialState.app.version, '0.7.16');
  assert.equal(initialState.app.reference, 'https://github.com/saintdle/pacman/releases/download/pacman-v0.7.16/release-metadata.json');
  assert.equal(initialState.app.sha256, 'ef15decce2fc0cb9aa0b14d4a2d059e10eb13638afc57fe99277b8688aea4c4e');
  assert.equal(initialState.app.image_manifest_list_digest, 'sha256:592740129bce7530a347146b86647f8b516df3c19105aa4c2fe40b878840fccf');
  assert.equal(initialState.chart.version, '0.7.17');
  assert.equal(initialState.chart.package_sha256, 'b0722ab17320afe14d5bee5ebf25ed26c9dcfa59f24e238764c4b2d8a76eb971');
  assert.deepEqual(planConsumerPromotion({ current: initialState, candidate: initialState }), { action: 'noop', reason: 'already-promoted' });
  assert.deepEqual(initialState.app.missing_evidence, ['app-release-record', 'platform-descriptors', 'provenance', 'signature']);
});

function filterPendingState(state, pullRequests, branchSha = 'a'.repeat(40), baseSha = 'd'.repeat(40)) {
  const directory = mkdtempSync(join(tmpdir(), 'pacman-pending-test-'));
  try {
    const statePath = join(directory, 'state.json');
    const proposalPath = join(directory, 'proposals.json');
    const outputPath = join(directory, 'selected.json');
    writeFileSync(statePath, JSON.stringify(state));
    writeFileSync(proposalPath, JSON.stringify(pullRequests));
    const result = spawnSync(process.execPath, [promotionCli, 'filter-pending', statePath, proposalPath], {
      encoding: 'utf8', env: { ...process.env, PENDING_BRANCH_SHA: branchSha, PENDING_BASE_SHA: baseSha, PENDING_FILTERED_PATH: outputPath },
    });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(readFileSync(outputPath, 'utf8'));
  } finally { rmSync(directory, { recursive: true, force: true }); }
}

test('verified app metadata accepts property reordering but rejects identity changes', () => {
  const expected = {
    version: '0.7.18', tag: 'pacman-v0.7.18', commit: '1'.repeat(40),
    image_digest: `sha256:${'b'.repeat(64)}`,
    app_record_ref: 'https://github.com/saintdle/pacman/releases/download/pacman-v0.7.18/app-release-record.json',
    app_record_sha256: 'c'.repeat(64),
  };
  const reordered = Object.fromEntries(Object.entries(expected).reverse());
  assert.deepEqual(validateAppReleaseMetadata(Buffer.from(JSON.stringify(reordered)), expected), reordered);
  for (const field of Object.keys(expected)) {
    const changed = { ...expected, [field]: `${expected[field]}-changed` };
    assert.throws(() => validateAppReleaseMetadata(Buffer.from(JSON.stringify(changed)), expected), /does not pin/);
  }
  assert.throws(() => validateAppReleaseMetadata(Buffer.from(JSON.stringify({ ...expected, unexpected: true })), expected), /invalid schema/);
});

test('only an exact open proposal holds pending promotion state', () => {
  const state = { schema_version: 1, kind: 'pacman-k8s-promotion-state', mode: 'automatic',
    app: { version: '0.7.18', image_manifest_list_digest: `sha256:${'b'.repeat(64)}` },
    chart: { version: '0.7.19', package_sha256: 'c'.repeat(64) } };
  const proposal = { state: 'OPEN', isCrossRepository: false, headRefName: 'automation/pacman-promotion', headRefOid: 'a'.repeat(40),
    baseRefName: 'main', baseRefOid: 'd'.repeat(40) };
  const select = (pullRequests) => filterPendingState(state, pullRequests);
  assert.deepEqual(select([proposal]), state);
  for (const stateValue of ['CLOSED', 'MERGED']) assert.equal(select([{ ...proposal, state: stateValue }]), null);
  assert.equal(select([]), null);
  assert.equal(select([{ ...proposal, headRefOid: 'e'.repeat(40) }]), null);
  assert.equal(select([{ ...proposal, baseRefOid: 'f'.repeat(40) }]), null);
  assert.equal(select([{ ...proposal, baseRefName: 'release' }]), null);
  assert.equal(select([{ ...proposal, isCrossRepository: true }]), null);
  const { isCrossRepository, ...missingRepositoryIdentity } = proposal;
  assert.equal(select([missingRepositoryIdentity]), null);
});

test('workflow filter preserves only a newer coherent proposal and advances past stale state', () => {
  const pending = fixture('pending-newer.json');
  const proposal = { state: 'OPEN', isCrossRepository: false, headRefName: 'automation/pacman-promotion', headRefOid: 'a'.repeat(40),
    baseRefName: 'main', baseRefOid: 'd'.repeat(40) };
  const selected = filterPendingState({ schema_version: 1, kind: 'pacman-k8s-promotion-state', mode: 'automatic', ...pending }, [proposal]);
  assert.deepEqual(planConsumerPromotion({ current, candidate: fixture('delayed-intermediate.json'), pending: selected }),
    { action: 'noop', reason: 'newer-coherent-promotion-is-pending' });

  const stale = structuredClone(pending);
  stale.app.version = '0.7.16';
  stale.chart.version = '0.7.17';
  const staleSelected = filterPendingState({ schema_version: 1, kind: 'pacman-k8s-promotion-state', mode: 'automatic', ...stale }, [proposal]);
  assert.equal(planConsumerPromotion({ current, candidate: newer, pending: staleSelected }).action, 'promotion');
  for (const stateValue of ['CLOSED', 'MERGED']) {
    assert.equal(planConsumerPromotion({ current, candidate: newer,
      pending: filterPendingState({ schema_version: 1, kind: 'pacman-k8s-promotion-state', mode: 'automatic', ...pending }, [{ ...proposal, state: stateValue }]) }).action, 'promotion');
  }
  assert.equal(filterPendingState({ schema_version: 1, kind: 'pacman-k8s-promotion-state', mode: 'automatic', ...pending }, []), null);
});

test('applied rollback state requires explicit resume even after its proposal closes', () => {
  const currentState = { ...current, mode: 'rollback', rollback: {
    target_app_reference: current.app.reference, target_app_sha256: current.app.sha256,
    target_chart_reference: current.chart.reference, target_chart_sha256: current.chart.sha256,
    reason: 'Restore the last known-good chart',
  } };
  assert.equal(rollbackRequiresExplicitResume({ currentState, pendingState: null, mode: 'reconcile' }), true);
  assert.equal(rollbackRequiresExplicitResume({ currentState, pendingState: null, mode: 'resume' }), false);
  assert.equal(rollbackRequiresExplicitResume({ currentState: { ...current, mode: 'automatic' }, pendingState: null, mode: 'resume' }), true);
  assert.equal(rollbackRequiresExplicitResume({ currentState: { mode: 'rollback' }, pendingState: null, mode: 'resume' }), true);
  assert.throws(() => planConsumerPromotion({ current: { ...newer, mode: 'automatic' }, candidate: current, mode: 'resume' }), /applied rollback/);
  assert.deepEqual(planConsumerPromotion({ current: currentState, candidate: newer, mode: 'resume' }),
    { action: 'resume', app: newer.app, chart: newer.chart });
  assert.throws(() => planConsumerPromotion({ current: currentState, candidate: fixture('old-event.json'), mode: 'resume' }), /cannot be older/);
});

test('workflow query uses supported branch filtering and requests repository identity', () => {
  const workflow = readFileSync(new URL('../.github/workflows/pacman-promotion.yml', import.meta.url), 'utf8');
  const query = workflow.split('\n').filter((line) => line.includes('gh pr list --repo saintdle/pacman-for-k8s')
    || line.trim().startsWith('--json state,headRefName')).join(' ');
  assert.match(query, /--head automation\/pacman-promotion/);
  assert.doesNotMatch(query, /--head saintdle:/);
  assert.match(query, /isCrossRepository/);
});

test('workflow yq command updates only Pacman immutable image digests', () => {
  const updateCommand = readFileSync(promotionWorkflow, 'utf8').split('\n').find((line) => line.trim().startsWith('yq -i '))?.trim();
  assert.ok(updateCommand, 'workflow image update command is present');
  const directory = mkdtempSync(join(tmpdir(), 'pacman-image-update-test-'));
  try {
    const manifestPath = join(directory, 'manifest.yaml');
    const oldDigest = `sha256:${'a'.repeat(64)}`;
    const candidateDigest = `sha256:${'b'.repeat(64)}`;
    writeFileSync(manifestPath, `apiVersion: v1\nkind: Pod\nspec:\n  containers:\n    - name: pacman\n      image: docker.io/saintdle/pacman@${oldDigest}\n    - name: mongo\n      image: mongo:7\n    - name: teaching-variant\n      image: docker.io/saintdle/pacman:latest\n    - name: optional\n      image: null\n`);
    const yqBinary = process.env.PROMOTION_YQ_BINARY;
    const path = yqBinary ? `${dirname(yqBinary)}:${process.env.PATH}` : process.env.PATH;
    const result = spawnSync('bash', ['-e', '-o', 'pipefail', '-c', `file=manifest.yaml\n${updateCommand}`], {
      cwd: directory, encoding: 'utf8', env: { ...process.env, PATH: path, PACMAN_IMAGE: `docker.io/saintdle/pacman@${candidateDigest}` },
    });
    assert.equal(result.status, 0, result.stderr);
    const updated = readFileSync(manifestPath, 'utf8');
    assert.equal(updated.split(`docker.io/saintdle/pacman@${candidateDigest}`).length - 1, 1);
    assert.ok(!updated.includes(`docker.io/saintdle/pacman@${oldDigest}`));
    assert.match(updated, /image: mongo:7/);
    assert.match(updated, /image: docker\.io\/saintdle\/pacman:latest/);
    assert.match(updated, /image: null/);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('workflow reference gate passes the candidate and rejects stale or mixed Pac-Man pins', () => {
  const gateCommand = readFileSync(promotionWorkflow, 'utf8').split('\n').find((line) => line.trim().startsWith('test -z "$(rg -l '))?.trim();
  assert.ok(gateCommand, 'workflow final image reference gate is present');
  const candidateDigest = `sha256:${'a'.repeat(64)}`;
  const staleDigest = `sha256:${'b'.repeat(64)}`;
  const candidateImage = `docker.io/saintdle/pacman@${candidateDigest}`;
  const staleImage = `docker.io/saintdle/pacman@${staleDigest}`;
  const cases = [
    { name: 'candidate-only', images: [candidateImage, 'mongo:7'], expected: 0 },
    { name: 'stale-only', images: [staleImage, 'mongo:7'], expected: 1 },
    { name: 'mixed', images: [candidateImage, staleImage, 'mongo:7'], expected: 1 },
  ];
  for (const fixtureCase of cases) {
    const directory = mkdtempSync(join(tmpdir(), 'pacman-reference-gate-test-'));
    try {
      writeFileSync(join(directory, 'manifest.yaml'), `containers:\n${fixtureCase.images.map((image) => `  - image: ${image}`).join('\n')}\n`);
      const command = gateCommand.replace('${{ steps.plan.outputs.digest }}', candidateDigest);
      const result = spawnSync('bash', ['-e', '-o', 'pipefail', '-c', command], { cwd: directory, encoding: 'utf8' });
      assert.equal(result.status, fixtureCase.expected, `${fixtureCase.name}: ${result.stderr}`);
    } finally { rmSync(directory, { recursive: true, force: true }); }
  }
});

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
