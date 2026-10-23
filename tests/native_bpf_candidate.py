#!/usr/bin/env python3
"""Resolve and verify the immutable Pac-Man candidate used by native BPF CI."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


APP_REPOSITORY = "https://github.com/saintdle/pacman"
CHART_REPOSITORY = "https://github.com/saintdle/helm-charts"
IMAGE_REPOSITORY = "docker.io/saintdle/pacman"
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
COMMIT_RE = re.compile(r"[a-f0-9]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}\Z")
SHA_RE = re.compile(r"[a-f0-9]{64}\Z")
LEGACY_APP_MISSING = {"app-release-record", "platform-descriptors", "provenance", "signature"}
LEGACY_CHART_MISSING = {"chart-release-record", "chart-source-identity"}
MAX_ASSET_BYTES = 64 * 1024 * 1024


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _obj(value: Any, name: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{name} must be an object")
    return value


def _version(value: Any, name: str) -> str:
    _require(isinstance(value, str) and VERSION_RE.fullmatch(value) is not None,
             f"{name} must be a stable MAJOR.MINOR.PATCH version")
    return value


def _commit(value: Any, name: str) -> str:
    _require(isinstance(value, str) and COMMIT_RE.fullmatch(value) is not None,
             f"{name} must be a full lowercase Git commit")
    return value


def _sha(value: Any, name: str) -> str:
    _require(isinstance(value, str) and SHA_RE.fullmatch(value) is not None,
             f"{name} must be a full lowercase SHA-256")
    return value


def _digest(value: Any, name: str) -> str:
    _require(isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None,
             f"{name} must be an immutable sha256 digest")
    return value


def _asset_url(value: Any, name: str) -> str:
    _require(isinstance(value, str), f"{name} must be an immutable HTTPS release asset")
    parsed = urllib.parse.urlparse(value)
    _require(parsed.scheme == "https" and parsed.netloc == "github.com" and not parsed.query and not parsed.fragment,
             f"{name} must be a credential-free immutable GitHub release asset")
    return value


def _expected_asset(repo: str, tag: str, filename: str) -> str:
    return f"{repo}/releases/download/{tag}/{filename}"


def _normalize_candidate(source_format: str, missing: list[str], app: dict[str, Any], chart: dict[str, Any]) -> dict[str, Any]:
    _require(app["source_repository"] == APP_REPOSITORY, "candidate app source repository is not Pac-Man")
    _require(app["image_repository"] == IMAGE_REPOSITORY, "candidate image repository is not the Pac-Man OCI repository")
    _require(chart["source_repository"] in (APP_REPOSITORY, CHART_REPOSITORY), "candidate chart source repository is unsupported")
    return {
        "format": source_format,
        "missing_evidence": sorted(set(missing)),
        "app": app,
        "chart": chart,
        "image_reference": f"{app['image_repository']}@{app['image_manifest_list_digest']}",
    }


def load_candidate(root: Path) -> dict[str, Any]:
    lock_path = root / "consumer-compatibility-lock.json"
    state_path = root / "pacman-promotion-state.json"
    if lock_path.exists():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"present consumer compatibility lock is invalid: {error}") from error
        return _load_lock(lock)
    if not state_path.exists():
        raise ValueError("candidate evidence is missing: no compatibility lock or promotion state")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"legacy promotion state is invalid: {error}") from error
    return _load_legacy_state(state)


def _load_lock(value: Any) -> dict[str, Any]:
    lock = _obj(value, "consumer compatibility lock")
    _require(lock.get("schema_version") == 1 and lock.get("kind") == "consumer-compatibility-lock",
             "unsupported consumer compatibility lock")
    app_value = _obj(lock.get("app"), "lock app")
    chart_value = _obj(lock.get("chart"), "lock chart")
    app_version = _version(app_value.get("version"), "lock app version")
    app_reference = _asset_url(app_value.get("reference"), "lock app reference")
    app_sha = _sha(app_value.get("sha256"), "lock app checksum")
    app_source = app_value.get("source_repository")
    app_commit = _commit(app_value.get("source_commit"), "lock app source commit")
    image_repository = app_value.get("image_repository")
    image_digest = _digest(app_value.get("image_manifest_list_digest"), "lock app manifest-list digest")
    _require(app_reference == _expected_asset(APP_REPOSITORY, f"pacman-v{app_version}", "app-release-record.json"),
             "lock app reference must identify the immutable versioned app release record")
    lanes = lock.get("supported_evidence_lanes")
    _require(isinstance(lanes, list) and {"app-manifest-list", "app-platform-descriptors", "chart-package-sha256"}.issubset(lanes),
             "lock does not declare manifest-list, platform-descriptor, and chart-package evidence")

    chart_version = _version(chart_value.get("version"), "lock chart version")
    chart_reference = _asset_url(chart_value.get("reference"), "lock chart record reference")
    chart_sha = _sha(chart_value.get("sha256"), "lock chart record checksum")
    package_reference = _asset_url(chart_value.get("package_reference"), "lock chart package reference")
    package_sha = _sha(chart_value.get("package_sha256"), "lock chart package checksum")
    chart_source = chart_value.get("source_repository")
    chart_commit = _commit(chart_value.get("source_commit"), "lock chart source commit")
    package_parts = urllib.parse.urlparse(package_reference).path.split("/")
    package_tag, package_filename = package_parts[-2:]
    _require(package_reference.startswith(CHART_REPOSITORY + "/releases/download/")
             and package_filename == f"pacman-{chart_version}.tgz",
             "lock chart package reference must be an immutable versioned package asset")
    _require(package_tag in (f"pacman-{chart_version}", f"helm-chart-v{chart_version}"),
             "lock package tag does not match chart version")
    _require(chart_reference == package_reference.rsplit("/", 1)[0] + "/chart-release-record.json",
             "lock chart record and package must be sibling release assets")

    return _normalize_candidate("consumer-compatibility-lock", [], {
        "version": app_version, "reference": app_reference, "sha256": app_sha,
        "source_repository": app_source, "source_commit": app_commit,
        "image_repository": image_repository, "image_manifest_list_digest": image_digest,
    }, {
        "version": chart_version, "reference": chart_reference, "sha256": chart_sha,
        "package_reference": package_reference, "package_sha256": package_sha,
        "source_repository": chart_source, "source_commit": chart_commit,
    })


def _load_legacy_state(value: Any) -> dict[str, Any]:
    state = _obj(value, "legacy promotion state")
    _require(state.get("schema_version") == 1 and state.get("kind") == "pacman-k8s-promotion-state",
             "unsupported legacy promotion state")
    app_value = _obj(state.get("app"), "legacy app identity")
    chart_value = _obj(state.get("chart"), "legacy chart identity")
    app_version = _version(app_value.get("version"), "legacy app version")
    chart_version = _version(chart_value.get("version"), "legacy chart version")
    app_reference = _asset_url(app_value.get("reference"), "legacy app reference")
    chart_reference = _asset_url(chart_value.get("reference"), "legacy chart reference")
    app_missing = app_value.get("missing_evidence")
    chart_missing = chart_value.get("missing_evidence")
    _require(app_value.get("format") == "legacy-release-metadata-v0" and isinstance(app_missing, list)
             and LEGACY_APP_MISSING.issubset(app_missing), "legacy app state must declare its format and missing evidence")
    _require(chart_value.get("format") == "legacy-chart-package-v0" and isinstance(chart_missing, list)
             and LEGACY_CHART_MISSING.issubset(chart_missing), "legacy chart state must declare its format and missing evidence")
    _require(app_reference == _expected_asset(APP_REPOSITORY, f"pacman-v{app_version}", "release-metadata.json"),
             "legacy app reference must identify the immutable release metadata asset")
    package_reference = _asset_url(chart_value.get("package_reference"), "legacy chart package reference")
    _require(chart_reference == package_reference, "legacy chart reference must be the immutable chart package")
    _require(package_reference == _expected_asset(CHART_REPOSITORY, f"pacman-{chart_version}", f"pacman-{chart_version}.tgz"),
             "legacy chart package must identify the immutable versioned package asset")
    package_sha = _sha(chart_value.get("package_sha256"), "legacy chart package checksum")
    chart_sha = _sha(chart_value.get("sha256"), "legacy chart checksum")
    _require(chart_sha == package_sha, "legacy chart reference checksum must match the package checksum")
    return _normalize_candidate("legacy-promotion-state", list(app_missing) + list(chart_missing), {
        "version": app_version, "reference": app_reference,
        "sha256": _sha(app_value.get("sha256"), "legacy app metadata checksum"),
        "source_repository": app_value.get("source_repository"),
        "source_commit": _commit(app_value.get("source_commit"), "legacy app source commit"),
        "image_repository": app_value.get("image_repository"),
        "image_manifest_list_digest": _digest(app_value.get("image_manifest_list_digest"), "legacy image manifest-list digest"),
    }, {
        "version": chart_version, "reference": chart_reference, "sha256": chart_sha,
        "package_reference": package_reference, "package_sha256": package_sha,
        "source_repository": chart_value.get("source_repository"),
        "source_commit": _commit(chart_value.get("source_commit"), "legacy chart source commit"),
    })


def validate_app_release(candidate: dict[str, Any], value: Any) -> dict[str, str]:
    app = candidate["app"]
    record = _obj(value, "app release asset")
    if candidate["format"] == "legacy-promotion-state":
        _require(record.get("version") == app["version"] and record.get("tag") == f"pacman-v{app['version']}"
                 and record.get("commit") == app["source_commit"], "legacy app metadata does not match candidate source identity")
        return {}

    _require(record.get("schema_version") == 1 and record.get("kind") == "app-release"
             and record.get("app_version") == app["version"] and record.get("tag") == f"pacman-v{app['version']}",
             "app release record does not match candidate version")
    source = _obj(record.get("source"), "app release source")
    image = _obj(record.get("image"), "app release image")
    _require(source.get("repository") == app["source_repository"] and source.get("commit") == app["source_commit"],
             "app release record source identity does not match compatibility lock")
    _require(image.get("repository") == app["image_repository"]
             and image.get("manifest_list_digest") == app["image_manifest_list_digest"],
             "app release record image identity does not match compatibility lock")
    platforms = record.get("platforms")
    _require(isinstance(platforms, list), "app release record must contain platform descriptors")
    result: dict[str, str] = {}
    for item in platforms:
        item = _obj(item, "app platform descriptor")
        platform = item.get("platform")
        _require(isinstance(platform, str) and platform in ("linux/amd64", "linux/arm64"),
                 "app release record contains an unsupported platform descriptor")
        _require(platform not in result, f"app release record repeats {platform}")
        result[platform] = _digest(item.get("digest"), f"{platform} manifest digest")
    _require(set(result) == {"linux/amd64", "linux/arm64"}, "app release record must include amd64 and arm64 descriptors")
    return result


def validate_chart_record(candidate: dict[str, Any], value: Any) -> None:
    chart = candidate["chart"]
    record = _obj(value, "chart release asset")
    _require(record.get("schema_version") == 1 and record.get("kind") == "chart-release"
             and record.get("chart_version") == chart["version"], "chart release record does not match candidate version")
    source = _obj(record.get("source"), "chart release source")
    package = _obj(record.get("package"), "chart release package")
    app_release = _obj(record.get("app_release"), "chart release app identity")
    _require(source.get("repository") == chart["source_repository"] and source.get("commit") == chart["source_commit"],
             "chart release record source identity does not match compatibility lock")
    _require(package.get("filename") == f"pacman-{chart['version']}.tgz"
             and package.get("immutable_url") == chart["package_reference"]
             and package.get("sha256") == chart["package_sha256"],
             "chart release record package identity does not match compatibility lock")
    _require(app_release.get("reference") == candidate["app"]["reference"]
             and app_release.get("sha256") == candidate["app"]["sha256"],
             "chart release record app identity does not match compatibility lock")


def _yaml_scalar(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*['\"]?([^\s#'\"]+)['\"]?\s*(?:#.*)?$", text)
    if not match:
        raise ValueError(f"chart package is missing {key}")
    return match.group(1)


def _nested_yaml_scalar(text: str, parent: str, key: str) -> str:
    lines = text.splitlines()
    parent_index = next((i for i, line in enumerate(lines) if re.fullmatch(rf"{re.escape(parent)}:\s*", line)), None)
    if parent_index is None:
        raise ValueError(f"chart values are missing {parent}")
    for line in lines[parent_index + 1:]:
        if line and not line[0].isspace():
            break
        match = re.fullmatch(rf"\s+{re.escape(key)}:\s*['\"]?([^\s#'\"]+)['\"]?\s*(?:#.*)?", line)
        if match:
            return match.group(1)
    raise ValueError(f"chart values are missing {parent}.{key}")


def verify_chart_contents(candidate: dict[str, Any], chart_yaml: str, values_yaml: str) -> None:
    chart = candidate["chart"]
    app = candidate["app"]
    _require(_yaml_scalar(chart_yaml, "name") == "pacman", "chart package is not the Pac-Man chart")
    _require(_yaml_scalar(chart_yaml, "version") == chart["version"], "chart package version does not match candidate")
    _require(_yaml_scalar(chart_yaml, "appVersion") == app["version"], "chart appVersion does not match candidate app")
    _require(_nested_yaml_scalar(values_yaml, "image", "digest") == app["image_manifest_list_digest"],
             "chart package image digest does not match candidate app image")


def resolve_platform_digests(index: Any, recorded: dict[str, str] | None = None) -> dict[str, str]:
    value = _obj(index, "OCI manifest list")
    descriptors = value.get("manifests")
    _require(isinstance(descriptors, list), "candidate image reference does not resolve to an OCI manifest list")
    result: dict[str, str] = {}
    for descriptor in descriptors:
        descriptor = _obj(descriptor, "OCI platform descriptor")
        platform = descriptor.get("platform")
        if not isinstance(platform, dict) or platform.get("os") != "linux":
            continue
        architecture = platform.get("architecture")
        if architecture not in ("amd64", "arm64"):
            continue
        name = f"linux/{architecture}"
        _require(name not in result, f"OCI manifest list repeats {name}")
        result[name] = _digest(descriptor.get("digest"), f"OCI {name} descriptor")
    _require(set(result) == {"linux/amd64", "linux/arm64"}, "OCI manifest list must link amd64 and arm64 images")
    if recorded:
        _require(result == recorded, "OCI platform descriptors do not match the immutable app release record")
    return result


def normalize_image_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = re.search(r"sha256:[a-f0-9]{64}\Z", value)
    return match.group(0) if match else value


def assert_pod_identity(pod: dict[str, Any], candidate: dict[str, Any], platform_digest: str,
                        runtime: dict[str, Any]) -> dict[str, str]:
    name = pod.get("metadata", {}).get("name", "<unnamed>")
    _require(pod.get("status", {}).get("phase") == "Running" and not pod.get("metadata", {}).get("deletionTimestamp"),
             f"{name} is not a running candidate workload")
    role = pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
    _require(isinstance(role, str) and role, f"{name} has no Pac-Man workload role")
    containers = pod.get("spec", {}).get("containers", [])
    container = next((item for item in containers if item.get("name") == "pacman"), None)
    _require(container is not None, f"{name} has no pacman container")
    _require(container.get("image") == candidate["image_reference"], f"{name} pod spec does not use the candidate image")
    statuses = pod.get("status", {}).get("containerStatuses", [])
    status = next((item for item in statuses if item.get("name") == "pacman"), None)
    _require(status is not None, f"{name} has no pacman container status")
    image_id = normalize_image_id(status.get("imageID"))
    _require(image_id == platform_digest, f"{name} runtime image ID {image_id!r} does not match amd64 OCI descriptor")
    expected = candidate["app"]
    _require(runtime.get("app") == "pacman" and runtime.get("role") == role,
             f"{name} runtime app or role does not match its workload")
    _require(runtime.get("version") == expected["version"], f"{name} runtime app version does not match candidate")
    _require(runtime.get("commit") == expected["source_commit"], f"{name} runtime source commit does not match candidate")
    return {"pod": name, "role": role, "image": container["image"], "image_id": image_id,
            "version": runtime["version"], "commit": runtime["commit"]}


def _read_url(url: str, headers: dict[str, str] | None = None) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "pacman-native-bpf-ci", **(headers or {})})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read(MAX_ASSET_BYTES + 1)
        _require(len(body) <= MAX_ASSET_BYTES, "candidate evidence asset exceeds the size limit")
        return body, {key.lower(): value for key, value in response.headers.items()}


def _check_checksum(body: bytes, expected: str, name: str) -> None:
    _require(hashlib.sha256(body).hexdigest() == expected, f"{name} checksum does not match candidate state")


def _git_tag_commit(repository: str, tag: str) -> str:
    url = repository + ".git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
            check=True, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"could not verify immutable source tag {tag}") from error
    refs = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t", 1)
        if len(fields) == 2 and COMMIT_RE.fullmatch(fields[0]):
            refs[fields[1]] = fields[0]
    commit = refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))
    _require(commit is not None, f"immutable source tag {tag} is missing")
    return commit


def _registry_token() -> str:
    query = urllib.parse.urlencode({"service": "registry.docker.io", "scope": "repository:saintdle/pacman:pull"})
    body, _ = _read_url(f"https://auth.docker.io/token?{query}")
    value = json.loads(body)
    token = value.get("token") or value.get("access_token")
    _require(isinstance(token, str) and token, "Docker registry did not provide a pull token")
    return token


def _registry_get(path: str, token: str, accept: str) -> tuple[bytes, dict[str, str]]:
    return _read_url(f"https://registry-1.docker.io/v2/saintdle/pacman/{path}", {
        "Authorization": f"Bearer {token}", "Accept": accept,
    })


def _check_registry_digest(body: bytes, headers: dict[str, str], expected: str, name: str) -> None:
    _require(headers.get("docker-content-digest") == expected, f"registry {name} digest header does not match candidate")
    _require("sha256:" + hashlib.sha256(body).hexdigest() == expected, f"registry {name} bytes do not match candidate digest")


def verify_candidate_artifacts(candidate: dict[str, Any]) -> dict[str, Any]:
    app = candidate["app"]
    chart = candidate["chart"]
    app_body, _ = _read_url(app["reference"])
    _check_checksum(app_body, app["sha256"], "app release asset")
    try:
        release = json.loads(app_body)
    except json.JSONDecodeError as error:
        raise ValueError("app release asset is not valid JSON") from error
    recorded_platforms = validate_app_release(candidate, release)
    app_tag = f"pacman-v{app['version']}"
    _require(_git_tag_commit(app["source_repository"], app_tag) == app["source_commit"],
             "app source commit does not match its immutable Git tag")

    chart_record_verified = False
    if candidate["format"] == "consumer-compatibility-lock":
        record_body, _ = _read_url(chart["reference"])
        _check_checksum(record_body, chart["sha256"], "chart release record")
        try:
            validate_chart_record(candidate, json.loads(record_body))
        except json.JSONDecodeError as error:
            raise ValueError("chart release record is not valid JSON") from error
        chart_record_verified = True

    package_body, _ = _read_url(chart["package_reference"])
    _check_checksum(package_body, chart["package_sha256"], "chart package")
    try:
        with tarfile.open(fileobj=io.BytesIO(package_body), mode="r:gz") as archive:
            chart_member = next((item for item in archive.getmembers() if item.name.endswith("/Chart.yaml") or item.name == "Chart.yaml"), None)
            values_member = next((item for item in archive.getmembers() if item.name.endswith("/values.yaml") or item.name == "values.yaml"), None)
            _require(chart_member is not None and values_member is not None, "chart package is missing Chart.yaml or values.yaml")
            chart_yaml = archive.extractfile(chart_member).read().decode("utf-8")
            values_yaml = archive.extractfile(values_member).read().decode("utf-8")
    except (OSError, tarfile.TarError, AttributeError) as error:
        raise ValueError("chart package is not a readable Helm archive") from error
    verify_chart_contents(candidate, chart_yaml, values_yaml)

    chart_release = urllib.parse.urlparse(chart["package_reference"]).path.split("/releases/download/", 1)[1].split("/", 1)[0]
    chart_tag_repo = chart["source_repository"]
    chart_tag_verified = False
    if candidate["format"] == "legacy-promotion-state" and chart_tag_repo == CHART_REPOSITORY:
        _require(_git_tag_commit(chart_tag_repo, chart_release) == chart["source_commit"],
                 "legacy chart source commit does not match its immutable Git tag")
        chart_tag_verified = True

    token = _registry_token()
    list_digest = app["image_manifest_list_digest"]
    index_body, index_headers = _registry_get(
        f"manifests/{list_digest}", token,
        "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
    )
    _check_registry_digest(index_body, index_headers, list_digest, "manifest-list")
    try:
        index = json.loads(index_body)
    except json.JSONDecodeError as error:
        raise ValueError("candidate image manifest list is not valid JSON") from error
    platform_digests = resolve_platform_digests(index, recorded_platforms or None)
    for platform_digest in platform_digests.values():
        child_body, child_headers = _registry_get(
            f"manifests/{platform_digest}", token,
            "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
        )
        _check_registry_digest(child_body, child_headers, platform_digest, "platform manifest")
    amd64_manifest = json.loads(_registry_get(
        f"manifests/{platform_digests['linux/amd64']}", token,
        "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
    )[0])
    config_digest = _digest(amd64_manifest.get("config", {}).get("digest"), "amd64 image config digest")
    config_body, _ = _registry_get(f"blobs/{config_digest}", token, "application/octet-stream")
    _require("sha256:" + hashlib.sha256(config_body).hexdigest() == config_digest,
             "amd64 image config bytes do not match the OCI descriptor")
    config = json.loads(config_body)
    _require(config.get("os") == "linux" and config.get("architecture") == "amd64",
             "candidate amd64 config does not identify linux/amd64")
    labels = config.get("config", {}).get("Labels") or {}
    image_revision = labels.get("org.opencontainers.image.revision")
    _require(image_revision == app["source_commit"], "candidate OCI source label does not match the promoted app source")

    return {
        "candidate": candidate,
        "platform_digests": platform_digests,
        "oci_source_revision": image_revision,
        "chart_package_sha256": chart["package_sha256"],
        "chart_release_record_verified": chart_record_verified,
        "app_source_tag_verified": True,
        "chart_source_tag_verified": chart_tag_verified,
        "verified": ["app-release-asset", "app-source-tag", "chart-package-sha256", "chart-metadata",
                     "oci-manifest-list", "oci-platform-descriptors", "oci-config-source-label"],
        "missing_evidence": candidate["missing_evidence"],
    }


def rewrite_app_version(manifest: Path, version: str) -> None:
    _version(version, "candidate app version")
    text = manifest.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(?m)^([ \t]*-[ \t]*name:[ \t]*APP_VERSION\n[ \t]*value:[ \t]*)[^\n]+$",
        lambda match: match.group(1) + version,
        text,
    )
    _require(count > 0, f"{manifest.name} contains no APP_VERSION field to bind to the candidate")
    manifest.write_text(updated, encoding="utf-8")


def verify_pods(identity: dict[str, Any], pods: Any, runtimes: dict[str, Any]) -> list[dict[str, str]]:
    items = _obj(pods, "Kubernetes pod list").get("items")
    _require(isinstance(items, list) and items, "native scenario has no running Pac-Man pods")
    outputs = []
    for pod in items:
        if pod.get("status", {}).get("phase") != "Running" or pod.get("metadata", {}).get("deletionTimestamp"):
            continue
        name = pod.get("metadata", {}).get("name", "<unnamed>")
        runtime = runtimes.get(name)
        _require(isinstance(runtime, dict), f"{name} has no runtime /version evidence")
        outputs.append(assert_pod_identity(
            pod, identity["candidate"], identity["platform_digests"]["linux/amd64"], runtime,
        ))
    _require(outputs, "native scenario has no running Pac-Man pods")
    return outputs


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--root", required=True)
    prepare.add_argument("--evidence-dir", required=True)
    rewrite = commands.add_parser("rewrite-version")
    rewrite.add_argument("--identity", required=True)
    rewrite.add_argument("--manifest", required=True)
    pods = commands.add_parser("verify-pods")
    pods.add_argument("--identity", required=True)
    pods.add_argument("--pods", required=True)
    pods.add_argument("--runtimes", required=True)
    pods.add_argument("--output", required=True)
    args = parser.parse_args()

    try:
        if args.command == "prepare":
            candidate = load_candidate(Path(args.root))
            identity = verify_candidate_artifacts(candidate)
            destination = Path(args.evidence_dir)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "candidate-identity.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
            print(f"PASS candidate {candidate['app']['version']} {candidate['image_reference']}")
            print(f"source {candidate['app']['source_commit']}; chart {candidate['chart']['version']} package {candidate['chart']['package_sha256']}")
            if candidate["missing_evidence"]:
                print("legacy missing evidence: " + ", ".join(candidate["missing_evidence"]))
        elif args.command == "rewrite-version":
            identity = _load_json(args.identity)
            rewrite_app_version(Path(args.manifest), identity["candidate"]["app"]["version"])
        else:
            identity = _load_json(args.identity)
            runtime_map = {}
            for line in Path(args.runtimes).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    item = json.loads(line)
                    runtime_map[item["pod"]] = item["version"]
            verified = verify_pods(identity, _load_json(args.pods), runtime_map)
            Path(args.output).write_text(json.dumps(verified, indent=2) + "\n", encoding="utf-8")
            print(f"PASS {len(verified)} running workloads use the exact candidate image, source, and runtime version")
    except (OSError, json.JSONDecodeError, ValueError, urllib.error.URLError) as error:
        print(f"FAIL native candidate identity: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
