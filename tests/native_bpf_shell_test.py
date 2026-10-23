import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeBpfShellTest(unittest.TestCase):
    def test_candidate_pod_verifier_runs_under_nounset(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            stubs = {
                "kubectl": """#!/usr/bin/env bash
set -euo pipefail
if [[ "$5" == --namespace && "$7" == get && "$8" == pods ]]; then
  cat <<'JSON'
{"items":[{"metadata":{"name":"pacman-web-abc","labels":{"app.kubernetes.io/component":"web"}},"status":{"phase":"Running","containerStatuses":[]}}]}
JSON
elif [[ "$5" == --namespace && "$7" == exec ]]; then
  printf '%s\\n' '{"app":"pacman","version":"0.7.16","commit":"13f04d6e6aea62f1d32d57d90e4eff786083c02e","role":"web"}'
else
  printf 'unexpected kubectl args: %s\\n' "$*" >&2
  exit 91
fi
""",
                "jq": "#!/usr/bin/env bash\nprintf '%s\\t%s\\n' pacman-web-abc web\n",
                "python3": "#!/usr/bin/env bash\nexit 0\n",
            }
            for name, contents in stubs.items():
                stub = bin_dir / name
                stub.write_text(contents, encoding="utf-8")
                stub.chmod(0o755)

            evidence_dir = temp / "evidence"
            env = os.environ.copy()
            env.update({
                "RUNNER_TEMP": str(temp),
                "EVIDENCE_DIR": str(evidence_dir),
                "NATIVE_BPF_RUN_TAG": "shell-test",
                "PATH": f"{bin_dir}:{env['PATH']}",
            })
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; verify_candidate_pods native-test cilium', "native-bpf-shell-test", str(ROOT / "tests/native-bpf.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((evidence_dir / "cilium-pods.json").is_file())
            self.assertTrue((evidence_dir / "cilium-runtime.jsonl").is_file())
            rows = (evidence_dir / "cilium-runtime.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(rows[0])["pod"], "pacman-web-abc")


if __name__ == "__main__":
    unittest.main()
