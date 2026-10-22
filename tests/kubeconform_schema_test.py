"""Run the advisory workflow command against real Kubernetes/CRD schemas."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/lint.yml"


def advisory_command():
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: Validate Cilium CRDs (advisory)")
    run = lines.index("        run: |", start)
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and not line.startswith("          "):
            break
        body.append(line)
    command = textwrap.dedent("\n".join(body))
    if not command.strip():
        raise AssertionError("advisory workflow command is empty")
    return command


class KubeconformSchemaTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("kubeconform"), "kubeconform must be installed")
        self.temp = tempfile.TemporaryDirectory(prefix="cilium-schema-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        demo = self.root / "cilium-demos/regression"
        (demo / "values").mkdir(parents=True)
        self.manifest = demo / "policy.yaml"
        (demo / "values/settings.yaml").write_text("replicaCount: 1\n")
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)

    def validate(self, manifest):
        self.manifest.write_text(manifest)
        subprocess.run(["git", "add", "--force", "."], cwd=self.root, check=True)
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", advisory_command()],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_valid_core_and_cilium_resources_are_validated_not_skipped(self):
        result = self.validate(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\ndata:\n  key: value\n"
            "---\napiVersion: cilium.io/v2\nkind: CiliumNetworkPolicy\n"
            "metadata:\n  name: allow-demo\nspec:\n  endpointSelector:\n"
            "    matchLabels:\n      app: pacman\n  ingress: []\n"
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"Valid:\s*2\b")
        self.assertRegex(result.stdout, r"Skipped:\s*0\b")
        self.assertRegex(result.stdout, r"Errors:\s*0\b")

    def test_invalid_cilium_resource_is_rejected_by_its_schema(self):
        result = self.validate(
            "apiVersion: cilium.io/v2\nkind: CiliumNetworkPolicy\n"
            "metadata:\n  name: invalid-demo\nspec: not-an-object\n"
        )
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"Invalid:\s*1\b")
        self.assertRegex(result.stdout, r"Skipped:\s*0\b")
        self.assertRegex(result.stdout, r"Errors:\s*0\b")


if __name__ == "__main__":
    unittest.main()
