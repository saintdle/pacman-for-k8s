import copy
import json
import tempfile
import unittest
from pathlib import Path

from native_bpf_candidate import (
    assert_pod_identity,
    load_candidate,
    resolve_platform_digests,
    validate_app_release,
    validate_chart_record,
    verify_chart_contents,
)


ROOT = Path(__file__).resolve().parents[1]
LEGACY_PROMOTION_STATE = {
    "schema_version": 1,
    "kind": "pacman-k8s-promotion-state",
    "mode": "automatic",
    "app": {
        "version": "0.7.16",
        "reference": "https://github.com/saintdle/pacman/releases/download/pacman-v0.7.16/release-metadata.json",
        "sha256": "ef15decce2fc0cb9aa0b14d4a2d059e10eb13638afc57fe99277b8688aea4c4e",
        "source_commit": "13f04d6e6aea62f1d32d57d90e4eff786083c02e",
        "source_repository": "https://github.com/saintdle/pacman",
        "image_repository": "docker.io/saintdle/pacman",
        "image_manifest_list_digest": "sha256:592740129bce7530a347146b86647f8b516df3c19105aa4c2fe40b878840fccf",
        "format": "legacy-release-metadata-v0",
        "missing_evidence": ["app-release-record", "platform-descriptors", "provenance", "signature"],
    },
    "chart": {
        "version": "0.7.17",
        "reference": "https://github.com/saintdle/helm-charts/releases/download/pacman-0.7.17/pacman-0.7.17.tgz",
        "sha256": "b0722ab17320afe14d5bee5ebf25ed26c9dcfa59f24e238764c4b2d8a76eb971",
        "package_reference": "https://github.com/saintdle/helm-charts/releases/download/pacman-0.7.17/pacman-0.7.17.tgz",
        "package_sha256": "b0722ab17320afe14d5bee5ebf25ed26c9dcfa59f24e238764c4b2d8a76eb971",
        "source_repository": "https://github.com/saintdle/helm-charts",
        "source_commit": "1d11684428b2069aef8d4e69c37237b5b3c910a5",
        "format": "legacy-chart-package-v0",
        "missing_evidence": ["chart-release-record", "chart-source-identity"],
    },
}


def write_legacy_state(root: Path) -> None:
    (root / "pacman-promotion-state.json").write_text(json.dumps(LEGACY_PROMOTION_STATE), encoding="utf-8")


def valid_lock():
    state = LEGACY_PROMOTION_STATE
    app = state["app"]
    chart = state["chart"]
    return {
        "schema_version": 1,
        "kind": "consumer-compatibility-lock",
        "app": {
            "version": app["version"],
            "reference": app["reference"].replace("release-metadata.json", "app-release-record.json"),
            "sha256": "a" * 64,
            "source_repository": app["source_repository"],
            "source_commit": app["source_commit"],
            "image_repository": app["image_repository"],
            "image_manifest_list_digest": app["image_manifest_list_digest"],
        },
        "chart": {
            "version": chart["version"],
            "reference": chart["package_reference"].rsplit("/", 1)[0] + "/chart-release-record.json",
            "sha256": "b" * 64,
            "package_reference": chart["package_reference"],
            "package_sha256": chart["package_sha256"],
            "source_repository": "https://github.com/saintdle/pacman",
            "source_commit": app["source_commit"],
        },
        "supported_evidence_lanes": [
            "app-manifest-list", "app-platform-descriptors", "app-provenance",
            "app-signature", "chart-package-sha256",
        ],
    }


class CandidateResolutionTest(unittest.TestCase):
    def test_legacy_state_is_explicit_and_keeps_missing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_legacy_state(root)
            candidate = load_candidate(root)
        self.assertEqual(candidate["format"], "legacy-promotion-state")
        self.assertIn("app-release-record", candidate["missing_evidence"])
        self.assertIn("chart-release-record", candidate["missing_evidence"])
        self.assertEqual(candidate["app"]["version"], "0.7.16")
        self.assertTrue(candidate["image_reference"].endswith(candidate["app"]["image_manifest_list_digest"]))

    def test_present_invalid_lock_fails_closed_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_legacy_state(root)
            (root / "consumer-compatibility-lock.json").write_text("{invalid")
            with self.assertRaisesRegex(ValueError, "present consumer compatibility lock"):
                load_candidate(root)

    def test_lock_requires_immutable_app_and_package_references(self):
        lock = valid_lock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "consumer-compatibility-lock.json").write_text(json.dumps(lock))
            candidate = load_candidate(root)
            self.assertEqual(candidate["format"], "consumer-compatibility-lock")
            self.assertEqual(candidate["missing_evidence"], [])

            lock["app"]["reference"] = "https://github.com/saintdle/pacman/releases/latest/release-metadata.json"
            (root / "consumer-compatibility-lock.json").write_text(json.dumps(lock))
            with self.assertRaisesRegex(ValueError, "immutable GitHub release asset"):
                load_candidate(root)

    def test_lock_rejects_noncanonical_lanes_and_unknown_contract_fields(self):
        mutations = (
            (lambda lock: lock["supported_evidence_lanes"].append("unvalidated-enforcement"), "unsupported evidence lane"),
            (lambda lock: lock["supported_evidence_lanes"].append("app-manifest-list"), "duplicate evidence lane"),
            (lambda lock: lock.update(unknown_contract_field=True), "consumer compatibility lock must contain exactly"),
            (lambda lock: lock["app"].update(unknown_contract_field=True), "lock app identity must contain exactly"),
            (lambda lock: lock["chart"].update(unknown_contract_field=True), "lock chart identity must contain exactly"),
        )
        for mutate, message in mutations:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lock = valid_lock()
                mutate(lock)
                (root / "consumer-compatibility-lock.json").write_text(json.dumps(lock))
                with self.assertRaisesRegex(ValueError, message):
                    load_candidate(root)

    def test_legacy_mutable_image_and_incomplete_missing_evidence_fail(self):
        for mutation, message in (
            (lambda item: item["app"].update(image_manifest_list_digest="latest"), "manifest-list digest"),
            (lambda item: item["app"].update(missing_evidence=["app-release-record"]), "missing evidence"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                state = copy.deepcopy(LEGACY_PROMOTION_STATE)
                mutation(state)
                root = Path(directory)
                (root / "pacman-promotion-state.json").write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, message):
                    load_candidate(root)

    def test_tracked_candidate_resolves_its_current_state_and_optional_lock(self):
        state = json.loads((ROOT / "pacman-promotion-state.json").read_text(encoding="utf-8"))
        candidate = load_candidate(ROOT)
        self.assertEqual(candidate["app"]["version"], state["app"]["version"])
        self.assertEqual(candidate["chart"]["version"], state["chart"]["version"])
        if (ROOT / "consumer-compatibility-lock.json").exists():
            self.assertEqual(candidate["format"], "consumer-compatibility-lock")
            self.assertEqual(candidate["missing_evidence"], [])
        else:
            self.assertEqual(candidate["format"], "legacy-promotion-state")
            self.assertIn("app-release-record", candidate["missing_evidence"])


class CandidateArtifactTest(unittest.TestCase):
    def test_compatibility_record_binds_source_and_both_platform_descriptors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "consumer-compatibility-lock.json").write_text(json.dumps(valid_lock()))
            candidate = load_candidate(root)
        app = candidate["app"]
        record = {
            "schema_version": 1,
            "kind": "app-release",
            "app_version": app["version"],
            "tag": f"pacman-v{app['version']}",
            "source": {"repository": app["source_repository"], "commit": app["source_commit"]},
            "image": {"repository": app["image_repository"], "manifest_list_digest": app["image_manifest_list_digest"]},
            "platforms": [
                {"platform": "linux/amd64", "digest": "sha256:" + "c" * 64},
                {"platform": "linux/arm64", "digest": "sha256:" + "d" * 64},
            ],
            "provenance_reference": "https://github.com/saintdle/pacman/actions/runs/123456",
            "signature_reference": f"docker://{app['image_repository']}@{app['image_manifest_list_digest']}",
        }
        recorded = validate_app_release(candidate, record)
        self.assertEqual(set(recorded), {"linux/amd64", "linux/arm64"})
        index = {"manifests": [
            {"digest": recorded["linux/amd64"], "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": recorded["linux/arm64"], "platform": {"os": "linux", "architecture": "arm64"}},
        ]}
        self.assertEqual(resolve_platform_digests(index, recorded), recorded)
        index["manifests"][0]["digest"] = "sha256:" + "e" * 64
        with self.assertRaisesRegex(ValueError, "do not match"):
            resolve_platform_digests(index, recorded)
        record["image"]["unknown_contract_field"] = True
        with self.assertRaisesRegex(ValueError, "app image must contain exactly"):
            validate_app_release(candidate, record)

    def test_chart_release_record_uses_exact_nested_contract(self):
        candidate = load_candidate(ROOT)
        chart = candidate["chart"]
        record = {
            "schema_version": 1,
            "kind": "chart-release",
            "chart_version": chart["version"],
            "source": {"repository": chart["source_repository"], "commit": chart["source_commit"]},
            "package": {
                "filename": f"pacman-{chart['version']}.tgz",
                "sha256": chart["package_sha256"],
                "immutable_url": chart["package_reference"],
            },
            "app_release": {
                "reference": candidate["app"]["reference"],
                "sha256": candidate["app"]["sha256"],
            },
        }
        validate_chart_record(candidate, record)
        record["package"]["unknown_contract_field"] = True
        with self.assertRaisesRegex(ValueError, "chart package must contain exactly"):
            validate_chart_record(candidate, record)

    def test_chart_package_metadata_binds_chart_app_and_image(self):
        candidate = load_candidate(ROOT)
        verify_chart_contents(
            candidate,
            f"name: pacman\nversion: {candidate['chart']['version']}\nappVersion: \"{candidate['app']['version']}\"\n",
            f"image:\n  repository: {candidate['app']['image_repository']}\n"
            f"  digest: {candidate['app']['image_manifest_list_digest']}\n",
        )
        with self.assertRaisesRegex(ValueError, "appVersion"):
            wrong_app_version = "9.9.9" if candidate["app"]["version"] != "9.9.9" else "9.9.8"
            verify_chart_contents(
                candidate,
                f"name: pacman\nversion: {candidate['chart']['version']}\nappVersion: {wrong_app_version}\n",
                "image:\n  digest: x\n",
            )

    def test_pod_accepts_captured_list_id_only_with_verified_cri_mapping(self):
        candidate = load_candidate(ROOT)
        manifest_list = candidate["app"]["image_manifest_list_digest"]
        platform = "sha256:" + "c" * 64
        config = "sha256:" + "e" * 64
        identity = {
            "candidate": candidate,
            "platform_digests": {"linux/amd64": platform, "linux/arm64": "sha256:" + "d" * 64},
            "amd64_config_digest": config,
            "oci_source_revision": candidate["app"]["source_commit"],
        }
        pod = {
            "metadata": {"name": "pacman-web-abc", "labels": {"app.kubernetes.io/component": "web"}},
            "spec": {"nodeName": "native-control-plane", "containers": [{"name": "pacman", "image": candidate["image_reference"]}]},
            "status": {"phase": "Running", "containerStatuses": [{
                "name": "pacman", "imageID": f"{candidate['app']['image_repository']}@{manifest_list}"
            }]},
        }
        runtime = {"app": "pacman", "role": "web", "version": candidate["app"]["version"], "commit": candidate["app"]["source_commit"]}
        cri = {
            "node": "native-control-plane",
            "container_runtime_version": "containerd://2.0.3",
            "image_reference": candidate["image_reference"],
            "inspect": {"status": {"id": config, "repoDigests": [f"{candidate['app']['image_repository']}@{platform}"]}},
        }
        verified = assert_pod_identity(pod, identity, runtime, cri)
        self.assertEqual(verified["image_id_digest"], manifest_list)
        self.assertEqual(verified["amd64_manifest_digest"], platform)
        self.assertEqual(verified["amd64_config_digest"], config)
        self.assertEqual(verified["cri_image_id"], config)
        self.assertEqual(verified["node"], "native-control-plane")
        self.assertEqual(verified["container_runtime_version"], "containerd://2.0.3")

    def test_pod_accepts_platform_id_only_with_verified_cri_mapping(self):
        candidate = load_candidate(ROOT)
        manifest_list = candidate["app"]["image_manifest_list_digest"]
        platform = "sha256:" + "c" * 64
        config = "sha256:" + "e" * 64
        identity = {
            "candidate": candidate,
            "platform_digests": {"linux/amd64": platform, "linux/arm64": "sha256:" + "d" * 64},
            "amd64_config_digest": config,
            "oci_source_revision": candidate["app"]["source_commit"],
        }
        pod = {
            "metadata": {"name": "pacman-web-abc", "labels": {"app.kubernetes.io/component": "web"}},
            "spec": {"nodeName": "native-control-plane", "containers": [{"name": "pacman", "image": candidate["image_reference"]}]},
            "status": {"phase": "Running", "containerStatuses": [{
                "name": "pacman", "imageID": f"docker-pullable://{candidate['app']['image_repository']}@{platform}"
            }]},
        }
        runtime = {"app": "pacman", "role": "web", "version": candidate["app"]["version"], "commit": candidate["app"]["source_commit"]}
        cri = {
            "node": "native-control-plane",
            "container_runtime_version": "containerd://2.0.3",
            "image_reference": candidate["image_reference"],
            "inspect": {"status": {"id": config, "repoDigests": [f"{candidate['app']['image_repository']}@{manifest_list}"]}},
        }
        verified = assert_pod_identity(pod, identity, runtime, cri)
        self.assertEqual(verified["image_id_digest"], platform)

    def test_pod_rejects_unlinked_image_and_wrong_cri_config_or_repository(self):
        candidate = load_candidate(ROOT)
        manifest_list = candidate["app"]["image_manifest_list_digest"]
        platform = "sha256:" + "c" * 64
        config = "sha256:" + "e" * 64
        identity = {
            "candidate": candidate,
            "platform_digests": {"linux/amd64": platform, "linux/arm64": "sha256:" + "d" * 64},
            "amd64_config_digest": config,
            "oci_source_revision": candidate["app"]["source_commit"],
        }
        pod = {
            "metadata": {"name": "pacman-web-abc", "labels": {"app.kubernetes.io/component": "web"}},
            "spec": {"nodeName": "native-control-plane", "containers": [{"name": "pacman", "image": candidate["image_reference"]}]},
            "status": {"phase": "Running", "containerStatuses": [{"name": "pacman", "imageID": f"{candidate['app']['image_repository']}@{manifest_list}"}]},
        }
        runtime = {"app": "pacman", "role": "web", "version": candidate["app"]["version"], "commit": candidate["app"]["source_commit"]}
        cri = {
            "node": "native-control-plane",
            "container_runtime_version": "containerd://2.0.3",
            "image_reference": candidate["image_reference"],
            "inspect": {"status": {"id": config, "repoDigests": [f"{candidate['app']['image_repository']}@{manifest_list}"]}},
        }
        cases = (
            (lambda item, witness: item["status"]["containerStatuses"][0].update(imageID=f"{candidate['app']['image_repository']}@sha256:{'f' * 64}"), cri, "not linked"),
            (lambda item, witness: item["status"]["containerStatuses"][0].update(imageID=f"docker.io/wrong/pacman@{manifest_list}"), cri, "repository"),
            (lambda item, witness: witness["inspect"]["status"].update(id="sha256:" + "a" * 64), cri, "config digest"),
            (lambda item, witness: witness["inspect"]["status"].update(repoDigests=[f"docker.io/wrong/pacman@{manifest_list}"]), cri, "repository"),
            (lambda item, witness: item["spec"]["containers"][0].update(image="docker.io/saintdle/pacman:latest"), cri, "pod spec"),
        )
        for mutate, witness, message in cases:
            with self.subTest(message=message):
                current_pod = json.loads(json.dumps(pod))
                current_witness = json.loads(json.dumps(witness))
                mutate(current_pod, current_witness)
                with self.assertRaisesRegex(ValueError, message):
                    assert_pod_identity(current_pod, identity, runtime, current_witness)

    def test_pod_rejects_wrong_runtime_source_and_version(self):
        candidate = load_candidate(ROOT)
        manifest_list = candidate["app"]["image_manifest_list_digest"]
        identity = {
            "candidate": candidate,
            "platform_digests": {"linux/amd64": "sha256:" + "c" * 64, "linux/arm64": "sha256:" + "d" * 64},
            "amd64_config_digest": "sha256:" + "e" * 64,
            "oci_source_revision": candidate["app"]["source_commit"],
        }
        pod = {
            "metadata": {"name": "pacman-web-abc", "labels": {"app.kubernetes.io/component": "web"}},
            "spec": {"nodeName": "native-control-plane", "containers": [{"name": "pacman", "image": candidate["image_reference"]}]},
            "status": {"phase": "Running", "containerStatuses": [{"name": "pacman", "imageID": f"{candidate['app']['image_repository']}@{manifest_list}"}]},
        }
        runtime = {"app": "pacman", "role": "web", "version": candidate["app"]["version"], "commit": candidate["app"]["source_commit"]}
        cri = {
            "node": "native-control-plane", "container_runtime_version": "containerd://2.0.3",
            "image_reference": candidate["image_reference"],
            "inspect": {"status": {"id": identity["amd64_config_digest"], "repoDigests": [f"{candidate['app']['image_repository']}@{manifest_list}"]}},
        }
        for mutation, message in (
            (lambda value: value.update(commit="0" * 40), "source commit"),
            (lambda value: value.update(version="9.9.9"), "app version"),
        ):
            with self.subTest(message=message):
                current_runtime = dict(runtime)
                mutation(current_runtime)
                with self.assertRaisesRegex(ValueError, message):
                    assert_pod_identity(pod, identity, current_runtime, cri)
        wrong_oci_source = dict(identity)
        wrong_oci_source["oci_source_revision"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "verified OCI source revision"):
            assert_pod_identity(pod, wrong_oci_source, runtime, cri)


if __name__ == "__main__":
    unittest.main()
