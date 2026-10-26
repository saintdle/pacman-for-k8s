import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeBpfShellTest(unittest.TestCase):
    def test_tetragon_manifest_gates_readiness_on_http_readyz(self):
        manifest = (ROOT / "cilium-demos/tetragon-enforcement/tetragon-demo.yaml").read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^          readinessProbe:\n(.*?)(?=^          [A-Za-z][A-Za-z0-9]*:|\Z)",
            manifest,
        )
        self.assertIsNotNone(match, "Tetragon demo must declare an application readiness probe")
        probe = match.group(0)
        self.assertRegex(probe, r"(?m)^              path: /readyz$")
        self.assertRegex(probe, r"(?m)^              port: http$")
        self.assertRegex(probe, r"(?m)^            periodSeconds: 2$")
        self.assertRegex(probe, r"(?m)^            failureThreshold: 30$")

    def test_candidate_pod_verifier_captures_owned_node_cri_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            app = json.loads((ROOT / "pacman-promotion-state.json").read_text(encoding="utf-8"))["app"]
            image_reference = f"{app['image_repository']}@{app['image_manifest_list_digest']}"
            config_digest = "sha256:" + "c" * 64
            cri_file = temp / "cri.json"
            cri_file.write_text(json.dumps({"status": {"id": config_digest, "repoDigests": [image_reference]}}), encoding="utf-8")
            node = "native-test-control-plane"

            kubectl = bin_dir / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"$*\" == *'get pods'* ]]; then\n"
                "  cat <<'JSON'\n"
                + json.dumps({"items": [{
                    "metadata": {"name": "pacman-web-abc", "labels": {"app.kubernetes.io/component": "web"}},
                    "spec": {"nodeName": node},
                    "status": {"phase": "Running", "containerStatuses": []},
                }]})
                + "\nJSON\nelif [[ \"$*\" == *'get node'* ]]; then\n"
                "  printf '%s\\n' '{\"status\":{\"nodeInfo\":{\"containerRuntimeVersion\":\"containerd://2.0.3\"}}}'\n"
                "elif [[ \"$*\" == *'get pod '* ]]; then\n"
                "  if [[ \"${NATIVE_BPF_FAIL_DIAGNOSTICS:-0}\" == 1 ]]; then printf '%s\\n' 'synthetic pod-state failure' >&2; exit 89; fi\n"
                "  if [[ \"${NATIVE_BPF_HANG_POD_DIAGNOSTIC:-0}\" == 1 ]]; then exec sleep 5; fi\n"
                "  cat <<'JSON'\n"
                "{\"metadata\":{\"name\":\"pacman-web-abc\",\"namespace\":\"native-test\"},\"spec\":{\"containers\":[{\"name\":\"pacman\",\"env\":[{\"name\":\"DEMO_TOKEN\",\"value\":\"diagnostic-secret-marker\"}]}]},\"status\":{\"phase\":\"Running\",\"conditions\":[{\"type\":\"Ready\",\"status\":\"False\",\"reason\":\"ContainersNotReady\"}],\"containerStatuses\":[{\"name\":\"pacman\",\"ready\":false,\"restartCount\":0,\"state\":{\"waiting\":{\"reason\":\"Running\"}},\"lastState\":{}}]}}\n"
                "JSON\n"
                "elif [[ \"$*\" == *'logs'* ]]; then\n"
                "  if [[ \"${NATIVE_BPF_FAIL_DIAGNOSTICS:-0}\" == 1 ]]; then printf '%s\\n' 'synthetic log failure' >&2; exit 88; fi\n"
                "  printf '%s\\n' 'pacman startup diagnostic marker'\n"
                "elif [[ \"$*\" == *'exec'* ]]; then\n"
                "  if [[ \"${NATIVE_BPF_TEST_FETCH_FAILURE:-0}\" == 1 ]]; then\n"
                "    [[ -z \"${NATIVE_BPF_EXEC_MARKER:-}\" ]] || printf x >>\"$NATIVE_BPF_EXEC_MARKER\"\n"
                "    while [[ $# -gt 0 && \"$1\" != -- ]]; do shift; done\n"
                "    [[ $# -gt 0 ]] && shift\n"
                "    exec \"$@\"\n"
                "  fi\n"
                "  printf '%s\\n' '{\"app\":\"pacman\",\"version\":\"0.7.16\",\"commit\":\"13f04d6e6aea62f1d32d57d90e4eff786083c02e\",\"role\":\"web\"}'\n"
                "else\n  printf 'unexpected kubectl args: %s\\n' \"$*\" >&2; exit 91\nfi\n",
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
            (bin_dir / "kind").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$*\" == *'delete cluster'* ]]; then\n"
                "  [[ -z \"${NATIVE_BPF_KIND_DELETE_MARKER:-}\" ]] || printf x >>\"$NATIVE_BPF_KIND_DELETE_MARKER\"\n"
                "  exit 0\n"
                "fi\n"
                f"printf '%s\\n' {node}\n",
                encoding="utf-8",
            )
            (bin_dir / "kind").chmod(0o755)
            (bin_dir / "docker").write_text(
                "#!/usr/bin/env bash\n[[ \"$1\" == exec && \"$2\" == \"$NATIVE_BPF_NODE_NAME\" && \"$3\" == crictl && \"$4\" == inspecti ]]\ncat \"$NATIVE_BPF_CRI_JSON\"\n",
                encoding="utf-8",
            )
            (bin_dir / "docker").chmod(0o755)
            (bin_dir / "python3").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            (bin_dir / "python3").chmod(0o755)
            (bin_dir / "jq").symlink_to(shutil.which("jq"))

            evidence_dir = temp / "evidence"
            evidence_dir.mkdir()
            (evidence_dir / "candidate-identity.json").write_text(json.dumps({
                "candidate": {"image_reference": image_reference},
            }), encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "RUNNER_TEMP": str(temp),
                "EVIDENCE_DIR": str(evidence_dir),
                "NATIVE_BPF_RUN_TAG": "shell-test",
                "NATIVE_BPF_NODE_NAME": node,
                "NATIVE_BPF_CRI_JSON": str(cri_file),
                "PATH": f"{bin_dir}:{env['PATH']}",
            })
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; CLUSTER_NAME=native-test; KIND_CONTEXT=kind-native-test; verify_candidate_pods native-test cilium', "native-bpf-shell-test", str(ROOT / "tests/native-bpf.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((evidence_dir / "cilium-pods.json").is_file())
            rows = (evidence_dir / "cilium-runtime.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(rows[0])["pod"], "pacman-web-abc")
            witness = json.loads(rows[0])["cri"]
            self.assertEqual(witness["node"], node)
            self.assertEqual(witness["container_runtime_version"], "containerd://2.0.3")
            self.assertEqual(witness["inspect"]["status"]["id"], config_digest)
            self.assertEqual(witness["inspect"]["status"]["repoDigests"], [image_reference])

            node = shutil.which("node")
            self.assertIsNotNone(node, "Node.js is required for the production-probe failure regression")
            fetch_stub = temp / "refused-fetch.cjs"
            fetch_stub.write_text(
                "globalThis.fetch = async () => {\n"
                "  const cause = Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:8080'), {\n"
                "    code: 'ECONNREFUSED', address: '127.0.0.1', port: 8080,\n"
                "  });\n"
                "  throw Object.assign(new TypeError('fetch failed'), { cause });\n"
                "};\n",
                encoding="utf-8",
            )
            exec_marker = temp / "probe-execs.txt"
            failed_env = env.copy()
            failed_env.update({
                "NATIVE_BPF_TEST_FETCH_FAILURE": "1",
                "NATIVE_BPF_EXEC_MARKER": str(exec_marker),
                "NODE_OPTIONS": f"--require={fetch_stub}",
            })
            failed = subprocess.run(
                ["bash", "-c", 'source "$1"; CLUSTER_NAME=native-test; KIND_CONTEXT=kind-native-test; verify_candidate_pods native-test tetragon', "native-bpf-shell-test", str(ROOT / "tests/native-bpf.sh")],
                env=failed_env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            self.assertEqual(exec_marker.read_text(encoding="utf-8"), "x")
            probe_error = evidence_dir / "tetragon-pacman-web-abc-version-probe-error.txt"
            self.assertTrue(probe_error.is_file(), "failed version probe must retain bounded error evidence")
            error_text = probe_error.read_text(encoding="utf-8")
            self.assertIn('"code":"ECONNREFUSED"', error_text)
            self.assertIn('"address":"127.0.0.1"', error_text)
            self.assertIn('"port":8080', error_text)
            self.assertIn("probe exit status: 1", error_text)
            self.assertLessEqual(len(error_text.encode("utf-8")), 4200)
            app_logs = evidence_dir / "tetragon-pacman-web-abc-app-logs.txt"
            app_log_text = app_logs.read_text(encoding="utf-8")
            self.assertIn("pacman startup diagnostic marker", app_log_text)
            self.assertLessEqual(len(app_log_text.encode("utf-8")), 16384)
            pod_state = evidence_dir / "tetragon-pacman-web-abc-pod-state.json"
            pod_state_text = pod_state.read_text(encoding="utf-8")
            self.assertIn('"phase":"Running"', pod_state_text)
            self.assertNotIn("diagnostic-secret-marker", pod_state_text)

            diagnostic_failure_env = failed_env.copy()
            diagnostic_failure_env["NATIVE_BPF_FAIL_DIAGNOSTICS"] = "1"
            cleanup_marker = temp / "cluster-cleanup.txt"
            diagnostic_failure_env["NATIVE_BPF_KIND_DELETE_MARKER"] = str(cleanup_marker)
            diagnostic_failure = subprocess.run(
                ["bash", "-c", 'source "$1"; trap cleanup EXIT; touch "$CLUSTER_ATTEMPT_MARKER"; CLUSTER_NAME=native-test; KIND_CONTEXT=kind-native-test; verify_candidate_pods native-test tetragon', "native-bpf-shell-test", str(ROOT / "tests/native-bpf.sh")],
                env=diagnostic_failure_env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(diagnostic_failure.returncode, 1, diagnostic_failure.stdout + diagnostic_failure.stderr)
            self.assertEqual(exec_marker.read_text(encoding="utf-8"), "xx")
            self.assertIn("probe exit status: 1", probe_error.read_text(encoding="utf-8"))
            self.assertEqual(pod_state.read_text(encoding="utf-8").strip(), '{"available":false}')
            self.assertEqual(cleanup_marker.read_text(encoding="utf-8"), "x")

            hanging_snapshot_env = failed_env.copy()
            hanging_snapshot_env.update({
                "NATIVE_BPF_DIAGNOSTIC_TIMEOUT_SECONDS": "1",
                "NATIVE_BPF_HANG_POD_DIAGNOSTIC": "1",
                "NATIVE_BPF_KIND_DELETE_MARKER": str(cleanup_marker),
            })
            hanging_snapshot = subprocess.run(
                ["bash", "-c", 'source "$1"; trap cleanup EXIT; touch "$CLUSTER_ATTEMPT_MARKER"; CLUSTER_NAME=native-test; KIND_CONTEXT=kind-native-test; verify_candidate_pods native-test tetragon', "native-bpf-shell-test", str(ROOT / "tests/native-bpf.sh")],
                env=hanging_snapshot_env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(hanging_snapshot.returncode, 1, hanging_snapshot.stdout + hanging_snapshot.stderr)
            self.assertEqual(exec_marker.read_text(encoding="utf-8"), "xxx")
            self.assertEqual(cleanup_marker.read_text(encoding="utf-8"), "xx")
            self.assertEqual(pod_state.read_text(encoding="utf-8").strip(), '{"available":false}')


if __name__ == "__main__":
    unittest.main()
