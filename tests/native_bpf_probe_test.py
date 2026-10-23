"""Exercise the native BPF HTTP probe without requiring a cluster."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path


RUNNER = Path(__file__).with_name("native-bpf.sh")
POLICY = RUNNER.parent.parent / "cilium-demos/microservices-east-west/cilium-network-policy.yaml"
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
        self.kubectl_call_log = self.root / "kubectl-calls.txt"
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
        host: url.host,
        request_target: url.pathname + url.search,
        method: options.method || 'GET',
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
import time

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
stage = os.environ["NATIVE_BPF_TEST_STAGE"]
with open(os.environ["NATIVE_BPF_KUBECTL_CALL_LOG"], "a", encoding="utf-8") as call_log:
    call_log.write(stage + "\\n")
failure_marker = os.environ.get("NATIVE_BPF_TEST_FAIL_ONCE_MARKER")
if failure_marker and not os.path.exists(failure_marker):
    with open(failure_marker, "w", encoding="utf-8") as marker:
        marker.write("failed once\\n")
    print("synthetic first kubectl exec failure", file=sys.stderr)
    raise SystemExit(91)
try:
    result = subprocess.run(
        command, env=pod_env, capture_output=True, text=True, check=False,
    )
except FileNotFoundError as error:
    print(f"pod exec could not find {error.filename!r} on PATH", file=sys.stderr)
    raise SystemExit(127)
delay_marker = os.environ.get("NATIVE_BPF_TEST_POST_NODE_DELAY_MARKER")
delay_seconds = float(os.environ.get("NATIVE_BPF_TEST_POST_NODE_DELAY_SECONDS", "0"))
if (
    result.returncode == 0 and delay_marker and stage == "baseline"
    and not os.path.exists(delay_marker)
):
    with open(delay_marker, "w", encoding="utf-8") as marker:
        marker.write("delayed once\\n")
    time.sleep(delay_seconds)
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
            "NATIVE_BPF_KUBECTL_CALL_LOG": str(self.kubectl_call_log),
            "NATIVE_BPF_TEST_STAGE": "baseline",
            "NATIVE_BPF_COMMAND_KILL_GRACE_SECONDS": "1",
            "NATIVE_BPF_KUBECTL_TIMEOUT_SECONDS": "15",
        }

    def tearDown(self):
        self.temp.cleanup()

    def _run_probe_stages(self, stages, env):
        script = (
            f"source {shlex.quote(str(RUNNER))}\n"
            'WEB_POD="mock-web-pod"\n'
            # Keep the harness's bounded retry loop fast when the old command fails.
            "sleep() { :; }\n"
            "export NATIVE_BPF_TEST_STAGE\n"
            'for NATIVE_BPF_TEST_STAGE in "$@"; do run_http_probe "$NATIVE_BPF_TEST_STAGE"; done\n'
        )
        return subprocess.run(
            ["bash", "-c", script, "native-bpf-probe-test", *stages],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def _records(self):
        if not self.request_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.request_log.read_text(encoding="utf-8").splitlines()
        ]

    def _failure_details(self, result, stages):
        records = self._records()
        calls = (
            self.kubectl_call_log.read_text(encoding="utf-8").splitlines()
            if self.kubectl_call_log.exists()
            else []
        )
        details = [
            f"returncode={result.returncode}",
            f"stdout:\n{result.stdout}",
            f"stderr:\n{result.stderr}",
            f"request_counts={Counter(record['stage'] for record in records)}",
            f"kubectl_calls={Counter(calls)}",
        ]
        evidence_dir = self.root / "evidence"
        for stage in stages:
            for label, path in (
                ("result", evidence_dir / f"http-{stage}.json"),
                ("last-check", evidence_dir / f"http-{stage}-last-check.txt"),
                ("last-error", evidence_dir / f"http-{stage}-last-error.txt"),
            ):
                if path.is_file():
                    details.append(f"{stage} {label}:\n{path.read_text(encoding='utf-8')}")
        return "\n".join(details)

    def test_run_http_probe_executes_node_without_env_and_propagates_run_tag(self):
        delayed_env = self.env.copy()
        delayed_env.update({
            "NATIVE_BPF_TEST_POST_NODE_DELAY_SECONDS": "3",
            "NATIVE_BPF_TEST_POST_NODE_DELAY_MARKER": str(self.root / "post-node-delay-once"),
        })
        result = self._run_probe_stages(STAGES, delayed_env)
        self.assertEqual(
            result.returncode,
            0,
            self._failure_details(result, STAGES),
        )

        records = self._records()
        self.assertEqual(len(records), 5 * len(STAGES), self._failure_details(result, STAGES))
        for stage in STAGES:
            with self.subTest(stage=stage):
                names = Counter(record["name"] for record in records if record["stage"] == stage)
                self.assertEqual(
                    names,
                    Counter({EXPECTED_NAME: 5}),
                    self._failure_details(result, STAGES),
                )

        policy_text = POLICY.read_text(encoding="utf-8")
        user_marker = "        app.kubernetes.io/component: user\n"
        self.assertEqual(policy_text.count(user_marker), 1)
        user_rules = policy_text.split(user_marker, 1)[1].split("\n  - endpointSelector:", 1)[0]
        session_rules = [
            value.strip().strip("'\"")
            for value in re.findall(
                r"(?m)^[ ]+- method: GET[ ]*\n[ ]+path: ([^\n]+)$", user_rules
            )
            if "/internal/user/session" in value
        ]
        self.assertEqual(len(session_rules), 1, "expected one user-session GET path rule")
        session_rule = session_rules[0]
        session_records = [
            record for record in records
            if record["host"] == "pacman-user:8080"
            and record["request_target"].startswith("/internal/user/session")
        ]
        self.assertEqual(len(session_records), len(STAGES))
        for record in session_records:
            self.assertEqual(record["method"], "GET")
            self.assertRegex(
                record["request_target"], re.compile(r"\A" + session_rule + r"\Z"),
            )
        self.assertIsNotNone(re.fullmatch(session_rule, "/internal/user/session"))
        for denied_target in (
            f"/internal/user/sessions?name={EXPECTED_NAME}",
            "/internal/user/session?other=1",
            f"/internal/user/session?name={EXPECTED_NAME}&other=1",
        ):
            with self.subTest(denied_target=denied_target):
                self.assertIsNone(re.fullmatch(session_rule, denied_target))

    def test_run_http_probe_retries_one_controlled_exec_failure(self):
        retry_env = self.env.copy()
        retry_env["NATIVE_BPF_TEST_FAIL_ONCE_MARKER"] = str(self.root / "exec-failure-once")
        result = self._run_probe_stages(("baseline",), retry_env)
        self.assertEqual(result.returncode, 0, self._failure_details(result, ("baseline",)))
        self.assertEqual(
            self.kubectl_call_log.read_text(encoding="utf-8").splitlines(),
            ["baseline", "baseline"],
            self._failure_details(result, ("baseline",)),
        )
        records = self._records()
        self.assertEqual(len(records), 5, self._failure_details(result, ("baseline",)))
        self.assertEqual(Counter(record["name"] for record in records), Counter({EXPECTED_NAME: 5}))


if __name__ == "__main__":
    unittest.main()
