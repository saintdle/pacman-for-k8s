#!/usr/bin/env python3
"""Select native BPF runtime coverage without hiding the stable PR check."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


RELEVANT_PATHS = {
    "consumer-compatibility-lock.json",
    "pacman-promotion-state.json",
    "tools/pacman-promotion.mjs",
    "tests/pacman-promotion.test.mjs",
    "tests/native-bpf.sh",
    ".github/workflows/native-bpf.yml",
    ".github/workflows/lint.yml",
    ".github/workflows/pacman-promotion.yml",
}
IDENTITY_DIFF = re.compile(
    r"(?:^\s*(?:image_repository|image_manifest_list_digest)\s*:"
    r"|^\s*image\s*:\s*['\"]?(?:docker\.io/)?saintdle/pacman(?:@|:|\s|$)"
    r"|\bsource_commit\s*:\s*|\bPACMAN_IMAGE\b|"
    r"(?:docker\.io/)?saintdle/pacman(?:@|:))",
    re.IGNORECASE,
)


def is_native_relevant(event: str, paths: list[str], diff: str = "") -> tuple[bool, str]:
    if event != "pull_request":
        return True, f"{event} keeps the full native lane"

    for raw_path in paths:
        path = raw_path.removeprefix("./")
        if path in RELEVANT_PATHS:
            return True, f"{path} changes promotion identity or native coverage"
        if path.startswith(".github/workflows/"):
            return True, f"{path} changes workflow behavior"
        if path.startswith("cilium-demos/microservices-east-west/"):
            return True, f"{path} changes Cilium native scenario inputs"
        if path.startswith("cilium-demos/tetragon-enforcement/"):
            return True, f"{path} changes Tetragon native scenario inputs"
        if path.startswith("tests/native_bpf_") and path.endswith(".py"):
            return True, f"{path} changes native BPF assertions"
        if path.startswith("tests/fixtures/promotion/"):
            return True, f"{path} changes promotion identity fixtures"

    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")) and IDENTITY_DIFF.search(line[1:]):
            return True, "a changed line modifies Pac-Man image or promotion identity"

    return False, "PR changes do not affect promotion identity or native scenarios"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--paths", required=True)
    parser.add_argument("--diff", required=True)
    args = parser.parse_args()
    paths = Path(args.paths).read_text(encoding="utf-8").splitlines()
    diff = Path(args.diff).read_text(encoding="utf-8")
    selected, reason = is_native_relevant(args.event, paths, diff)
    print(f"run_native={'true' if selected else 'false'}")
    print(f"Native BPF selection: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
