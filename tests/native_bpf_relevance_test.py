import unittest

from native_bpf_relevance import is_native_relevant


class NativeBpfRelevanceTest(unittest.TestCase):
    def test_irrelevant_pull_request_has_explicit_noop_selection(self):
        selected, reason = is_native_relevant("pull_request", ["README.md"])
        self.assertFalse(selected)
        self.assertIn("do not affect", reason)

    def test_manual_and_nightly_runs_keep_full_coverage(self):
        for event in ("workflow_dispatch", "schedule"):
            with self.subTest(event=event):
                self.assertTrue(is_native_relevant(event, ["README.md"])[0])

    def test_promotion_policy_native_workflow_and_helper_changes_select_runtime(self):
        for path in (
            "consumer-compatibility-lock.json",
            "pacman-promotion-state.json",
            "tools/pacman-promotion.mjs",
            "cilium-demos/microservices-east-west/cilium-network-policy.yaml",
            "cilium-demos/tetragon-enforcement/tracingpolicy.yaml",
            "tests/native-bpf.sh",
            "tests/native_bpf_candidate.py",
            ".github/workflows/native-bpf.yml",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_native_relevant("pull_request", [path])[0])

    def test_image_identity_line_is_relevant_in_any_manifest(self):
        selected, _ = is_native_relevant(
            "pull_request",
            ["new-example/role.yaml"],
            "-          image: docker.io/saintdle/pacman:old\n"
            "+          image: docker.io/saintdle/pacman@sha256:" + "a" * 64,
        )
        self.assertTrue(selected)

    def test_unrelated_image_and_documentation_changes_are_noop(self):
        selected, _ = is_native_relevant(
            "pull_request",
            ["README.md", "examples/other.yaml"],
            "-          image: docker.io/library/nginx:1.26\n"
            "+          image: docker.io/library/nginx:1.27",
        )
        self.assertFalse(selected)


if __name__ == "__main__":
    unittest.main()
