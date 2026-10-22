"""CPU-only contract tests for the assertions used by native-bpf.sh."""

import json
import unittest

from native_bpf_assert import (
    HTTP_ROUTES,
    POLICY_NAME,
    assert_http_observation,
    assert_policy_loaded,
    assert_tetragon_events,
)


def http_result(score_version):
    return {
        "score_version": score_version,
        "routes": {route: 200 for route in HTTP_ROUTES},
    }


def file_event(namespace="native-tetragon", pod="pacman-tetragon-demo-abc"):
    return {
        "process_kprobe": {
            "policy_name": POLICY_NAME,
            "function_name": "security_file_open",
            "process": {"pod": {"namespace": namespace, "name": pod}},
            "args": [{"file_arg": {"path": "/etc/hostname"}}],
        }
    }


def exec_event(namespace="native-tetragon", pod="pacman-tetragon-demo-abc"):
    return {
        "process_exec": {
            "process": {
                "pod": {"namespace": namespace, "name": pod},
                "binary": "/usr/local/bin/node",
                "arguments": "node /app/tools/demo-exec-probe.js",
            },
            "parent": {"pod": {"namespace": namespace, "name": pod}},
        }
    }


class HttpAssertionsTest(unittest.TestCase):
    def test_expected_allow_deny_recovery_sequence_passes(self):
        for stage, status in (("baseline", 200), ("active", 403), ("recovery", 200)):
            with self.subTest(stage=stage):
                assert_http_observation(stage, http_result(status))

    def test_timeout_cannot_satisfy_explicit_denial(self):
        for status in (None, "timeout", 0, 404, 200, 500):
            with self.subTest(status=status), self.assertRaises(ValueError):
                assert_http_observation("active", {**http_result(status), "score_version": status})

    def test_allowed_route_failure_cannot_pass_active_policy(self):
        result = http_result(403)
        result["routes"]["public_user_session"] = 403
        with self.assertRaisesRegex(ValueError, "public_user_session"):
            assert_http_observation("active", result)


class TetragonAssertionsTest(unittest.TestCase):
    def test_valid_correlated_file_and_child_exec_pair_passes(self):
        assert_tetragon_events(
            [
                "not-json",
                json.dumps(file_event()),
                json.dumps(exec_event()),
            ],
            "native-tetragon",
            "pacman-tetragon-demo-abc",
        )

    def test_wrong_file_namespace_pod_path_or_policy_fails(self):
        for reason, mutate, namespace, pod in (
            ("namespace", lambda event: event["process_kprobe"]["process"]["pod"].update(namespace="other"), "native-tetragon", "pacman-tetragon-demo-abc"),
            ("pod", lambda event: event["process_kprobe"]["process"]["pod"].update(name="other"), "native-tetragon", "pacman-tetragon-demo-abc"),
            ("path", lambda event: event["process_kprobe"]["args"][0]["file_arg"].update(path="/etc/passwd"), "native-tetragon", "pacman-tetragon-demo-abc"),
            ("policy", lambda event: event["process_kprobe"].update(policy_name="other"), "native-tetragon", "pacman-tetragon-demo-abc"),
        ):
            with self.subTest(reason=reason):
                invalid = file_event()
                mutate(invalid)
                with self.assertRaises(ValueError):
                    assert_tetragon_events([json.dumps(invalid), json.dumps(exec_event())], namespace, pod)

    def test_wrong_exec_namespace_pod_or_process_fails(self):
        for reason, mutate in (
            ("namespace", lambda event: event["process_exec"]["process"]["pod"].update(namespace="other")),
            ("pod", lambda event: event["process_exec"]["process"]["pod"].update(name="other")),
            ("parent pod", lambda event: event["process_exec"]["parent"]["pod"].update(name="other")),
            ("binary", lambda event: event["process_exec"]["process"].update(binary="/bin/sh")),
            ("arguments", lambda event: event["process_exec"]["process"].update(arguments="node other.js")),
        ):
            with self.subTest(reason=reason):
                invalid = exec_event()
                mutate(invalid)
                with self.assertRaises(ValueError):
                    assert_tetragon_events([json.dumps(file_event()), json.dumps(invalid)], "native-tetragon", "pacman-tetragon-demo-abc")

    def test_missing_event_fails(self):
        with self.assertRaises(ValueError):
            assert_tetragon_events([json.dumps(file_event())], "native-tetragon", "pacman-tetragon-demo-abc")


class PolicyAssertionsTest(unittest.TestCase):
    def test_loaded_enabled_policy_with_sensors_passes(self):
        assert_policy_loaded({"policies": [{"name": POLICY_NAME, "state": "TP_STATE_ENABLED", "sensors": ["sensor"], "error": ""}]})

    def test_missing_disabled_or_failed_policy_does_not_pass(self):
        for item in (
            {"name": POLICY_NAME, "state": "TP_STATE_DISABLED", "sensors": ["sensor"]},
            {"name": POLICY_NAME, "state": "TP_STATE_LOAD_ERROR", "sensors": []},
            {"name": "other", "state": "TP_STATE_ENABLED", "sensors": ["sensor"]},
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                assert_policy_loaded({"policies": [item]})


if __name__ == "__main__":
    unittest.main()
