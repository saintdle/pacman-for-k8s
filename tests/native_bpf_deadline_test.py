"""Fast, mocked checks for native-bpf command and phase deadlines."""

from __future__ import annotations

import os
import errno
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


RUNNER = Path(__file__).with_name("native-bpf.sh")


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class NativeBpfDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-bpf-deadline-")
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "mock-tools.log"
        self.descendant_pid = self.root / "descendant.pid"
        self.port_forward_pid = self.root / "port-forward.pid"
        self.port_forward_descendant_pid = self.root / "port-forward-descendant.pid"
        self.handoff = self.root / "handoff.txt"
        self.phase_status = self.root / "phase-status.txt"

        common_staller = """#!/bin/bash
printf '%s\\n' "$*" >>"$MOCK_LOG"
(trap '' TERM; while :; do sleep 1; done) &
echo "$!" >"$MOCK_DESCENDANT_PID"
trap '' TERM
while :; do sleep 1; done
"""
        write_executable(self.bin / "curl", common_staller)
        write_executable(
            self.bin / "kind",
            """#!/bin/bash
case "$1" in
  version) exit 0 ;;
  delete) printf 'delete %s\\n' "$*" >>"$MOCK_LOG"; exit 0 ;;
  create)
    printf 'create %s\\n' "$*" >>"$MOCK_LOG"
    (trap '' TERM; while :; do sleep 1; done) &
    echo "$!" >"$MOCK_DESCENDANT_PID"
    trap '' TERM
    while :; do sleep 1; done
    ;;
esac
exit 0
""",
        )
        write_executable(
            self.bin / "kubectl",
            """#!/bin/bash
if [[ "$*" == *port-forward* ]]; then
  echo "$$" >"$MOCK_PORT_FORWARD_PID"
  (trap '' TERM; while :; do sleep 1; done) &
  echo "$!" >"$MOCK_PORT_FORWARD_DESCENDANT_PID"
  trap '' TERM
  while :; do sleep 1; done
fi
exit 0
""",
        )
        write_executable(
            self.bin / "setsid",
            """#!/usr/bin/env python3
import os
import sys

if os.getpid() == os.getpgrp():
    child = os.fork()
    if child:
        _, status = os.waitpid(child, 0)
        os._exit(os.waitstatus_to_exitcode(status))
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
""",
        )
        for tool in ("helm", "cilium", "docker"):
            write_executable(self.bin / tool, "#!/bin/sh\nexit 0\n")

        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.root),
            "EVIDENCE_DIR": str(self.root / "evidence"),
            "NATIVE_BPF_RUN_TAG": "deadline-test-run",
            "NATIVE_BPF_MAIN_PHASE_TIMEOUT_SECONDS": "1",
            "NATIVE_BPF_MAIN_PHASE_KILL_GRACE_SECONDS": "1",
            "NATIVE_BPF_COMMAND_KILL_GRACE_SECONDS": "1",
            "NATIVE_BPF_DOWNLOAD_TIMEOUT_SECONDS": "1",
            "NATIVE_BPF_CURL_MAX_TIME_SECONDS": "1",
            "NATIVE_BPF_KIND_CREATE_TIMEOUT_SECONDS": "1",
            "NATIVE_BPF_KIND_DELETE_TIMEOUT_SECONDS": "2",
            "NATIVE_BPF_KIND_DELETE_KILL_GRACE_SECONDS": "1",
            "NATIVE_BPF_DIAGNOSTIC_TIMEOUT_SECONDS": "1",
            "NATIVE_BPF_PORT_FORWARD_TIMEOUT_SECONDS": "20",
            "NATIVE_BPF_PORT_FORWARD_STOP_GRACE_SECONDS": "1",
            "MOCK_LOG": str(self.log),
            "MOCK_DESCENDANT_PID": str(self.descendant_pid),
            "MOCK_PORT_FORWARD_PID": str(self.port_forward_pid),
            "MOCK_PORT_FORWARD_DESCENDANT_PID": str(self.port_forward_descendant_pid),
            "HANDOFF_FILE": str(self.handoff),
            "PHASE_STATUS_FILE": str(self.phase_status),
            "RUNNER_SOURCE": str(RUNNER),
        }

    def tearDown(self):
        self.temp.cleanup()

    def run_harness(self, body: str, *, timeout: float = 12) -> subprocess.CompletedProcess[str]:
        command = f"source {shlex.quote(str(RUNNER))}\n{body}"
        return subprocess.run(
            ["bash", "-c", command],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def process_alive(pid_file: Path) -> bool:
        if not pid_file.exists():
            return False
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        try:
            os.kill(pid, 0)
        except OSError as error:
            if error.errno == errno.ESRCH:
                return False
            if error.errno != errno.EPERM:
                raise
        if Path(f"/proc/{pid}/stat").exists():
            return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2] != "Z"
        return True

    def assert_stopped(self, *pid_files: Path) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(self.process_alive(path) for path in pid_files):
            time.sleep(0.05)
        for path in pid_files:
            self.assertFalse(self.process_alive(path), f"process from {path.name} survived cleanup")

    def test_stalled_download_times_out_and_preserves_failure(self):
        result = self.run_harness(
            "trap cleanup EXIT\n"
            "if download_chart cilium https://invalid.example/chart.tgz deadbeef \"$RUNNER_TEMP/chart.tgz\"; then\n"
            "  exit 0\n"
            "else\n"
            "  exit $?\n"
            "fi"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--connect-timeout", self.log.read_text(encoding="utf-8"))
        self.assert_stopped(self.descendant_pid)
        self.assertFalse(self.log.read_text(encoding="utf-8").find("delete") >= 0)

    def test_stalled_kind_create_times_out_and_attempt_marker_triggers_cleanup(self):
        result = self.run_harness(
            "trap cleanup EXIT\n"
            "if create_kind_cluster; then exit 0; else exit $?; fi"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("delete cluster", self.log.read_text(encoding="utf-8"))
        self.assert_stopped(self.descendant_pid)

    def run_worker_case(self, mode: str, expected: int | None) -> None:
        child = """source "$RUNNER_SOURCE"
printf '%s|%s|%s\\n' "$RUN_TAG" "$CLUSTER_NAME" "$KUBECONFIG_FILE" >"$HANDOFF_FILE"
touch "$CLUSTER_ATTEMPT_MARKER"
PROBE_PORT=32123
start_port_forward
echo "worker=$$" >"$RUNNER_TEMP/worker.pid"
case "$WORKER_MODE" in
  success) exit 0 ;;
  fail) exit 7 ;;
  timeout) trap '' TERM; while :; do sleep 1; done ;;
esac
"""
        self.env["WORKER_MODE"] = mode
        self.env["WORKER_CODE"] = child
        result = self.run_harness(
            "trap cleanup EXIT\n"
            "if run_main_phase bash -c \"$WORKER_CODE\"; then status=0; else status=$?; fi\n"
            "printf '%s\\n' \"$status\" >\"$PHASE_STATUS_FILE\"\n"
            "exit \"$status\""
        )
        code = int(self.phase_status.read_text(encoding="utf-8").strip())
        if expected is not None:
            self.assertEqual(code, expected)
            self.assertEqual(result.returncode, expected)
        else:
            self.assertNotEqual(code, 0)
            self.assertNotEqual(result.returncode, 0)

        run_tag, cluster, kubeconfig = self.handoff.read_text(encoding="utf-8").strip().split("|")
        self.assertEqual(run_tag, "deadline-test-run")
        self.assertEqual(cluster, "nbpf-deadline-test-run")
        self.assertEqual(kubeconfig, str(self.root / "native-bpf-kubeconfig-deadline-test-run.yaml"))
        self.assertIn("delete cluster", self.log.read_text(encoding="utf-8"))
        self.assert_stopped(self.port_forward_pid, self.port_forward_descendant_pid)

    def test_parent_cleans_background_process_after_worker_success(self):
        self.run_worker_case("success", 0)

    def test_parent_cleans_background_process_after_early_worker_failure(self):
        self.run_worker_case("fail", 7)

    def test_main_deadline_kills_term_ignoring_worker_then_parent_cleans(self):
        self.run_worker_case("timeout", None)


if __name__ == "__main__":
    unittest.main()
