"""CPU-only checks for the Tetragon return-value contract used by the demo."""

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "cilium-demos/tetragon-enforcement/tracingpolicy.yaml"


def _kprobe_specs(document):
    """Return the small kprobe entries needed by the loader-contract check."""
    specs = []
    current = None
    in_kprobes = False
    for line in document.splitlines():
        if line == "  kprobes:":
            in_kprobes = True
            continue
        if in_kprobes and re.fullmatch(r"  [A-Za-z][A-Za-z0-9_]*:\s*", line):
            break
        if not in_kprobes:
            continue
        if re.match(r"^    - call:\s*", line):
            if current is not None:
                specs.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        specs.append(current)
    return specs


def _has_typed_return_arg(spec):
    for index, line in enumerate(spec):
        match = re.fullmatch(r"      returnArg:\s*(.*?)(?:\s+#.*)?", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value.startswith("{"):
            inline_type = re.search(r"\btype:\s*([^,}]+)", value)
            return inline_type is not None and inline_type.group(1).strip(" '\"\t") not in ("", "null", "~")
        if value:
            return False
        for nested in spec[index + 1 :]:
            if nested.strip() and len(nested) - len(nested.lstrip()) <= 6:
                break
            type_match = re.fullmatch(r"        type:\s*(.*?)(?:\s+#.*)?", nested)
            if type_match:
                return type_match.group(1).strip(" '\"\t") not in ("", "null", "~")
        return False
    return False


def _return_capture_errors(document):
    errors = []
    for spec in _kprobe_specs(document):
        call = re.match(r"^    - call:\s*(\S+)", spec[0]).group(1)
        has_return = any(
            re.fullmatch(r"      return:\s*true\s*(?:#.*)?", line, re.IGNORECASE)
            for line in spec
        )
        if has_return and not _has_typed_return_arg(spec):
            errors.append(f"{call}: return: true requires a typed returnArg")
    return errors


class TetragonReturnContractTest(unittest.TestCase):
    def test_demo_policy_meets_tetragon_return_capture_contract(self):
        policy = POLICY_PATH.read_text(encoding="utf-8")
        calls = [
            re.match(r"^    - call:\s*(\S+)", spec[0]).group(1)
            for spec in _kprobe_specs(policy)
        ]
        self.assertEqual(["security_bprm_check", "security_file_open"], calls)
        self.assertEqual([], _return_capture_errors(policy))

    def test_return_capture_requires_a_nonempty_return_type(self):
        valid = """  kprobes:
    - call: sk_alloc
      return: true
      returnArg:
        index: 0
        type: sock
"""
        self.assertEqual([], _return_capture_errors(valid))

        for reason, return_arg in (
            ("missing returnArg", ""),
            ("empty return type", "      returnArg:\n        index: 0\n        type: ''\n"),
        ):
            with self.subTest(reason=reason):
                invalid = "  kprobes:\n    - call: sk_alloc\n      return: true\n" + return_arg
                self.assertEqual(1, len(_return_capture_errors(invalid)))


if __name__ == "__main__":
    unittest.main()
