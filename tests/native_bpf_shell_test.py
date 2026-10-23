import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeBpfShellTest(unittest.TestCase):
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
                "elif [[ \"$*\" == *'exec'* ]]; then\n"
                "  printf '%s\\n' '{\"app\":\"pacman\",\"version\":\"0.7.16\",\"commit\":\"13f04d6e6aea62f1d32d57d90e4eff786083c02e\",\"role\":\"web\"}'\n"
                "else\n  printf 'unexpected kubectl args: %s\\n' \"$*\" >&2; exit 91\nfi\n",
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
            (bin_dir / "kind").write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {node}\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
