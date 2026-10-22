#!/usr/bin/env python3
"""Strict assertions shared by the native BPF runner and CPU-only tests."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


HTTP_ROUTES = (
    "internal_score_read",
    "public_score_read",
    "public_score_write",
    "internal_user_session",
    "public_user_session",
    "internal_user_stats_write",
    "public_user_stats_write",
)
HTTP_SCORE_VERSION = {"baseline": 200, "active": 403, "recovery": 200}
POLICY_NAME = "pacman-demo-process-and-file-observe"
FILE_PROBE_PATH = "/app/demo-fixtures/synthetic-secret.txt"
EXEC_PROBE_PATH = "/app/tools/demo-exec-probe.js"


def assert_http_observation(stage: str, value: Any) -> None:
    if stage not in HTTP_SCORE_VERSION:
        raise ValueError(f"unknown HTTP stage: {stage}")
    if not isinstance(value, Mapping):
        raise ValueError("HTTP probe output must be a JSON object")

    score_version = value.get("score_version")
    expected_score_version = HTTP_SCORE_VERSION[stage]
    if type(score_version) is not int or score_version != expected_score_version:
        raise ValueError(
            f"{stage}: score /version expected HTTP {expected_score_version}, "
            f"observed {score_version!r}"
        )

    routes = value.get("routes")
    if not isinstance(routes, Mapping):
        raise ValueError("HTTP probe output is missing its routes object")
    for route in HTTP_ROUTES:
        status = routes.get(route)
        if type(status) is not int or status != 200:
            raise ValueError(f"{stage}: {route} expected HTTP 200, observed {status!r}")


def _field(value: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return None


def _same_pod(process: Any, namespace: str, pod: str) -> bool:
    if not isinstance(process, Mapping):
        return False
    pod_info = process.get("pod")
    if not isinstance(pod_info, Mapping):
        return False
    return pod_info.get("namespace") == namespace and pod_info.get("name") == pod


def _matching_file_event(
    event: Mapping[str, Any], namespace: str, pod: str, policy: str
) -> bool:
    kprobe = _field(event, "process_kprobe", "processKprobe")
    if not isinstance(kprobe, Mapping):
        return False
    if _field(kprobe, "policy_name", "policyName") != policy:
        return False
    if _field(kprobe, "function_name", "functionName") != "security_file_open":
        return False
    if not _same_pod(kprobe.get("process"), namespace, pod):
        return False
    args = kprobe.get("args")
    if not isinstance(args, list):
        return False
    for arg in args:
        if not isinstance(arg, Mapping):
            continue
        file_arg = _field(arg, "file_arg", "fileArg")
        if isinstance(file_arg, Mapping) and file_arg.get("path") == FILE_PROBE_PATH:
            return True
    return False


def _matching_exec_event(event: Mapping[str, Any], namespace: str, pod: str) -> bool:
    process_exec = _field(event, "process_exec", "processExec")
    if not isinstance(process_exec, Mapping):
        return False
    process = process_exec.get("process")
    if not _same_pod(process, namespace, pod):
        return False
    parent = process_exec.get("parent")
    if not _same_pod(parent, namespace, pod):
        return False
    if not isinstance(process, Mapping):
        return False
    binary = process.get("binary")
    arguments = process.get("arguments")
    if isinstance(arguments, list):
        arguments = " ".join(str(part) for part in arguments)
    return binary == EXEC_PROBE_PATH and isinstance(arguments, str) and "demo-exec-probe" in arguments


def assert_tetragon_events(
    lines: Iterable[str], namespace: str, pod: str, policy: str = POLICY_NAME
) -> None:
    found_file = False
    found_exec = False
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, Mapping):
            continue
        found_file = found_file or _matching_file_event(event, namespace, pod, policy)
        found_exec = found_exec or _matching_exec_event(event, namespace, pod)
        if found_file and found_exec:
            return
    missing = []
    if not found_file:
        missing.append(f"policy-correlated security_file_open for {FILE_PROBE_PATH}")
    if not found_exec:
        missing.append(f"child process_exec for {EXEC_PROBE_PATH} containing demo-exec-probe")
    raise ValueError(
        f"missing Tetragon event(s) for {namespace}/{pod}: " + " and ".join(missing)
    )


def assert_policy_loaded(value: Any, policy: str = POLICY_NAME) -> None:
    if not isinstance(value, Mapping) or not isinstance(value.get("policies"), list):
        raise ValueError("tetra output must contain a policies array")
    for item in value["policies"]:
        if not isinstance(item, Mapping) or item.get("name") != policy:
            continue
        state = item.get("state")
        if state not in ("TP_STATE_ENABLED", 1, "1"):
            raise ValueError(f"policy {policy} is not enabled (state={state!r})")
        if item.get("error"):
            raise ValueError(f"policy {policy} reports an error")
        sensors = item.get("sensors")
        if not isinstance(sensors, list) or not sensors:
            raise ValueError(f"policy {policy} has no loaded sensors")
        return
    raise ValueError(f"policy {policy} is absent from Tetragon's loaded policy list")


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    http = subparsers.add_parser("http", help="assert one Cilium HTTP phase result")
    http.add_argument("stage", choices=HTTP_SCORE_VERSION)
    http.add_argument("json_file")
    events = subparsers.add_parser("events", help="assert the correlated Tetragon event pair")
    events.add_argument("jsonl_file")
    events.add_argument("--namespace", required=True)
    events.add_argument("--pod", required=True)
    events.add_argument("--policy", default=POLICY_NAME)
    policy = subparsers.add_parser("policy", help="assert the Tetragon policy is loaded")
    policy.add_argument("json_file")
    policy.add_argument("--name", default=POLICY_NAME)
    args = parser.parse_args(argv)

    try:
        if args.command == "http":
            assert_http_observation(args.stage, _load_json(args.json_file))
            print(f"PASS {args.stage} HTTP behavior")
        elif args.command == "events":
            with Path(args.jsonl_file).open(encoding="utf-8") as stream:
                assert_tetragon_events(stream, args.namespace, args.pod, args.policy)
            print(f"PASS Tetragon events for {args.namespace}/{args.pod}")
        else:
            assert_policy_loaded(_load_json(args.json_file), args.name)
            print(f"PASS Tetragon policy {args.name} is enabled with loaded sensors")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
