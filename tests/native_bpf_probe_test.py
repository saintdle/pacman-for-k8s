"""Exercise the native BPF HTTP probe without requiring a cluster."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path


RUNNER = Path(__file__).with_name("native-bpf.sh")
RUN_TAG = "probe-run-42x"
EXPECTED_NAME = f"native-bpf-{RUN_TAG}"
STAGES = ("baseline", "active", "recovery")


class NativeBpfHttpProbeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-bpf-http-probe-")
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.pod_bin = self.root / "pod-bin"
        self.pod_bin.mkdir()
        node = shutil.which("node")
        if not node:
            self.fail("Node.js is required for the native probe regression")
        (self.pod_bin / "node").symlink_to(node)

        self.request_log = self.root / "requests.jsonl"
        self.fetch_stub = self.root / "mock-fetch.cjs"
        self.fetch_stub.write_text(
            """const fs = require('node:fs');
globalThis.fetch = async (input, options = {}) => {
  const url = new URL(String(input));
  const body = options.body == null ? '' : String(options.body);
  const name = url.searchParams.get('name') ?? new URLSearchParams(body).get('name');
  if (name !== null) {
    fs.appendFileSync(process.env.NATIVE_BPF_REQUEST_LOG, JSON.stringify({
      stage: process.env.NATIVE_BPF_TEST_STAGE,
      name,
    }) + '\\n');
  }
  const status = url.href === 'http://pacman-score:8080/version' &&
    process.env.NATIVE_BPF_TEST_STAGE === 'active' ? 403 : 200;
  const responseBody = url.pathname.endsWith('/session') || url.pathname === '/user/id'
    ? JSON.stringify({ userId: 'mock-user-1' }) : '';
  return { status, text: async () => responseBody };
};
""",
            encoding="utf-8",
        )

        kubectl = self.bin / "kubectl"
        kubectl.write_text(
            f"#!{shutil.which('python3')}\n"
            + """import os
import subprocess
import sys

arguments = sys.argv[1:]
try:
    command = arguments[arguments.index("--") + 1:]
except (ValueError, IndexError):
    print("mock kubectl expected an exec command after --", file=sys.stderr)
    raise SystemExit(2)

pod_env = {
    "PATH": os.environ["NATIVE_BPF_POD_BIN"],
    "NODE_OPTIONS": os.environ["NATIVE_BPF_NODE_OPTIONS"],
    "NATIVE_BPF_TEST_STAGE": os.environ["NATIVE_BPF_TEST_STAGE"],
    "NATIVE_BPF_REQUEST_LOG": os.environ["NATIVE_BPF_REQUEST_LOG"],
}
try:
    result = subprocess.run(command, env=pod_env, capture_output=True, text=True, check=False)
except FileNotFoundError as error:
    print(f"pod exec could not find {error.filename!r} on PATH", file=sys.stderr)
    raise SystemExit(127)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
""",
            encoding="utf-8",
        )
        kubectl.chmod(0o755)

        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.root),
            "EVIDENCE_DIR": str(self.root / "evidence"),
            "NATIVE_BPF_RUN_TAG": RUN_TAG,
            "NATIVE_BPF_POD_BIN": str(self.pod_bin),
            "NATIVE_BPF_NODE_OPTIONS": f"--require={self.fetch_stub}",
            "NATIVE_BPF_REQUEST_LOG": str(self.request_log),
            "NATIVE_BPF_TEST_STAGE": "baseline",
            "NATIVE_BPF_COMMAND_KILL_GRACE_SECONDS": "1",
            "NATIVE_BPF_KUBECTL_TIMEOUT_SECONDS": "2",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_run_http_probe_executes_node_without_env_and_propagates_run_tag(self):
        script = (
            f"source {shlex.quote(str(RUNNER))}\n"
            'WEB_POD="mock-web-pod"\n'
            # Keep the harness's bounded retry loop fast when the old command fails.
            "sleep() { :; }\n"
            "export NATIVE_BPF_TEST_STAGE\n"
            "for NATIVE_BPF_TEST_STAGE in baseline active recovery; do\n"
            '  run_http_probe "$NATIVE_BPF_TEST_STAGE"\n'
            "done\n"
        )
        result = subprocess.run(
            ["bash", "-c", script],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"run_http_probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

        records = [
            json.loads(line)
            for line in self.request_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 5 * len(STAGES))
        for stage in STAGES:
            with self.subTest(stage=stage):
                names = Counter(record["name"] for record in records if record["stage"] == stage)
                self.assertEqual(names, Counter({EXPECTED_NAME: 5}))


if __name__ == "__main__":
    unittest.main()
