import json
import tempfile
import unittest
from pathlib import Path

from native_bpf_candidate import (
    assert_pod_identity,
    load_candidate,
    resolve_platform_digests,
    validate_app_release,
    verify_chart_contents,
)


ROOT = Path(__file__).resolve().parents[1]


def valid_lock():
    state = json.loads((ROOT / "pacman-promotion-state.json").read_text(encoding="utf-8"))
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
        candidate = load_candidate(ROOT)
        self.assertEqual(candidate["format"], "legacy-promotion-state")
        self.assertIn("app-release-record", candidate["missing_evidence"])
        self.assertIn("chart-release-record", candidate["missing_evidence"])
        self.assertEqual(candidate["app"]["version"], "0.7.16")
        self.assertTrue(candidate["image_reference"].endswith(candidate["app"]["image_manifest_list_digest"]))

    def test_present_invalid_lock_fails_closed_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pacman-promotion-state.json").write_text((ROOT / "pacman-promotion-state.json").read_text())
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
            with self.assertRaisesRegex(ValueError, "immutable versioned app release record"):
                load_candidate(root)

    def test_legacy_mutable_image_and_incomplete_missing_evidence_fail(self):
        state = json.loads((ROOT / "pacman-promotion-state.json").read_text(encoding="utf-8"))
        for mutation, message in (
            (lambda item: item["app"].update(image_manifest_list_digest="latest"), "manifest-list digest"),
            (lambda item: item["app"].update(missing_evidence=["app-release-record"]), "missing evidence"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                mutation(state)
                root = Path(directory)
                (root / "pacman-promotion-state.json").write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, message):
                    load_candidate(root)
                state = json.loads((ROOT / "pacman-promotion-state.json").read_text(encoding="utf-8"))


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

    def test_chart_package_metadata_binds_chart_app_and_image(self):
        candidate = load_candidate(ROOT)
        verify_chart_contents(
            candidate,
            "name: pacman\nversion: 0.7.17\nappVersion: \"0.7.16\"\n",
            "image:\n  repository: docker.io/saintdle/pacman\n  digest: "
            + candidate["app"]["image_manifest_list_digest"] + "\n",
        )
        with self.assertRaisesRegex(ValueError, "appVersion"):
            verify_chart_contents(candidate, "name: pacman\nversion: 0.7.17\nappVersion: 0.7.15\n", "image:\n  digest: x\n")

    def test_pod_requires_candidate_spec_platform_image_id_and_runtime_source(self):
        candidate = load_candidate(ROOT)
        platform = "sha256:" + "c" * 64
        pod = {
            "metadata": {"name": "pacman-web-abc", "labels": {"app.kubernetes.io/component": "web"}},
            "spec": {"containers": [{"name": "pacman", "image": candidate["image_reference"]}]},
            "status": {"phase": "Running", "containerStatuses": [{"name": "pacman", "imageID": f"containerd://{platform}"}]},
        }
        runtime = {"app": "pacman", "role": "web", "version": candidate["app"]["version"], "commit": candidate["app"]["source_commit"]}
        self.assertEqual(assert_pod_identity(pod, candidate, platform, runtime)["image_id"], platform)
        pod["spec"]["containers"][0]["image"] = "docker.io/saintdle/pacman:latest"
        with self.assertRaisesRegex(ValueError, "pod spec"):
            assert_pod_identity(pod, candidate, platform, runtime)


if __name__ == "__main__":
    unittest.main()
