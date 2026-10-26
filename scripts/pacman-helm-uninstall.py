#!/usr/bin/env python3
"""Safely uninstall this project's Pac-Man Helm release."""

import json
import os
import re
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def usage():
    fail("Usage: pacman-uninstall.sh --helm-release NAME [--delete-data]")


def parse_args(argv):
    if len(argv) not in (2, 3) or argv[0] != "--helm-release":
        usage()
    release = argv[1]
    delete_data = len(argv) == 3 and argv[2] == "--delete-data"
    if len(argv) == 3 and not delete_data:
        usage()
    if len(release) > 53 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", release):
        fail("Invalid Helm release name")
    namespace = os.environ.get("NAMESPACE", "pacman-demo")
    if len(namespace) > 63 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace):
        fail("Invalid namespace")
    return release, namespace, delete_data


def run(argv, *, data=None):
    try:
        result = subprocess.run(
            argv,
            input=data,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=240,
        )
    except OSError:
        fail(f"Could not run {argv[0]}")
    except subprocess.TimeoutExpired:
        fail(f"{argv[0]} command timed out; no diagnostic output was shown")
    if result.returncode:
        fail(f"{argv[0]} command failed (exit {result.returncode}); no diagnostic output was shown")
    return result.stdout


def scalar(value):
    value = value.strip()
    if not value:
        return ""
    # Only simple scalar syntax is needed for Kubernetes object identity and
    # Helm's keep annotation. Unrecognized YAML remains ambiguous and fails
    # closed when it appears on a PVC or Secret.
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, str) else None
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            return None
        return value[1:-1].replace("''", "'")
    if value[0] in "[{|>&*!%@`":
        return None
    return value


def parse_manifest(manifest):
    """Read only top-level Kubernetes identity and metadata from rendered YAML."""
    resources = []
    for document in re.split(r"(?m)^---[ \t]*(?:#.*)?$", manifest):
        lines = document.splitlines()
        kind = None
        metadata_start = None
        for index, line in enumerate(lines):
            match = re.fullmatch(r"kind:[ \t]*(.*?)[ \t]*(?:#.*)?", line)
            if match:
                kind = scalar(match.group(1))
            if re.fullmatch(r"metadata:[ \t]*(?:#.*)?", line):
                metadata_start = index + 1
        if kind is None:
            if document.strip():
                # Some Helm versions include only comments between documents.
                if any(line.strip() and not line.lstrip().startswith("#") for line in lines):
                    fail("Could not identify a resource in the Helm manifest")
            continue
        if metadata_start is None:
            if kind in ("PersistentVolumeClaim", "Secret", "Namespace"):
                fail("Could not identify resource metadata in the Helm manifest")
            continue
        name = None
        labels = {}
        annotations = {}
        metadata_end = len(lines)
        for i in range(metadata_start, len(lines)):
            line = lines[i]
            if line and not line[0].isspace() and not line.lstrip().startswith("#"):
                metadata_end = i
                break
        section = None
        for line in lines[metadata_start:metadata_end]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line.startswith("  ") and not line.startswith("    "):
                field = re.fullmatch(r"  ([A-Za-z0-9_./-]+):[ \t]*(.*?)[ \t]*(?:#.*)?", line)
                if not field:
                    fail("Could not parse resource metadata in the Helm manifest")
                key, value = field.groups()
                if key == "name":
                    name = scalar(value)
                    section = None
                elif key in ("labels", "annotations"):
                    if value:
                        if value not in ("{}", "null"):
                            section = "invalid"
                        else:
                            section = None
                    else:
                        section = key
                else:
                    section = None
                continue
            if line.startswith("    "):
                field = re.fullmatch(r"    ([A-Za-z0-9_./-]+):[ \t]*(.*?)[ \t]*(?:#.*)?", line)
                if not field:
                    if section in ("labels", "annotations"):
                        section = "invalid"
                    continue
                key, value = field.groups()
                if section == "labels":
                    parsed = scalar(value)
                    labels[key] = parsed
                elif section == "annotations":
                    parsed = scalar(value)
                    annotations[key] = parsed
                elif section == "invalid":
                    # Flow-style metadata is not interpreted as ownership proof.
                    continue
        if kind in ("PersistentVolumeClaim", "Secret", "Namespace"):
            if not name or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", name):
                fail(f"Could not safely identify a Helm-managed {kind} name")
            if "invalid" in (section,):
                fail(f"Could not safely parse Helm-managed {kind} metadata")
        resources.append({"kind": kind, "name": name, "labels": labels, "annotations": annotations})
    return resources


def start_proxy(context):
    try:
        process = subprocess.Popen(
            ["kubectl", "--context", context, "proxy", "--address=127.0.0.1", "--port=0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except OSError:
        fail("Could not start kubectl proxy")
    deadline = time.monotonic() + 10
    output = b""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            fail("kubectl proxy failed to start")
        ready, _, _ = select.select([process.stdout], [], [], 0.2)
        if not ready:
            continue
        chunk = os.read(process.stdout.fileno(), 4096)
        if not chunk:
            break
        output += chunk
        match = re.search(rb"127\.0\.0\.1:(\d+)", output)
        if match:
            return process, f"http://127.0.0.1:{match.group(1).decode()}"
    process.terminate()
    process.wait(timeout=5)
    fail("kubectl proxy did not report a listening port")


def proxy_request(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        # Do not emit API response bodies: Secret responses and cluster details
        # must not enter terminal output.
        error.read()
        return error.code, {}
    except (OSError, TimeoutError, json.JSONDecodeError):
        fail("Kubernetes API query failed; no cleanup was attempted")


def api_path(namespace, resource, name):
    api_resource = {
        "PersistentVolumeClaim": "persistentvolumeclaims",
        "Secret": "secrets",
    }[resource["kind"]]
    return f"/api/v1/namespaces/{namespace}/{api_resource}/{name}"


def validate_live(resource, obj, release, namespace, chart_label):
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("namespace") != namespace:
        fail(f"Refusing {resource['kind']} {resource['name']}: namespace ownership is unverified")
    labels = metadata.get("labels") or {}
    expected = {
        "app.kubernetes.io/part-of": "pacman",
        "app.kubernetes.io/instance": release,
        "app.kubernetes.io/managed-by": "Helm",
        "helm.sh/chart": chart_label,
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        fail(f"Refusing {resource['kind']} {resource['name']}: Helm ownership is unverified")
    annotations = metadata.get("annotations") or {}
    for key, value in (("meta.helm.sh/release-name", release), ("meta.helm.sh/release-namespace", namespace)):
        if key in annotations and annotations[key] != value:
            fail(f"Refusing {resource['kind']} {resource['name']}: Helm ownership conflicts")
    if not metadata.get("uid") or not metadata.get("resourceVersion"):
        fail(f"Refusing {resource['kind']} {resource['name']}: Kubernetes UID is unavailable")
    return metadata


def main(argv):
    release, namespace, delete_data = parse_args(argv)
    for variable in ("HELM_KUBEAPISERVER", "HELM_KUBETOKEN", "HELM_KUBECAFILE",
                     "HELM_KUBEASUSER", "HELM_KUBEASGROUPS", "HELM_KUBETLS_SERVER_NAME"):
        if os.environ.get(variable):
            fail(f"Refusing mismatched Helm Kubernetes override: {variable}")
    if os.environ.get("HELM_KUBEINSECURE_SKIP_TLS_VERIFY", "false").lower() == "true":
        fail("Refusing mismatched Helm Kubernetes TLS override")
    try:
        context = os.environ.get("HELM_KUBECONTEXT") or run(["kubectl", "config", "current-context"]).strip()
    except SystemExit:
        raise
    if not context:
        fail("Could not determine the Kubernetes context")

    helm = ["helm", "--kube-context", context]
    try:
        releases = json.loads(run(helm + ["list", "--all", "--namespace", namespace,
                                          "--filter", "^" + release + "$", "--output", "json"]))
    except json.JSONDecodeError:
        fail("Helm returned an unreadable release list")
    if not isinstance(releases, list) or len(releases) != 1:
        fail("No unique Helm release matches the selected name and namespace")
    listed = releases[0]
    if listed.get("name") != release or listed.get("namespace") != namespace:
        fail("Helm release identity does not match the selected namespace")
    if listed.get("status") not in ("deployed", "uninstalled"):
        fail("Refusing Helm release with an unexpected status")

    try:
        metadata = json.loads(run(helm + ["get", "metadata", release, "--namespace", namespace, "--output", "json"]))
    except json.JSONDecodeError:
        fail("Helm returned unreadable chart metadata")
    version = metadata.get("version") if isinstance(metadata, dict) else None
    semver = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
    if (metadata.get("name") != release or metadata.get("namespace") != namespace
            or metadata.get("chart") != "pacman" or metadata.get("status") != listed.get("status")
            or not isinstance(version, str)
            or not re.fullmatch(semver, version)
            or listed.get("chart") != "pacman-" + version):
        fail("Selected release is not the exact Pac-Man chart")
    chart_label = "pacman-" + version.replace("+", "_")

    manifest = run(helm + ["get", "manifest", release, "--namespace", namespace])
    resources = parse_manifest(manifest)
    if not resources:
        fail("Helm release manifest is empty")
    if any(resource["kind"] == "Namespace" for resource in resources):
        fail("Refusing a Pac-Man release manifest that includes a Namespace")
    candidates = [resource for resource in resources
                  if resource["kind"] in ("PersistentVolumeClaim", "Secret")]
    seen = set()
    for resource in candidates:
        key = (resource["kind"], resource["name"])
        if key in seen:
            fail("Helm manifest has duplicate data resource identities")
        seen.add(key)
        labels = resource["labels"]
        expected = {
            "app.kubernetes.io/part-of": "pacman",
            "app.kubernetes.io/instance": release,
            "app.kubernetes.io/managed-by": "Helm",
            "helm.sh/chart": chart_label,
        }
        if any(labels.get(key) != value for key, value in expected.items()):
            fail(f"Refusing {resource['kind']} {resource['name']}: chart manifest ownership is unverified")
        for key, value in (("meta.helm.sh/release-name", release), ("meta.helm.sh/release-namespace", namespace)):
            if key in resource["annotations"] and resource["annotations"][key] != value:
                fail(f"Refusing {resource['kind']} {resource['name']}: chart manifest ownership conflicts")

    proxy = None
    captured = []
    try:
        if candidates:
            proxy, base = start_proxy(context)
            for resource in candidates:
                status, obj = proxy_request(base, "GET", api_path(namespace, resource, resource["name"]))
                if status == 404:
                    captured.append((resource, None))
                    continue
                if status != 200:
                    fail(f"Kubernetes query failed for {resource['kind']} {resource['name']}; no cleanup was attempted")
                if obj.get("kind") not in ("PartialObjectMetadata", resource["kind"]):
                    fail("Kubernetes returned an unexpected resource type")
                live = validate_live(resource, obj, release, namespace, chart_label)
                if not delete_data and resource["annotations"].get("helm.sh/resource-policy") != "keep":
                    fail(f"{resource['kind']} {resource['name']} is not marked keep by this chart; use --delete-data to remove it")
                captured.append((resource, {
                    "uid": live["uid"],
                    "resourceVersion": live["resourceVersion"],
                }))

        if listed["status"] == "deployed":
            run(helm + ["uninstall", "--namespace", namespace, release, "--keep-history",
                        "--wait", "--timeout", "180s"])
        if delete_data:
            for resource, identity in captured:
                if identity is None:
                    continue
                body = {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": identity,
                    "propagationPolicy": "Background",
                }
                status, _ = proxy_request(base, "DELETE", api_path(namespace, resource, resource["name"]), body)
                if status == 404:
                    continue
                if status not in (200, 202):
                    fail(f"UID-bound deletion failed for {resource['kind']} {resource['name']}; rerun after reviewing cluster state")
        action = "uninstalled and verified release-owned data removed" if delete_data else "uninstalled; release-owned data retained"
        print(f"Helm release '{release}' in namespace '{namespace}' {action}.")
    finally:
        if proxy is not None:
            proxy.terminate()
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()


if __name__ == "__main__":
    main(sys.argv[1:])
