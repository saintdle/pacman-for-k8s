import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { assertManagedConsumerReferences, MANAGED_IMAGE_CONTAINER_COUNTS, MANAGED_IMAGE_REFERENCE_PATHS, MANAGED_VERSION_REFERENCE_PATHS, parseRollbackChartReference, planConsumerPromotion, planPromotionCandidate, rollbackCandidate, rollbackRequiresExplicitResume, validateAppReleaseMetadata } from '../tools/pacman-promotion.mjs';

const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/promotion/${name}`, import.meta.url), 'utf8'));
const trackedState = JSON.parse(readFileSync(new URL('../pacman-promotion-state.json', import.meta.url), 'utf8'));
const promotionCli = fileURLToPath(new URL('../tools/pacman-promotion.mjs', import.meta.url));
const promotionWorkflow = fileURLToPath(new URL('../.github/workflows/pacman-promotion.yml', import.meta.url));
const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const current = fixture('current.json');
const newer = fixture('newer.json');
const LEGACY_PROMOTION_STATE = {
  schema_version: 1,
  kind: 'pacman-k8s-promotion-state',
  mode: 'automatic',
  app: {
    version: '0.7.16',
    reference: 'https://github.com/saintdle/pacman/releases/download/pacman-v0.7.16/release-metadata.json',
    sha256: 'ef15decce2fc0cb9aa0b14d4a2d059e10eb13638afc57fe99277b8688aea4c4e',
    source_commit: '13f04d6e6aea62f1d32d57d90e4eff786083c02e',
    source_repository: 'https://github.com/saintdle/pacman',
    image_repository: 'docker.io/saintdle/pacman',
    image_manifest_list_digest: 'sha256:592740129bce7530a347146b86647f8b516df3c19105aa4c2fe40b878840fccf',
    format: 'legacy-release-metadata-v0',
    missing_evidence: ['app-release-record', 'platform-descriptors', 'provenance', 'signature'],
  },
  chart: {
    version: '0.7.17',
    reference: 'https://github.com/saintdle/helm-charts/releases/download/pacman-0.7.17/pacman-0.7.17.tgz',
    sha256: 'b0722ab17320afe14d5bee5ebf25ed26c9dcfa59f24e238764c4b2d8a76eb971',
    package_reference: 'https://github.com/saintdle/helm-charts/releases/download/pacman-0.7.17/pacman-0.7.17.tgz',
    package_sha256: 'b0722ab17320afe14d5bee5ebf25ed26c9dcfa59f24e238764c4b2d8a76eb971',
    source_repository: 'https://github.com/saintdle/helm-charts',
    source_commit: '1d11684428b2069aef8d4e69c37237b5b3c910a5',
    format: 'legacy-chart-package-v0',
    missing_evidence: ['chart-release-record', 'chart-source-identity'],
  },
};

function rollbackFixture({ generated = true, alterRecord = (record) => record } = {}) {
  const tag = 'pacman-0.7.16';
  const chartVersion = '0.7.16';
  const appVersion = '0.7.15';
  const packageReference = `https://github.com/saintdle/helm-charts/releases/download/${tag}/pacman-${chartVersion}.tgz`;
  const recordReference = `https://github.com/saintdle/helm-charts/releases/download/${tag}/chart-release-record.json`;
  const appReference = `https://github.com/saintdle/pacman/releases/download/pacman-v${appVersion}/app-release-record.json`;
  const packageBytes = Buffer.from('deterministic chart package bytes');
  const packageSha = createHash('sha256').update(packageBytes).digest('hex');
  const record = generated ? alterRecord({
    schema_version: 1,
    kind: 'chart-release',
    chart_version: chartVersion,
    source: { repository: 'https://github.com/saintdle/pacman', commit: 'b'.repeat(40) },
    package: { filename: `pacman-${chartVersion}.tgz`, sha256: packageSha, immutable_url: packageReference },
    app_release: { reference: appReference, sha256: 'd'.repeat(64) },
  }) : null;
  const recordBytes = record && Buffer.from(JSON.stringify(record));
  const recordSha = recordBytes && createHash('sha256').update(recordBytes).digest('hex');
  const releaseData = { draft: false, prerelease: false, tag_name: tag, assets: [
    { name: `pacman-${chartVersion}.tgz`, browser_download_url: packageReference },
    ...(generated ? [{ name: 'chart-release-record.json', browser_download_url: recordReference }] : []),
  ] };
  const requestedAssets = [];
  const verifiedAssets = new Map([
    [`pacman-${chartVersion}.tgz`, { data: packageBytes, checksum: packageSha, url: packageReference }],
    ...(generated ? [['chart-release-record.json', { data: recordBytes, checksum: recordSha, url: recordReference }]] : []),
  ]);
  const app = { version: appVersion, reference: appReference, sha256: 'd'.repeat(64), source_commit: 'e'.repeat(40),
    source_repository: 'https://github.com/saintdle/pacman', image_repository: 'docker.io/saintdle/pacman',
    image_manifest_list_digest: `sha256:${'f'.repeat(64)}`, format: generated ? 'app-release-v1' : 'legacy-release-metadata-v0',
    ...(!generated ? { missing_evidence: ['app-release-record', 'platform-descriptors', 'provenance', 'signature'] } : {}) };
  return {
    packageReference, packageSha, recordReference, recordSha, appReference, requestedAssets, record, releaseData, app,
    options: {
      packageRef: packageReference, packageSha, appRef: appReference, appSha: 'd'.repeat(64), reason: 'Restore the last known-good release',
      getRelease: async () => releaseData,
      getVerifiedAsset: async (_release, name) => { requestedAssets.push(name); return verifiedAssets.get(name); },
      readPackageMetadata: () => ({ chartVersion, appVersion, imageDigest: `sha256:${'f'.repeat(64)}` }),
      verifyAppRelease: async (version, reference, sha, imageDigest) => {
        assert.equal(version, appVersion);
        assert.equal(reference, appReference);
        assert.equal(sha, 'd'.repeat(64));
        assert.equal(imageDigest, `sha256:${'f'.repeat(64)}`);
        return app;
      },
      resolveTagCommit: async () => 'b'.repeat(40),
    },
  };
}

function syntheticTypedState() {
  const state = structuredClone(LEGACY_PROMOTION_STATE);
  state.app.format = 'app-release-v1';
  state.app.reference = 'https://github.com/saintdle/pacman/releases/download/pacman-v0.7.16/app-release-record.json';
  state.app.sha256 = '1'.repeat(64);
  delete state.app.missing_evidence;
  state.chart.format = 'chart-release-v1';
  state.chart.reference = 'https://github.com/saintdle/helm-charts/releases/download/helm-chart-v0.7.17/chart-release-record.json';
  state.chart.sha256 = '2'.repeat(64);
  delete state.chart.missing_evidence;
  return state;
}

function syntheticCompatibilityLock(state) {
  return {
    schema_version: 1,
    kind: 'consumer-compatibility-lock',
    app: {
      version: state.app.version, reference: state.app.reference, sha256: state.app.sha256,
      source_repository: state.app.source_repository, source_commit: state.app.source_commit,
      image_repository: state.app.image_repository, image_manifest_list_digest: state.app.image_manifest_list_digest,
    },
    chart: {
      version: state.chart.version, reference: state.chart.reference, sha256: state.chart.sha256,
      package_reference: state.chart.package_reference, package_sha256: state.chart.package_sha256,
      source_repository: state.chart.source_repository, source_commit: state.chart.source_commit,
    },
    supported_evidence_lanes: ['app-manifest-list', 'app-platform-descriptors', 'app-provenance', 'app-signature', 'chart-package-sha256'],
  };
}

function managedReferences(state) {
  const image = `${state.app.image_repository}@${state.app.image_manifest_list_digest}`;
  return [...MANAGED_IMAGE_REFERENCE_PATHS, ...MANAGED_VERSION_REFERENCE_PATHS].map((path) => {
    if (path === 'README.md') return { path, content: `The raw manifests use app \`${state.app.version}\`, matching chart \`${state.chart.version}\`, with roles pinned to \`${image}\`.\nChart \`${state.chart.version}\` defaults to an all-in-one app and Service port \`80\`.\nUse \`--version ${state.chart.version}\` to reproduce the app/chart pair above.` };
    if (path === 'cilium-demos/README.md') return { path, content: `Default image: \`${image}\`.\n` };
    if (path === 'cilium-demos/scripts/deploy-demo.sh') return { path, content: `PACMAN_IMAGE="\${PACMAN_IMAGE:-${image}}"\n` };
    if (path === 'cilium-demos/fault-demo/README.md') return { path, content: `published Pac-Man chart \`${state.chart.version}\` from https://github.com/saintdle/helm-charts/releases/tag/pacman-${state.chart.version})\nexport CHART_VERSION=${state.chart.version}\n` };
    const containers = Array.from({ length: MANAGED_IMAGE_CONTAINER_COUNTS[path] }, () => `    - name: pacman\n      image: ${image}`).join('\n');
    return { path, content: `apiVersion: v1\nspec:\n  containers:\n${containers}\n` };
  });
}

function trackedManagedReferences() {
  const paths = [...new Set([...MANAGED_IMAGE_REFERENCE_PATHS, ...MANAGED_VERSION_REFERENCE_PATHS])];
  return paths.map((path) => ({ path, content: readFileSync(join(repositoryRoot, path), 'utf8') }));
}

function updateDocsFixture({ oldApp, newApp, oldChart, newChart, missingChartEnvPin = false }) {
  const directory = mkdtempSync(join(tmpdir(), 'pacman-update-docs-'));
  const oldDigest = `sha256:${'a'.repeat(64)}`;
  const newDigest = `sha256:${'b'.repeat(64)}`;
  const docs = {
    'README.md': `The raw manifests use app \`${oldApp}\`, matching chart \`${oldChart}\`, with all three\nroles pinned to \`docker.io/saintdle/pacman@${oldDigest}\`.\n\nChart \`${oldChart}\` defaults to an all-in-one app and Service port \`80\`.\nnamespace. Use \`--version ${oldChart}\` to reproduce the app/chart pair above.\n\nExample release: app \`${oldApp}\`, chart \`${oldChart}\`.\n`,
    'cilium-demos/fault-demo/README.md': `The published Pac-Man chart \`${oldChart}\` from the\n[versioned chart release](https://github.com/saintdle/helm-charts/releases/tag/pacman-${oldChart})\n${missingChartEnvPin ? '' : `export CHART_VERSION=${oldChart}\n`}\nExample chart release: \`${oldChart}\`.\n`,
    'cilium-demos/README.md': `Default image: \`docker.io/saintdle/pacman@${oldDigest}\`.\n`,
    'cilium-demos/scripts/deploy-demo.sh': `PACMAN_IMAGE="\${PACMAN_IMAGE:-docker.io/saintdle/pacman@${oldDigest}}"\n`,
  };
  for (const [path, content] of Object.entries(docs)) {
    const fullPath = join(directory, path);
    mkdirSync(dirname(fullPath), { recursive: true });
    writeFileSync(fullPath, content);
  }
  const result = spawnSync(process.execPath, [promotionCli, 'update-docs'], {
    cwd: directory,
    encoding: 'utf8',
    env: { ...process.env,
      DOC_OLD_APP_VERSION: oldApp, DOC_NEW_APP_VERSION: newApp,
      DOC_OLD_CHART_VERSION: oldChart, DOC_NEW_CHART_VERSION: newChart,
      DOC_OLD_IMAGE_DIGEST: oldDigest, DOC_NEW_IMAGE_DIGEST: newDigest },
  });
  const updatedDocs = Object.fromEntries(Object.keys(docs).map((path) => [path, readFileSync(join(directory, path), 'utf8')]));
  rmSync(directory, { recursive: true, force: true });
  return { ...result, docs: updatedDocs, initialDocs: docs, oldDigest, newDigest };
}

test('update-docs keeps app and chart pins independent when the new app version equals the old chart version', () => {
  const result = updateDocsFixture({ oldApp: '0.7.16', newApp: '0.7.17', oldChart: '0.7.17', newChart: '0.7.20' });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.docs['README.md'], /raw manifests use app `0\.7\.17`, matching chart `0\.7\.20`/);
  assert.match(result.docs['README.md'], /--version 0\.7\.20` to reproduce/);
  assert.match(result.docs['README.md'], /Chart `0\.7\.20` defaults to an all-in-one app and Service port `80`\./);
  assert.match(result.docs['cilium-demos/fault-demo/README.md'], /published Pac-Man chart `0\.7\.20`/);
  assert.match(result.docs['cilium-demos/fault-demo/README.md'], /releases\/tag\/pacman-0\.7\.20/);
  assert.match(result.docs['cilium-demos/fault-demo/README.md'], /export CHART_VERSION=0\.7\.20/);
  assert.match(result.docs['README.md'], /Example release: app `0\.7\.16`, chart `0\.7\.17`/);
  assert.match(result.docs['cilium-demos/fault-demo/README.md'], /Example chart release: `0\.7\.17`/);
  for (const path of ['README.md', 'cilium-demos/README.md', 'cilium-demos/scripts/deploy-demo.sh']) {
    assert.ok(result.docs[path].includes(result.newDigest), `${path} should contain the new digest`);
    assert.ok(!result.docs[path].includes(result.oldDigest), `${path} should not retain the old digest`);
  }
});

test('update-docs updates frozen legacy prose and passes the full managed-reference gate', () => {
  const directory = mkdtempSync(join(tmpdir(), 'pacman-real-doc-update-'));
  const paths = [...new Set([...MANAGED_IMAGE_REFERENCE_PATHS, ...MANAGED_VERSION_REFERENCE_PATHS])];
  const originalDocs = new Map();
  const state = structuredClone(LEGACY_PROMOTION_STATE);
  state.app.version = '0.7.17';
  state.chart.version = '0.7.20';
  const statePath = join(directory, 'candidate-state.json');
  try {
    for (const { path, content } of managedReferences(LEGACY_PROMOTION_STATE)) {
      originalDocs.set(path, content);
      const fullPath = join(directory, path);
      mkdirSync(dirname(fullPath), { recursive: true });
      writeFileSync(fullPath, content);
    }
    writeFileSync(statePath, JSON.stringify(state));

    const update = spawnSync(process.execPath, [promotionCli, 'update-docs'], {
      cwd: directory,
      encoding: 'utf8',
      env: { ...process.env,
        DOC_OLD_APP_VERSION: '0.7.16', DOC_NEW_APP_VERSION: '0.7.17',
        DOC_OLD_CHART_VERSION: '0.7.17', DOC_NEW_CHART_VERSION: '0.7.20' },
    });
    const readmePath = join(directory, 'README.md');
    const readme = readFileSync(readmePath, 'utf8');
    const faultDemo = readFileSync(join(directory, 'cilium-demos/fault-demo/README.md'), 'utf8');
    const chartVersionLines = faultDemo.match(/^export CHART_VERSION=.*$/gm) ?? [];
    const chartDefaultsVersion = /^Chart `([^`]+)` defaults to an all-in-one app and Service port `80`\./m.exec(readme)?.[1] ?? null;
    const referenceCheck = spawnSync(process.execPath, [promotionCli, 'check-references', statePath], {
      cwd: directory, encoding: 'utf8',
    });
    const staleReadme = readme.replace('Chart `0.7.20` defaults', 'Chart `0.7.17` defaults');
    writeFileSync(readmePath, staleReadme);
    const staleDefaultsCheck = spawnSync(process.execPath, [promotionCli, 'check-references', statePath], {
      cwd: directory, encoding: 'utf8',
    });

    assert.equal(update.status, 0, update.stderr);
    assert.deepEqual({
      chartVersionLines,
      chartDefaultsVersion,
      referenceCheckStatus: referenceCheck.status,
      staleDefaultsMutationApplied: staleReadme !== readme,
      staleDefaultsRejected: staleDefaultsCheck.status !== 0
        && /README\.md current chart defaults version 0\.7\.17 does not match promotion state 0\.7\.20/.test(staleDefaultsCheck.stderr),
    }, {
      chartVersionLines: ['export CHART_VERSION=0.7.20'],
      chartDefaultsVersion: '0.7.20',
      referenceCheckStatus: 0,
      staleDefaultsMutationApplied: true,
      staleDefaultsRejected: true,
    });

    const expectedReadme = originalDocs.get('README.md')
      .replace('raw manifests use app `0.7.16`, matching chart `0.7.17`', 'raw manifests use app `0.7.17`, matching chart `0.7.20`')
      .replace('Chart `0.7.17` defaults', 'Chart `0.7.20` defaults')
      .replace('Use `--version 0.7.17`', 'Use `--version 0.7.20`');
    const expectedFaultDemo = originalDocs.get('cilium-demos/fault-demo/README.md')
      .replace('published Pac-Man chart `0.7.17`', 'published Pac-Man chart `0.7.20`')
      .replace('releases/tag/pacman-0.7.17', 'releases/tag/pacman-0.7.20')
      .replace('export CHART_VERSION=0.7.17', 'export CHART_VERSION=0.7.20');
    assert.equal(readme, expectedReadme, 'only the managed README version pins should change');
    assert.equal(faultDemo, expectedFaultDemo, 'only the managed fault-demo chart pins should change');
    for (const path of paths.filter((path) => path !== 'README.md' && path !== 'cilium-demos/fault-demo/README.md')) {
      assert.equal(readFileSync(join(directory, path), 'utf8'), originalDocs.get(path), `${path} should remain unchanged`);
    }
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('update-docs handles identical old app and chart versions with different new versions', () => {
  const result = updateDocsFixture({ oldApp: '0.7.16', newApp: '0.7.18', oldChart: '0.7.16', newChart: '0.7.19' });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.docs['README.md'], /raw manifests use app `0\.7\.18`, matching chart `0\.7\.19`/);
  assert.match(result.docs['README.md'], /--version 0\.7\.19` to reproduce/);
  assert.match(result.docs['README.md'], /Example release: app `0\.7\.16`, chart `0\.7\.16`/);
  assert.match(result.docs['cilium-demos/fault-demo/README.md'], /export CHART_VERSION=0\.7\.19/);
  assert.match(result.docs['cilium-demos/fault-demo/README.md'], /Example chart release: `0\.7\.16`/);
});

test('update-docs leaves an unchanged version axis intact while updating the other axis', () => {
  const result = updateDocsFixture({ oldApp: '0.7.16', newApp: '0.7.18', oldChart: '0.7.19', newChart: '0.7.19' });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.docs['README.md'], /raw manifests use app `0\.7\.18`, matching chart `0\.7\.19`/);
  assert.match(result.docs['README.md'], /--version 0\.7\.19` to reproduce/);
  assert.match(result.docs['cilium-demos/fault-demo/README.md'], /export CHART_VERSION=0\.7\.19/);
});

test('update-docs fails when an expected managed version pin is missing', () => {
  const result = updateDocsFixture({ oldApp: '0.7.16', newApp: '0.7.18', oldChart: '0.7.19', newChart: '0.7.20', missingChartEnvPin: true });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /expected documented CHART_VERSION is missing from cilium-demos\/fault-demo\/README\.md/);
  assert.deepEqual(result.docs, result.initialDocs);
});

test('frozen legacy state remains an explicit immutable baseline', () => {
  assert.equal(LEGACY_PROMOTION_STATE.kind, 'pacman-k8s-promotion-state');
  assert.equal(LEGACY_PROMOTION_STATE.app.version, '0.7.16');
  assert.equal(LEGACY_PROMOTION_STATE.app.reference, 'https://github.com/saintdle/pacman/releases/download/pacman-v0.7.16/release-metadata.json');
  assert.equal(LEGACY_PROMOTION_STATE.app.sha256, 'ef15decce2fc0cb9aa0b14d4a2d059e10eb13638afc57fe99277b8688aea4c4e');
  assert.equal(LEGACY_PROMOTION_STATE.app.image_manifest_list_digest, 'sha256:592740129bce7530a347146b86647f8b516df3c19105aa4c2fe40b878840fccf');
  assert.equal(LEGACY_PROMOTION_STATE.chart.version, '0.7.17');
  assert.equal(LEGACY_PROMOTION_STATE.chart.package_sha256, 'b0722ab17320afe14d5bee5ebf25ed26c9dcfa59f24e238764c4b2d8a76eb971');
  assert.deepEqual(planConsumerPromotion({ current: LEGACY_PROMOTION_STATE, candidate: LEGACY_PROMOTION_STATE }), { action: 'noop', reason: 'already-promoted' });
  assert.deepEqual(LEGACY_PROMOTION_STATE.app.missing_evidence, ['app-release-record', 'platform-descriptors', 'provenance', 'signature']);
});

test('tracked promotion state, optional compatibility lock, and managed references stay coherent', () => {
  const lockPath = join(repositoryRoot, 'consumer-compatibility-lock.json');
  const compatibilityLock = existsSync(lockPath) ? JSON.parse(readFileSync(lockPath, 'utf8')) : null;
  const image = `${trackedState.app.image_repository}@${trackedState.app.image_manifest_list_digest}`;
  assert.equal(assertManagedConsumerReferences({ state: trackedState, compatibilityLock, references: trackedManagedReferences() }), image);
  const check = spawnSync(process.execPath, [promotionCli, 'check-references', join(repositoryRoot, 'pacman-promotion-state.json'), lockPath], {
    cwd: repositoryRoot, encoding: 'utf8',
  });
  assert.equal(check.status, 0, check.stderr);
  if (trackedState.app.format === 'app-release-v1' && trackedState.chart.format === 'chart-release-v1') {
    assert.deepEqual(compatibilityLock, syntheticCompatibilityLock(trackedState));
  } else {
    assert.equal(compatibilityLock, null);
    assert.deepEqual(trackedState.app.missing_evidence, ['app-release-record', 'platform-descriptors', 'provenance', 'signature']);
  }
  assert.deepEqual(planConsumerPromotion({ current: trackedState, candidate: trackedState }), { action: 'noop', reason: 'already-promoted' });
});

test('legacy state accepts matching references and rejects one stale generic image pin', () => {
  const currentImage = `${LEGACY_PROMOTION_STATE.app.image_repository}@${LEGACY_PROMOTION_STATE.app.image_manifest_list_digest}`;
  const references = managedReferences(LEGACY_PROMOTION_STATE);
  assert.equal(assertManagedConsumerReferences({ state: LEGACY_PROMOTION_STATE, compatibilityLock: null, references }), currentImage);
  const drifted = references.map((reference) => ({ ...reference }));
  const driftedReference = drifted.find((reference) => reference.path === 'deployments/pacman-deployment.yaml');
  driftedReference.content = driftedReference.content.replace(currentImage, `docker.io/saintdle/pacman@sha256:${'a'.repeat(64)}`);
  assert.throws(() => assertManagedConsumerReferences({ state: LEGACY_PROMOTION_STATE, compatibilityLock: null, references: drifted }), /deployments\/pacman-deployment.yaml.*does not match compatibility state/);
  const mutable = references.map((reference) => ({ ...reference }));
  const mutableReference = mutable.find((reference) => reference.path === 'deployments/pacman-deployment.yaml');
  mutableReference.content = mutableReference.content.replace(currentImage, 'docker.io/saintdle/pacman:latest');
  assert.throws(() => assertManagedConsumerReferences({ state: LEGACY_PROMOTION_STATE, compatibilityLock: null, references: mutable }), /deployments\/pacman-deployment.yaml.*does not match compatibility state/);
  const spacedKey = references.map((reference) => ({ ...reference }));
  const spacedReference = spacedKey.find((reference) => reference.path === 'cilium-demos/microservices-east-west/workloads.yaml');
  spacedReference.content = spacedReference.content.replace(`image: ${currentImage}`, 'image : docker.io/saintdle/pacman:latest');
  assert.throws(() => assertManagedConsumerReferences({ state: LEGACY_PROMOTION_STATE, compatibilityLock: null, references: spacedKey }), /microservices-east-west\/workloads.yaml.*does not match compatibility state/);
  const quotedKey = references.map((reference) => ({ ...reference }));
  const quotedReference = quotedKey.find((reference) => reference.path === 'cilium-demos/microservices-east-west/workloads.yaml');
  quotedReference.content = quotedReference.content.replace(`image: ${currentImage}`, `"image": ${currentImage}`);
  assert.equal(assertManagedConsumerReferences({ state: LEGACY_PROMOTION_STATE, compatibilityLock: null, references: quotedKey }), currentImage);
  const missingImage = references.map((reference) => ({ ...reference }));
  const missingReference = missingImage.find((reference) => reference.path === 'deployments/pacman-deployment.yaml');
  const missingLines = missingReference.content.split('\n');
  missingLines.splice(missingLines.findIndex((line) => line.trimStart().startsWith('image:')), 1);
  missingReference.content = missingLines.join('\n');
  assert.throws(() => assertManagedConsumerReferences({ state: LEGACY_PROMOTION_STATE, compatibilityLock: null, references: missingImage }), /deployments\/pacman-deployment.yaml.*must contain 3 Pac-Man containers; found 2/);
});

test('future compatibility locks are checked against both synthetic typed identities', () => {
  const state = syntheticTypedState();
  const compatibilityLock = syntheticCompatibilityLock(state);
  const references = managedReferences(state);
  assert.equal(assertManagedConsumerReferences({ state, compatibilityLock, references }), `${state.app.image_repository}@${state.app.image_manifest_list_digest}`);
  assert.throws(() => assertManagedConsumerReferences({ state, compatibilityLock: { ...compatibilityLock, chart: { ...compatibilityLock.chart, version: '9.9.9' } }, references }), /compatibility lock does not match promotion state/);
});

test('promotion state, lock, and references move together when either identity axis changes', () => {
  const syntheticState = syntheticTypedState();
  const originalLock = syntheticCompatibilityLock(syntheticState);
  const cases = [
    (state) => {
      state.app.version = '0.7.18';
      state.app.reference = 'https://github.com/saintdle/pacman/releases/download/pacman-v0.7.18/app-release-record.json';
      state.app.sha256 = 'a'.repeat(64);
      state.app.source_commit = 'b'.repeat(40);
      state.app.image_manifest_list_digest = `sha256:${'c'.repeat(64)}`;
    },
    (state) => {
      state.chart.version = '0.7.18';
      state.chart.reference = 'https://github.com/saintdle/helm-charts/releases/download/helm-chart-v0.7.18/chart-release-record.json';
      state.chart.sha256 = 'd'.repeat(64);
      state.chart.package_reference = 'https://github.com/saintdle/helm-charts/releases/download/helm-chart-v0.7.18/pacman-0.7.18.tgz';
      state.chart.package_sha256 = 'e'.repeat(64);
      state.chart.source_commit = 'f'.repeat(40);
    },
  ];
  for (const changeAxis of cases) {
    const state = structuredClone(syntheticState);
    changeAxis(state);
    const currentLock = syntheticCompatibilityLock(state);
    const references = managedReferences(state);
    assert.equal(assertManagedConsumerReferences({ state, compatibilityLock: currentLock, references }), `${state.app.image_repository}@${state.app.image_manifest_list_digest}`);
    assert.throws(() => assertManagedConsumerReferences({ state, compatibilityLock: originalLock, references }), /compatibility lock does not match promotion state/);
  }
});

test('promotion writes one coherent synthetic state and lock consumed by the production checker', () => {
  const directory = mkdtempSync(join(tmpdir(), 'pacman-coherent-consumer-promotion-'));
  try {
    execFileSync('git', ['init', '-q'], { cwd: directory });
    const state = syntheticTypedState();
    state.app.version = '0.7.18';
    state.app.reference = 'https://github.com/saintdle/pacman/releases/download/pacman-v0.7.18/app-release-record.json';
    state.app.sha256 = '3'.repeat(64);
    state.app.source_commit = '4'.repeat(40);
    state.app.image_manifest_list_digest = `sha256:${'5'.repeat(64)}`;
    state.chart.version = '0.7.18';
    state.chart.reference = 'https://github.com/saintdle/helm-charts/releases/download/helm-chart-v0.7.18/chart-release-record.json';
    state.chart.sha256 = '6'.repeat(64);
    state.chart.package_reference = 'https://github.com/saintdle/helm-charts/releases/download/helm-chart-v0.7.18/pacman-0.7.18.tgz';
    state.chart.package_sha256 = '7'.repeat(64);
    state.chart.source_commit = '8'.repeat(40);
    for (const reference of managedReferences(state)) {
      const path = join(directory, reference.path);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, reference.content);
    }
    const planPath = join(directory, 'plan.json');
    const statePath = join(directory, 'pacman-promotion-state.json');
    writeFileSync(planPath, JSON.stringify({ action: 'promotion', app: state.app, chart: state.chart, compatibility_lock: syntheticCompatibilityLock(state) }));
    const written = spawnSync(process.execPath, [promotionCli, 'write-state', planPath, statePath], { cwd: directory, encoding: 'utf8' });
    assert.equal(written.status, 0, written.stderr);
    execFileSync('git', ['add', ...MANAGED_IMAGE_REFERENCE_PATHS, ...MANAGED_VERSION_REFERENCE_PATHS], { cwd: directory });
    const check = spawnSync(process.execPath, [promotionCli, 'check-references', statePath, join(directory, 'consumer-compatibility-lock.json')], { cwd: directory, encoding: 'utf8' });
    assert.equal(check.status, 0, check.stderr);
    assert.deepEqual(JSON.parse(readFileSync(statePath, 'utf8')).app, state.app);
    assert.deepEqual(JSON.parse(readFileSync(join(directory, 'consumer-compatibility-lock.json'), 'utf8')), syntheticCompatibilityLock(state));
    const manifestPath = join(directory, 'deployments/pacman-deployment.yaml');
    const matchingManifest = readFileSync(manifestPath, 'utf8');
    writeFileSync(manifestPath, matchingManifest.replace(new RegExp(`${state.app.image_repository.replaceAll('.', '\\.')}@${state.app.image_manifest_list_digest}`), 'docker.io/saintdle/pacman:latest'));
    const mutable = spawnSync(process.execPath, [promotionCli, 'check-references', statePath, join(directory, 'consumer-compatibility-lock.json')], { cwd: directory, encoding: 'utf8' });
    assert.notEqual(mutable.status, 0);
    assert.match(mutable.stderr, /deployments\/pacman-deployment\.yaml.*does not match compatibility state/);
    writeFileSync(manifestPath, matchingManifest);
    writeFileSync(manifestPath, matchingManifest.replace('image:', 'image :').replace(new RegExp(`${state.app.image_repository.replaceAll('.', '\\.')}@${state.app.image_manifest_list_digest}`), 'docker.io/saintdle/pacman:latest'));
    const spacedKey = spawnSync(process.execPath, [promotionCli, 'check-references', statePath, join(directory, 'consumer-compatibility-lock.json')], { cwd: directory, encoding: 'utf8' });
    assert.notEqual(spacedKey.status, 0);
    assert.match(spacedKey.stderr, /deployments\/pacman-deployment\.yaml.*does not match compatibility state/);
    writeFileSync(manifestPath, matchingManifest);
    const restored = spawnSync(process.execPath, [promotionCli, 'check-references', statePath, join(directory, 'consumer-compatibility-lock.json')], { cwd: directory, encoding: 'utf8' });
    assert.equal(restored.status, 0, restored.stderr);
    const versionPath = join(directory, 'cilium-demos/fault-demo/README.md');
    const matchingVersion = readFileSync(versionPath, 'utf8');
    writeFileSync(versionPath, matchingVersion.replace(`export CHART_VERSION=${state.chart.version}`, 'export CHART_VERSION=9.9.9'));
    const staleVersion = spawnSync(process.execPath, [promotionCli, 'check-references', statePath, join(directory, 'consumer-compatibility-lock.json')], { cwd: directory, encoding: 'utf8' });
    assert.notEqual(staleVersion.status, 0);
    assert.match(staleVersion.stderr, /CHART_VERSION.*does not match promotion state/);
    writeFileSync(versionPath, matchingVersion);
    const coherent = spawnSync(process.execPath, [promotionCli, 'check-references', statePath, join(directory, 'consumer-compatibility-lock.json')], { cwd: directory, encoding: 'utf8' });
    assert.equal(coherent.status, 0, coherent.stderr);
  } finally { rmSync(directory, { recursive: true, force: true }); }
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

test('every promotion PR runs the consumer drift contract and lifecycle workflow', () => {
  const workflow = readFileSync(new URL('../.github/workflows/lint.yml', import.meta.url), 'utf8');
  assert.match(workflow, /pull_request:\n/);
  assert.doesNotMatch(workflow, /pull_request:\n\s+branches:/);
  assert.match(workflow, /node --test tests\/pacman-promotion\.test\.mjs/);
  assert.match(workflow, /node tools\/pacman-promotion\.mjs check-references/);
  assert.match(workflow, /bash tests\/lifecycle\.sh/);
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

test('workflow stages only managed outputs when Python bytecode and unrelated files exist', () => {
  const workflow = readFileSync(promotionWorkflow, 'utf8');
  assert.match(workflow, /PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests\.kubeconform_schema_test/);
  assert.match(workflow, /add-paths: \$\{\{ steps\.proposal-paths\.outputs\.paths \}\}/);

  const stepStart = workflow.indexOf('- name: Select exact generated files for proposal');
  const nextStep = workflow.indexOf('\n      - name:', stepStart);
  assert.notEqual(stepStart, -1, 'exact proposal path selection step is present');
  assert.notEqual(nextStep, -1, 'proposal path selection step has a following action');
  const step = workflow.slice(stepStart, nextStep);
  const runStart = step.indexOf('run: |\n');
  assert.notEqual(runStart, -1, 'proposal path selection uses a shell block');
  const script = step.slice(runStart + 'run: |\n'.length).split('\n')
    .map((line) => line.replace(/^ {10}/, '')).join('\n');
  const configuredList = /paths=\(\n([\s\S]*?)\n\)/.exec(script)?.[1];
  assert.ok(configuredList, 'the workflow declares its managed output allowlist');
  const configuredPaths = configuredList.split('\n').map((path) => path.trim()).filter(Boolean);
  const expectedPaths = [...new Set([
    ...MANAGED_IMAGE_REFERENCE_PATHS,
    ...MANAGED_VERSION_REFERENCE_PATHS,
    'consumer-compatibility-lock.json',
    'pacman-promotion-state.json',
  ])];
  assert.equal(expectedPaths.length, 16);
  assert.deepEqual(configuredPaths, expectedPaths);

  const directory = mkdtempSync(join(tmpdir(), 'pacman-proposal-stage-test-'));
  try {
    execFileSync('git', ['init', '-q'], { cwd: directory });
    for (const path of expectedPaths.filter((path) => path !== 'consumer-compatibility-lock.json')) {
      const fullPath = join(directory, path);
      mkdirSync(dirname(fullPath), { recursive: true });
      writeFileSync(fullPath, `generated ${path}\n`);
    }
    const bytecodePath = join(directory, 'tests/__pycache__/kubeconform_schema_test.cpython-312.pyc');
    mkdirSync(dirname(bytecodePath), { recursive: true });
    writeFileSync(bytecodePath, 'seeded bytecode');
    writeFileSync(join(directory, 'unrelated-generated-output.txt'), 'unrelated');

    const githubOutput = join(directory, 'github-output.txt');
    const selected = spawnSync('bash', ['-e', '-o', 'pipefail', '-c', script], {
      cwd: directory, encoding: 'utf8', env: { ...process.env, GITHUB_OUTPUT: githubOutput },
    });
    assert.equal(selected.status, 0, selected.stderr);
    const selectedPaths = /^paths<<EOF\n([\s\S]*?)\nEOF$/m.exec(readFileSync(githubOutput, 'utf8'))?.[1].split('\n');
    assert.deepEqual(selectedPaths, expectedPaths.filter((path) => path !== 'consumer-compatibility-lock.json'));
    execFileSync('git', ['add', '--', ...selectedPaths], { cwd: directory });
    const stagedPaths = execFileSync('git', ['diff', '--cached', '--name-only', '--'], { cwd: directory, encoding: 'utf8' })
      .trim().split('\n').filter(Boolean).sort();
    assert.deepEqual(stagedPaths, [...selectedPaths].sort());
    assert.ok(!stagedPaths.includes('tests/__pycache__/kubeconform_schema_test.cpython-312.pyc'));
    assert.ok(!stagedPaths.includes('unrelated-generated-output.txt'));
  } finally { rmSync(directory, { recursive: true, force: true }); }

  const trackedLockDirectory = mkdtempSync(join(tmpdir(), 'pacman-proposal-lock-removal-test-'));
  try {
    execFileSync('git', ['init', '-q'], { cwd: trackedLockDirectory });
    for (const path of expectedPaths) {
      const fullPath = join(trackedLockDirectory, path);
      mkdirSync(dirname(fullPath), { recursive: true });
      writeFileSync(fullPath, `generated ${path}\n`);
    }
    execFileSync('git', ['add', '--', ...expectedPaths], { cwd: trackedLockDirectory });
    execFileSync('git', ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'seed proposal outputs'], {
      cwd: trackedLockDirectory,
    });
    rmSync(join(trackedLockDirectory, 'consumer-compatibility-lock.json'));
    const bytecodePath = join(trackedLockDirectory, 'tests/__pycache__/kubeconform_schema_test.cpython-312.pyc');
    mkdirSync(dirname(bytecodePath), { recursive: true });
    writeFileSync(bytecodePath, 'seeded bytecode');
    writeFileSync(join(trackedLockDirectory, 'unrelated-generated-output.txt'), 'unrelated');

    const githubOutput = join(trackedLockDirectory, 'github-output.txt');
    const selected = spawnSync('bash', ['-e', '-o', 'pipefail', '-c', script], {
      cwd: trackedLockDirectory, encoding: 'utf8', env: { ...process.env, GITHUB_OUTPUT: githubOutput },
    });
    assert.equal(selected.status, 0, selected.stderr);
    const selectedPaths = /^paths<<EOF\n([\s\S]*?)\nEOF$/m.exec(readFileSync(githubOutput, 'utf8'))?.[1].split('\n');
    assert.deepEqual(selectedPaths, expectedPaths);
    execFileSync('git', ['add', '--', ...selectedPaths], { cwd: trackedLockDirectory });
    const stagedChanges = execFileSync('git', ['diff', '--cached', '--name-status', '--'], {
      cwd: trackedLockDirectory, encoding: 'utf8',
    }).trim().split('\n').filter(Boolean);
    assert.deepEqual(stagedChanges, ['D\tconsumer-compatibility-lock.json']);
    const stagedPaths = execFileSync('git', ['ls-files', '--cached', '--', 'tests/__pycache__/kubeconform_schema_test.cpython-312.pyc', 'unrelated-generated-output.txt'], {
      cwd: trackedLockDirectory, encoding: 'utf8',
    }).trim();
    assert.equal(stagedPaths, '');
  } finally { rmSync(trackedLockDirectory, { recursive: true, force: true }); }
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

test('rollback package URL parser captures canonical stable 0.x and 1.x versions', () => {
  assert.deepEqual(parseRollbackChartReference('https://github.com/saintdle/helm-charts/releases/download/pacman-0.7.17/pacman-0.7.17.tgz'), {
    tag: 'pacman-0.7.17', filename: 'pacman-0.7.17.tgz', chartVersion: '0.7.17',
  });
  assert.deepEqual(parseRollbackChartReference('https://github.com/saintdle/helm-charts/releases/download/helm-chart-v1.7.17/pacman-1.7.17.tgz'), {
    tag: 'helm-chart-v1.7.17', filename: 'pacman-1.7.17.tgz', chartVersion: '1.7.17',
  });
  for (const reference of [
    'https://github.com/saintdle/helm-charts/releases/download/pacman-0.7.17/pacman-0.7.18.tgz',
    'https://github.com/saintdle/helm-charts/releases/download/pacman-0.07.17/pacman-0.07.17.tgz',
    'https://github.com/saintdle/helm-charts/releases/download/pacman-0.7.017/pacman-0.7.017.tgz',
    'https://github.com/saintdle/helm-charts/releases/download/pacman-00.7.17/pacman-00.7.17.tgz',
    'https://github.com/example/helm-charts/releases/download/pacman-0.7.17/pacman-0.7.17.tgz',
  ]) assert.throws(() => parseRollbackChartReference(reference), /rollback chart (reference|tag)/);
});

test('rollback candidate selects and preserves a verified generated chart record and v1 lock', async () => {
  const fixtureCase = rollbackFixture();
  const candidate = await rollbackCandidate(fixtureCase.options);
  assert.deepEqual(fixtureCase.requestedAssets, ['pacman-0.7.16.tgz', 'chart-release-record.json']);
  assert.deepEqual(candidate.chart, {
    version: '0.7.16', reference: fixtureCase.recordReference,
    sha256: fixtureCase.recordSha,
    package_reference: fixtureCase.packageReference, package_sha256: fixtureCase.packageSha,
    source_repository: 'https://github.com/saintdle/pacman', source_commit: 'b'.repeat(40), format: 'chart-release-v1',
  });
  assert.equal(candidate.app.format, 'app-release-v1');
  assert.equal(candidate.app.reference, fixtureCase.appReference);
  const plan = planPromotionCandidate({ current, candidate, mode: 'rollback', reason: fixtureCase.options.reason });
  assert.equal(plan.action, 'rollback-proposal');
  const lock = plan.compatibility_lock;
  assert.ok(lock);
  assert.equal(lock.chart.reference, fixtureCase.recordReference);
  assert.equal(lock.chart.package_reference, fixtureCase.packageReference);
  assert.equal(lock.chart.source_repository, 'https://github.com/saintdle/pacman');
  const state = { ...trackedState, app: candidate.app, chart: candidate.chart };
  assert.equal(assertManagedConsumerReferences({ state, compatibilityLock: lock, references: managedReferences(state) }),
    `${candidate.app.image_repository}@${candidate.app.image_manifest_list_digest}`);
});

test('rollback rejects malformed or forged generated chart records without legacy fallback', async () => {
  const invalidRecords = [
    { mutate: (record) => ({ ...record, source: { ...record.source, repository: 'https://github.com/attacker/pacman' } }), message: /source identity is invalid/ },
    { mutate: (record) => ({ ...record, source: { ...record.source, commit: 'not-a-commit' } }), message: /source identity is invalid/ },
    { mutate: (record) => ({ ...record, package: { ...record.package, immutable_url: 'https://example.invalid/chart.tgz' } }), message: /package identity or checksum is invalid/ },
    { mutate: (record) => ({ ...record, package: { ...record.package, sha256: 'c'.repeat(64) } }), message: /package identity or checksum is invalid/ },
    { mutate: (record) => ({ ...record, app_release: { ...record.app_release, unexpected: true } }), message: /chart app release identity has an invalid schema/ },
    { mutate: (record) => ({ ...record, unexpected: true }), message: /invalid schema/ },
  ];
  for (const invalid of invalidRecords) {
    const fixtureCase = rollbackFixture({ alterRecord: invalid.mutate });
    await assert.rejects(rollbackCandidate(fixtureCase.options), invalid.message);
    assert.deepEqual(fixtureCase.requestedAssets, ['pacman-0.7.16.tgz', 'chart-release-record.json']);
  }
  const mismatchedRecordReference = rollbackFixture();
  const getVerifiedAsset = mismatchedRecordReference.options.getVerifiedAsset;
  mismatchedRecordReference.options.getVerifiedAsset = async (releaseData, name) => {
    const verified = await getVerifiedAsset(releaseData, name);
    return name === 'chart-release-record.json' ? { ...verified, url: `${verified.url}.forged` } : verified;
  };
  await assert.rejects(rollbackCandidate(mismatchedRecordReference.options), /record reference is not immutable/);

  const malformed = rollbackFixture();
  const getVerifiedAssetForMalformed = malformed.options.getVerifiedAsset;
  malformed.options.getVerifiedAsset = async (_release, name) => {
    if (name === 'chart-release-record.json') {
      malformed.requestedAssets.push(name);
      return { data: Buffer.from('{'), checksum: 'a'.repeat(64), url: malformed.recordReference };
    }
    return getVerifiedAssetForMalformed(malformed.releaseData, name);
  };
  await assert.rejects(rollbackCandidate(malformed.options));
  assert.deepEqual(malformed.requestedAssets, ['pacman-0.7.16.tgz', 'chart-release-record.json']);

  const mismatchedApp = rollbackFixture();
  mismatchedApp.options.appRef = 'https://github.com/saintdle/pacman/releases/download/pacman-v0.7.15/release-metadata.json';
  await assert.rejects(rollbackCandidate(mismatchedApp.options), /does not match chart release record/);
});

test('rollback retains explicit legacy chart compatibility and missing evidence', async () => {
  const fixtureCase = rollbackFixture({ generated: false });
  const candidate = await rollbackCandidate(fixtureCase.options);
  assert.deepEqual(fixtureCase.requestedAssets, ['pacman-0.7.16.tgz']);
  assert.equal(candidate.chart.reference, fixtureCase.packageReference);
  assert.equal(candidate.chart.format, 'legacy-chart-package-v0');
  assert.deepEqual(candidate.chart.missing_evidence, ['chart-release-record', 'chart-source-identity']);
  const plan = planPromotionCandidate({ current, candidate, mode: 'rollback', reason: fixtureCase.options.reason });
  assert.equal(plan.compatibility_lock, null);
});
