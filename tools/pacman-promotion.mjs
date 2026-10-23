import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const chartRepo = 'saintdle/helm-charts';
const appRepo = 'saintdle/pacman';
const imageRepo = 'docker.io/saintdle/pacman';
const versionRe = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const digestRe = /^sha256:[a-f0-9]{64}$/;
const shaRe = /^[a-f0-9]{64}$/;
const commitRe = /^[a-f0-9]{40}$/;

function fail(message) { throw new Error(message); }
function version(value, name = 'version') {
  if (typeof value !== 'string' || !versionRe.test(value)) fail(`${name} must be canonical stable SemVer`);
  return value.split('.').map(Number);
}
export function compareVersion(left, right) {
  const a = version(left), b = version(right);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] - b[i];
  return 0;
}
function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function exactKeys(value, keys, name) {
  if (!isObject(value) || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) fail(`${name} has an invalid schema`);
}
function checksum(bytes) { return createHash('sha256').update(bytes).digest('hex'); }
function releaseAssetUrl(repo, tag, name) { return `https://github.com/${repo}/releases/download/${tag}/${name}`; }
export function validateAppReleaseMetadata(bytes, expected) {
  const metadata = JSON.parse(bytes.toString());
  exactKeys(metadata, ['version', 'tag', 'commit', 'image_digest', 'app_record_ref', 'app_record_sha256'], 'release metadata');
  for (const key of ['version', 'tag', 'commit', 'image_digest', 'app_record_ref', 'app_record_sha256']) {
    if (metadata[key] !== expected[key]) fail('release metadata does not pin the app record bytes and identity');
  }
  return metadata;
}
function hasAppliedRollbackState(state) {
  return state?.mode === 'rollback'
    && typeof state.app?.reference === 'string' && shaRe.test(state.app?.sha256 ?? '')
    && typeof state.chart?.reference === 'string' && shaRe.test(state.chart?.sha256 ?? '')
    && state.rollback?.target_app_reference === state.app.reference
    && state.rollback?.target_app_sha256 === state.app.sha256
    && state.rollback?.target_chart_reference === state.chart.reference
    && state.rollback?.target_chart_sha256 === state.chart.sha256
    && typeof state.rollback?.reason === 'string' && state.rollback.reason.trim().length >= 8;
}
function versionFromAsset(reference) {
  const match = /\/releases\/download\/pacman-v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))\/(?:app-release-record|release-metadata)\.json$/.exec(reference ?? '');
  if (!match) fail('app release reference does not identify a supported immutable release asset');
  return match[1];
}

export function planConsumerPromotion({ current, candidate, pending, mode = 'reconcile', reason = '' }) {
  if (!current?.app || !current?.chart || !candidate?.app || !candidate?.chart) fail('promotion identities are incomplete');
  version(current.app.version); version(current.chart.version); version(candidate.app.version); version(candidate.chart.version);
  if (!digestRe.test(current.app.image_manifest_list_digest ?? '') || !digestRe.test(candidate.app.image_manifest_list_digest ?? '')) fail('app identity needs a manifest-list digest');
  if (!shaRe.test(current.chart.package_sha256 ?? '') || !shaRe.test(candidate.chart.package_sha256 ?? '')) fail('chart identity needs a package checksum');
  if (!['reconcile', 'rollback', 'resume'].includes(mode)) fail('unsupported promotion mode');
  const appOrder = compareVersion(candidate.app.version, current.app.version);
  const chartOrder = compareVersion(candidate.chart.version, current.chart.version);
  const assertSameIdentity = (axis, left, right, fields) => {
    for (const [field, label] of fields) {
      const a = left[field], b = right[field];
      if (a !== undefined || b !== undefined) if (!a || !b || a !== b) fail(`same ${axis} version has a conflicting ${label}`);
    }
  };
  if (mode === 'rollback') {
    if (appOrder > 0 || chartOrder > 0 || (appOrder === 0 && chartOrder === 0)) fail('rollback target must be an older explicit app or chart identity');
    if (reason.trim().length < 8) fail('rollback proposal requires a reason of at least 8 characters');
    return { action: 'rollback-proposal', app: candidate.app, chart: candidate.chart, rollback_reason: reason.trim() };
  }
  if (mode === 'resume') {
    if (!hasAppliedRollbackState(current)) fail('resume requires an applied rollback state');
    if (appOrder < 0 || chartOrder < 0) fail('resume candidate cannot be older than the applied rollback state');
    if (appOrder === 0) assertSameIdentity('app', candidate.app, current.app, [
      ['image_manifest_list_digest', 'image digest'], ['reference', 'record reference'], ['sha256', 'record checksum'], ['source_commit', 'source commit'],
    ]);
    if (chartOrder === 0) assertSameIdentity('chart', candidate.chart, current.chart, [
      ['package_sha256', 'package checksum'], ['reference', 'record reference'], ['sha256', 'record checksum'],
    ]);
    return { action: 'resume', app: candidate.app, chart: candidate.chart };
  }
  if (appOrder < 0 || chartOrder < 0) return { action: 'noop', reason: 'older-release-event' };
  if (appOrder === 0) assertSameIdentity('app', candidate.app, current.app, [
    ['image_manifest_list_digest', 'image digest'], ['reference', 'record reference'], ['sha256', 'record checksum'], ['source_commit', 'source commit'],
  ]);
  if (chartOrder === 0) assertSameIdentity('chart', candidate.chart, current.chart, [
    ['package_sha256', 'package checksum'], ['reference', 'record reference'], ['sha256', 'record checksum'],
  ]);
  if (pending) {
    const pendingAppOrder = compareVersion(pending.app.version, candidate.app.version);
    const pendingChartOrder = compareVersion(pending.chart.version, candidate.chart.version);
    if (pendingAppOrder > 0 || pendingChartOrder > 0) return { action: 'noop', reason: 'newer-coherent-promotion-is-pending' };
    if (pendingAppOrder === 0) assertSameIdentity('pending app', pending.app, candidate.app, [
      ['image_manifest_list_digest', 'image digest'], ['reference', 'record reference'], ['sha256', 'record checksum'], ['source_commit', 'source commit'],
    ]);
    if (pendingChartOrder === 0) assertSameIdentity('pending chart', pending.chart, candidate.chart, [
      ['package_sha256', 'package checksum'], ['reference', 'record reference'], ['sha256', 'record checksum'],
    ]);
  }
  if (appOrder === 0 && chartOrder === 0) return { action: 'noop', reason: 'already-promoted' };
  return { action: 'promotion', app: candidate.app, chart: candidate.chart };
}

export function selectPendingProposalState({ state, pullRequests, branchSha, baseSha, stateKind = 'pacman-k8s-promotion-state' }) {
  if (!isObject(state) || state.schema_version !== 1 || state.kind !== stateKind
    || !['automatic', 'rollback'].includes(state.mode) || !Array.isArray(pullRequests)
    || !digestRe.test(state.app?.image_manifest_list_digest ?? '') || !shaRe.test(state.chart?.package_sha256 ?? '')) return null;
  try { version(state.app.version, 'pending app version'); version(state.chart.version, 'pending chart version'); }
  catch { return null; }
  const matches = pullRequests.filter((proposal) => proposal?.state === 'OPEN' && proposal.isCrossRepository === false
    && proposal.headRefName === 'automation/pacman-promotion' && proposal.headRefOid === branchSha
    && proposal.baseRefName === 'main' && proposal.baseRefOid === baseSha);
  return matches.length === 1 ? state : null;
}

export function rollbackRequiresExplicitResume({ currentState, pendingState, mode }) {
  if (mode === 'resume') return !hasAppliedRollbackState(currentState);
  return mode !== 'rollback' && (currentState?.mode === 'rollback' || pendingState?.mode === 'rollback');
}

export function validateChartRecord(record, bytes, release, packageBytes) {
  exactKeys(record, ['schema_version', 'kind', 'chart_version', 'source', 'package', 'app_release'], 'chart release record');
  if (record.schema_version !== 1 || record.kind !== 'chart-release') fail('unsupported chart record schema');
  version(record.chart_version, 'chart_version');
  const tag = release.tag_name;
  if (![ `pacman-${record.chart_version}`, `helm-chart-v${record.chart_version}` ].includes(tag)) fail('chart tag does not match chart_version');
  exactKeys(record.source, ['repository', 'commit'], 'chart source');
  if (record.source.repository !== 'https://github.com/saintdle/pacman' || !commitRe.test(record.source.commit)) fail('chart source identity is invalid');
  exactKeys(record.package, ['filename', 'sha256', 'immutable_url'], 'chart package');
  const expectedPackageUrl = releaseAssetUrl(chartRepo, tag, `pacman-${record.chart_version}.tgz`);
  if (record.package.filename !== `pacman-${record.chart_version}.tgz` || record.package.immutable_url !== expectedPackageUrl
    || checksum(packageBytes) !== record.package.sha256) fail('chart package identity or checksum is invalid');
  const appVersion = versionFromAsset(record.app_release?.reference);
  if (record.app_release.reference !== releaseAssetUrl(appRepo, `pacman-v${appVersion}`, record.app_release.reference.endsWith('/app-release-record.json') ? 'app-release-record.json' : 'release-metadata.json')) fail('chart app reference is not immutable');
  if (!shaRe.test(record.app_release?.sha256 ?? '') || !bytes?.length) fail('chart record checksum or bytes are missing');
  record.app_release.version = appVersion;
  return record;
}

async function api(path) {
  const response = await fetch(`https://api.github.com${path}`, { headers: {
    Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
    ...(process.env.GITHUB_TOKEN ? { Authorization: `Bearer ${process.env.GITHUB_TOKEN}` } : {}),
  } });
  if (!response.ok) fail(`GitHub API request failed (${response.status})`);
  return response;
}
async function bytes(url) {
  const response = await fetch(url);
  if (!response.ok) fail(`immutable release asset request failed (${response.status})`);
  return Buffer.from(await response.arrayBuffer());
}
async function release(repo, tag) { return (await api(`/repos/${repo}/releases/tags/${encodeURIComponent(tag)}`)).json(); }
async function tagCommit(repo, tag) {
  let object = (await api(`/repos/${repo}/git/ref/tags/${encodeURIComponent(tag)}`)).json().then((ref) => ref.object);
  object = await object;
  for (let depth = 0; object?.type === 'tag' && depth < 5; depth++) object = (await api(`/repos/${repo}/git/tags/${object.sha}`)).json().then((entry) => entry.object).then((entry) => entry);
  if (object?.type !== 'commit' || !commitRe.test(object.sha)) fail(`release tag ${tag} does not resolve to a commit`);
  return object.sha;
}
function asset(releaseData, name) {
  const found = releaseData.assets.filter((item) => item.name === name);
  if (found.length !== 1) fail(`${releaseData.tag_name} must contain exactly one ${name}`);
  return found[0];
}
async function verifiedAsset(releaseData, name) {
  const meta = asset(releaseData, name);
  const data = await bytes(meta.browser_download_url);
  if (meta.digest && meta.digest !== `sha256:${checksum(data)}`) fail(`${name} checksum does not match release metadata`);
  return { data, checksum: checksum(data), url: meta.browser_download_url };
}
function verifyVersionTag(releaseData, expected) {
  if (releaseData.draft || releaseData.prerelease || releaseData.tag_name !== expected) fail(`release ${expected} is not published and stable`);
}
async function verifyApp(versionValue, expectedRef, expectedSha, expectedDigest) {
  version(versionValue, 'app version');
  const tag = `pacman-v${versionValue}`;
  const rel = await release(appRepo, tag);
  verifyVersionTag(rel, tag);
  if (expectedRef.endsWith('/app-release-record.json')) {
    const recAsset = await verifiedAsset(rel, 'app-release-record.json');
    if (recAsset.url !== expectedRef || recAsset.checksum !== expectedSha) fail('app record reference/checksum mismatch');
    const record = JSON.parse(recAsset.data.toString());
    exactKeys(record, ['schema_version', 'kind', 'app_version', 'tag', 'source', 'image', 'platforms', 'provenance_reference', 'signature_reference'], 'app release record');
    if (record.kind !== 'app-release' || record.schema_version !== 1 || record.app_version !== versionValue || record.tag !== tag
      || record.source.repository !== `https://github.com/${appRepo}` || !commitRe.test(record.source.commit)
      || record.image.repository !== imageRepo || !digestRe.test(record.image.manifest_list_digest)) fail('app release record identity is invalid');
    for (const platform of ['linux/amd64', 'linux/arm64']) if (!record.platforms.some((item) => item.platform === platform && digestRe.test(item.digest))) fail(`app record lacks ${platform}`);
    if (expectedDigest && expectedDigest !== record.image.manifest_list_digest) fail('app record conflicts with chart package');
    const metadataAsset = await verifiedAsset(rel, 'release-metadata.json');
    const pinned = { version: versionValue, tag, commit: record.source.commit, image_digest: record.image.manifest_list_digest,
      app_record_ref: recAsset.url, app_record_sha256: recAsset.checksum };
    validateAppReleaseMetadata(metadataAsset.data, pinned);
    if (await tagCommit(appRepo, tag) !== record.source.commit) fail('app record source commit does not match immutable release tag');
    const registry = execFileSync('docker', ['buildx', 'imagetools', 'inspect', `${imageRepo}@${record.image.manifest_list_digest}`], { encoding: 'utf8' });
    if (!new RegExp(`^Digest:\\s+${record.image.manifest_list_digest.replace(':', ':')}$`, 'm').test(registry)) fail('app record digest is not present in the registry');
    return { version: versionValue, reference: expectedRef, sha256: recAsset.checksum, source_commit: record.source.commit,
      source_repository: record.source.repository, image_repository: record.image.repository, image_manifest_list_digest: record.image.manifest_list_digest, format: 'app-release-v1' };
  }
  const legacy = await verifiedAsset(rel, 'release-metadata.json');
  if (legacy.url !== expectedRef || legacy.checksum !== expectedSha) fail('legacy app metadata reference/checksum mismatch');
  const metadata = JSON.parse(legacy.data.toString());
  if (metadata.version !== versionValue || metadata.tag !== tag || !commitRe.test(metadata.commit)) fail('legacy app metadata identity is invalid');
  const imageDigest = expectedDigest ?? /(?:^|\n)- Digest: (sha256:[a-f0-9]{64})(?:\n|$)/.exec(rel.body ?? '')?.[1];
  if (!digestRe.test(imageDigest ?? '')) fail('legacy app image digest has no verifiable release evidence');
  if (await tagCommit(appRepo, tag) !== metadata.commit) fail('legacy app metadata commit does not match immutable release tag');
  const registry = execFileSync('docker', ['buildx', 'imagetools', 'inspect', `${imageRepo}@${imageDigest}`], { encoding: 'utf8' });
  if (!new RegExp(`^Digest:\\s+${imageDigest}$`, 'm').test(registry)) fail('legacy app digest is not present in the registry');
  return { version: versionValue, reference: expectedRef, sha256: legacy.checksum, source_commit: metadata.commit,
    source_repository: `https://github.com/${appRepo}`, image_repository: imageRepo, image_manifest_list_digest: imageDigest,
    format: 'legacy-release-metadata-v0', missing_evidence: ['app-release-record', 'platform-descriptors', 'provenance', 'signature'] };
}
function chartPackageMetadata(packageBytes) {
  const temp = mkdtempSync(join(tmpdir(), 'pacman-chart-'));
  const packagePath = join(temp, 'chart.tgz');
  try {
    writeFileSync(packagePath, packageBytes);
    const names = execFileSync('tar', ['-tzf', packagePath], { encoding: 'utf8' }).trim().split('\n');
    const chartFile = names.find((name) => /(^|\/)Chart\.yaml$/.test(name));
    const valuesFile = names.find((name) => /(^|\/)values\.yaml$/.test(name));
    if (!chartFile || !valuesFile) fail('chart package is missing Chart.yaml or values.yaml');
    const chartYaml = execFileSync('tar', ['-xOf', packagePath, chartFile], { encoding: 'utf8' });
    const valuesYaml = execFileSync('tar', ['-xOf', packagePath, valuesFile], { encoding: 'utf8' });
    const chartVersion = execFileSync('yq', ['-r', '.version', '-'], { input: chartYaml, encoding: 'utf8' }).trim();
    const appVersion = execFileSync('yq', ['-r', '.appVersion', '-'], { input: chartYaml, encoding: 'utf8' }).trim();
    const imageDigest = execFileSync('yq', ['-r', '.image.digest', '-'], { input: valuesYaml, encoding: 'utf8' }).trim();
    return { chartVersion, appVersion, imageDigest };
  } finally { rmSync(temp, { recursive: true, force: true }); }
}
async function rollbackCandidate() {
  const packageRef = process.env.ROLLBACK_CHART_PACKAGE_REF ?? '';
  const packageSha = process.env.ROLLBACK_CHART_PACKAGE_SHA256 ?? '';
  const appRef = process.env.ROLLBACK_APP_REF ?? '';
  const appSha = process.env.ROLLBACK_APP_SHA256 ?? '';
  const reason = process.env.ROLLBACK_REASON?.trim() ?? '';
  if (!packageRef || !shaRe.test(packageSha) || !appRef || !shaRe.test(appSha) || reason.length < 8) fail('rollback requires immutable app/chart asset references, both checksums, and a reason of at least 8 characters');
  const match = /^https:\/\/github\.com\/saintdle\/helm-charts\/releases\/download\/([^/]+)\/(pacman-(0|[1-9]\d*\.\d+\.\d+)\.tgz)$/.exec(packageRef);
  if (!match) fail('rollback chart reference must be the immutable Pac-Man chart package asset');
  const [, tag, filename, chartVersion] = match;
  if (![ `pacman-${chartVersion}`, `helm-chart-v${chartVersion}` ].includes(tag)) fail('rollback chart tag/version mismatch');
  const rel = await release(chartRepo, tag);
  verifyVersionTag(rel, tag);
  const pkg = await verifiedAsset(rel, filename);
  if (pkg.url !== packageRef || pkg.checksum !== packageSha) fail('rollback chart package reference/checksum mismatch');
  const metadata = chartPackageMetadata(pkg.data);
  if (metadata.chartVersion !== chartVersion) fail('rollback chart package contents do not match requested version');
  const appVersion = versionFromAsset(appRef);
  if (appVersion !== metadata.appVersion) fail('rollback app target does not match chart package appVersion');
  const app = await verifyApp(appVersion, appRef, appSha, metadata.imageDigest);
  return { app, chart: { version: chartVersion, reference: packageRef, sha256: packageSha, package_reference: packageRef,
    package_sha256: packageSha, source_repository: `https://github.com/${chartRepo}`, source_commit: await tagCommit(chartRepo, tag),
    format: 'legacy-chart-package-v0', missing_evidence: ['chart-release-record', 'chart-source-identity'] }, reason };
}
function makeCompatibilityLock(candidate) {
  const lock = {
    schema_version: 1,
    kind: 'consumer-compatibility-lock',
    app: {
      version: candidate.app.version, reference: candidate.app.reference, sha256: candidate.app.sha256,
      source_repository: candidate.app.source_repository, source_commit: candidate.app.source_commit,
      image_repository: candidate.app.image_repository, image_manifest_list_digest: candidate.app.image_manifest_list_digest,
    },
    chart: {
      version: candidate.chart.version, reference: candidate.chart.reference, sha256: candidate.chart.sha256,
      package_reference: candidate.chart.package_reference, package_sha256: candidate.chart.package_sha256,
      source_repository: candidate.chart.source_repository, source_commit: candidate.chart.source_commit,
    },
    supported_evidence_lanes: ['app-manifest-list', 'app-platform-descriptors', 'app-provenance', 'app-signature', 'chart-package-sha256'],
  };
  return lock;
}
async function selectLatestChart() {
  const releases = await (await api(`/repos/${chartRepo}/releases?per_page=100`)).json();
  const sorted = releases.filter((rel) => !rel.draft && !rel.prerelease)
    .map((rel) => ({ rel, version: /^(?:pacman-|helm-chart-v)((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$/.exec(rel.tag_name)?.[1] }))
    .filter((item) => item.version).sort((a, b) => compareVersion(b.version, a.version));
  if (!sorted.length) fail('no stable chart release exists');
  const { rel, version: chartVersion } = sorted[0];
  const packageAsset = await verifiedAsset(rel, `pacman-${chartVersion}.tgz`);
  const recordAsset = rel.assets.some((item) => item.name === 'chart-release-record.json')
    ? await verifiedAsset(rel, 'chart-release-record.json') : null;
  if (!recordAsset) {
    const metadata = chartPackageMetadata(packageAsset.data);
    if (metadata.chartVersion !== chartVersion || !versionRe.test(metadata.appVersion) || !digestRe.test(metadata.imageDigest)) fail('legacy chart package identity is invalid');
    const appRelease = await release(appRepo, `pacman-v${metadata.appVersion}`);
    verifyVersionTag(appRelease, `pacman-v${metadata.appVersion}`);
    const appMeta = await verifiedAsset(appRelease, 'release-metadata.json');
    const app = await verifyApp(metadata.appVersion, appMeta.url, appMeta.checksum, metadata.imageDigest);
    return { app, chart: { version: chartVersion, reference: packageAsset.url, sha256: packageAsset.checksum,
      package_reference: packageAsset.url, package_sha256: packageAsset.checksum, source_repository: `https://github.com/${chartRepo}`,
      source_commit: await tagCommit(chartRepo, rel.tag_name), format: 'legacy-chart-package-v0',
      missing_evidence: ['chart-release-record', 'chart-source-identity'] } };
  }
  const record = JSON.parse(recordAsset.data.toString());
  validateChartRecord(record, recordAsset.data, rel, packageAsset.data);
  const appRelease = record.app_release;
  const app = await verifyApp(appRelease.version, appRelease.reference, appRelease.sha256);
  if (!commitRe.test(record.source.commit)) fail('chart source commit is invalid');
  return {
    app,
    chart: { version: chartVersion, reference: recordAsset.url, sha256: recordAsset.checksum, package_reference: packageAsset.url,
      package_sha256: packageAsset.checksum, source_repository: record.source.repository, source_commit: record.source.commit, format: 'chart-release-v1' },
  };
}

function readState(path) { try { return JSON.parse(readFileSync(path, 'utf8')); } catch (error) { if (error.code === 'ENOENT') return null; throw error; } }
async function main() {
  const [command, planPath, statePath] = process.argv.slice(2);
  if (command === 'filter-pending') {
    const pending = readState(planPath);
    const proposals = JSON.parse(readFileSync(statePath, 'utf8'));
    const selected = selectPendingProposalState({ state: pending, pullRequests: proposals,
      branchSha: process.env.PENDING_BRANCH_SHA, baseSha: process.env.PENDING_BASE_SHA });
    writeFileSync(process.env.PENDING_FILTERED_PATH, `${JSON.stringify(selected)}\n`);
    return;
  }
  if (command === 'plan') {
    const current = JSON.parse(readFileSync(process.env.CURRENT_STATE_PATH, 'utf8'));
    const state = readState(process.env.PENDING_STATE_PATH);
    const mode = process.env.PROMOTION_MODE || 'reconcile';
    if (rollbackRequiresExplicitResume({ currentState: current, pendingState: state, mode })) {
      writeFileSync(planPath, JSON.stringify({ action: 'noop', reason: 'rollback-proposal-pending' }, null, 2) + '\n'); return;
    }
    const rollback = mode === 'rollback' ? await rollbackCandidate() : null;
    const candidate = rollback?.app ? { app: rollback.app, chart: rollback.chart } : await selectLatestChart();
    const plan = planConsumerPromotion({ current, candidate, pending: state?.mode === 'automatic' ? state : null, mode,
      reason: rollback?.reason ?? '' });
    if (plan.action !== 'noop') plan.compatibility_lock = candidate.app.format === 'app-release-v1' && candidate.chart.format === 'chart-release-v1'
      ? makeCompatibilityLock(candidate) : null;
    writeFileSync(planPath, JSON.stringify(plan, null, 2) + '\n'); return;
  }
  if (command === 'write-state') {
    const plan = JSON.parse(readFileSync(planPath, 'utf8'));
    if (plan.action === 'noop') return;
    const state = { schema_version: 1, kind: 'pacman-k8s-promotion-state', mode: plan.action === 'rollback-proposal' ? 'rollback' : 'automatic', app: plan.app, chart: plan.chart };
    if (plan.rollback_reason) state.rollback = { target_app_reference: plan.app.reference, target_app_sha256: plan.app.sha256,
      target_chart_reference: plan.chart.reference, target_chart_sha256: plan.chart.sha256, reason: plan.rollback_reason };
    writeFileSync(statePath, JSON.stringify(state, null, 2) + '\n');
    if (plan.compatibility_lock) writeFileSync('consumer-compatibility-lock.json', JSON.stringify(plan.compatibility_lock, null, 2) + '\n');
    return;
  }
  if (command === 'update-docs') {
    const oldApp = process.env.DOC_OLD_APP_VERSION, newApp = process.env.DOC_NEW_APP_VERSION;
    const oldChart = process.env.DOC_OLD_CHART_VERSION, newChart = process.env.DOC_NEW_CHART_VERSION;
    const oldDigest = process.env.DOC_OLD_IMAGE_DIGEST, newDigest = process.env.DOC_NEW_IMAGE_DIGEST;
    for (const [path, oldValue, newValue] of [
      ['README.md', oldApp, newApp], ['README.md', oldChart, newChart], ['README.md', oldDigest, newDigest],
      ['cilium-demos/fault-demo/README.md', oldChart, newChart],
      ['cilium-demos/README.md', oldDigest, newDigest], ['cilium-demos/scripts/deploy-demo.sh', oldDigest, newDigest],
    ]) {
      if (!oldValue || !newValue || oldValue === newValue) continue;
      const before = readFileSync(path, 'utf8');
      if (!before.includes(oldValue)) fail(`expected documented pin is missing from ${path}`);
      writeFileSync(path, before.split(oldValue).join(newValue));
    }
    return;
  }
  if (command === 'verify-assets') {
    const candidate = await selectLatestChart();
    console.log(JSON.stringify(candidate)); return;
  }
  fail('usage: pacman-promotion.mjs plan <plan.json> | write-state <plan.json> <state.json> | update-docs | verify-assets');
}

if (process.argv[1]?.endsWith('/pacman-promotion.mjs')) main().catch((error) => { console.error(error.message); process.exitCode = 1; });
