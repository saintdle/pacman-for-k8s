import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
FAKE_KUBECTL = r'''#!/usr/bin/env python3
import http.server, json, os, pathlib, re, sys
state_path = pathlib.Path(os.environ['FAKE_STATE'])
def load(): return json.loads(state_path.read_text())
def save(value): state_path.write_text(json.dumps(value))
args = sys.argv[1:]
if args[:1] == ['--context']: args = args[2:]
if args[:2] == ['config', 'current-context']:
    print('safe-context')
    raise SystemExit(0)
if args[:1] == ['proxy']:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def reply(self, code, value=None):
            raw = b'' if value is None else json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            if raw: self.wfile.write(raw)
        def do_GET(self):
            s = load()
            if s.get('proxy_error'): self.reply(500); return
            match = re.search(r'/namespaces/([^/]+)/([^/]+)/([^/]+)$', self.path)
            if not match: self.reply(404); return
            namespace, kind, name = match.groups()
            kind = {'persistentvolumeclaims':'PersistentVolumeClaim','secrets':'Secret'}.get(kind)
            obj = s['objects'].get(kind+'/'+name)
            if obj is None or obj['metadata'].get('namespace') != namespace:
                self.reply(404); return
            self.reply(200, {'apiVersion':'meta.k8s.io/v1','kind':'PartialObjectMetadata',
                             'metadata':obj['metadata']})
        def do_DELETE(self):
            s = load()
            match = re.search(r'/namespaces/([^/]+)/([^/]+)/([^/]+)$', self.path)
            if not match: self.reply(404); return
            namespace, kind, name = match.groups()
            kind = {'persistentvolumeclaims':'PersistentVolumeClaim','secrets':'Secret'}.get(kind)
            obj = s['objects'].get(kind+'/'+name)
            if obj is None or obj['metadata'].get('namespace') != namespace:
                self.reply(404); return
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
            if s.get('replace_on_delete') == kind+'/'+name:
                obj['metadata']['uid'] = 'replacement-uid'
                obj['metadata']['resourceVersion'] = '999'
                s['replace_on_delete'] = None
                save(s)
            pre = body.get('preconditions', {})
            metadata = obj['metadata']
            s.setdefault('delete_attempts', []).append({'kind':kind, 'name':name, 'preconditions':pre})
            if pre.get('uid') != metadata.get('uid') or pre.get('resourceVersion') != metadata.get('resourceVersion'):
                save(s); self.reply(409, {'kind':'Status','reason':'Conflict'}); return
            del s['objects'][kind+'/'+name]
            save(s)
            self.reply(200, {'kind':'Status','status':'Success'})
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    print('Starting to serve on 127.0.0.1:'+str(server.server_address[1]), flush=True)
    server.serve_forever()
if args and args[0] == 'get':
    print('API unavailable', file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(2)
'''

FAKE_HELM = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ['FAKE_STATE'])
s = json.loads(p.read_text())
args = sys.argv[1:]
if args[:2] == ['--kube-context','safe-context']: args = args[2:]
def save(): p.write_text(json.dumps(s))
if args[0] == 'list':
    if s.get('list_error'): raise SystemExit(1)
    print(json.dumps([s['release']]))
elif args[:2] == ['get','metadata']:
    if s.get('metadata_error'): raise SystemExit(1)
    print(json.dumps(s.get('chart_metadata', {'name':s['release']['name'],
          'namespace':s['release']['namespace'],'chart':'pacman','version':'1.2.3',
          'status':s['release']['status']})))
elif args[:2] == ['get','manifest']:
    if s.get('manifest_error'): raise SystemExit(1)
    print(s['manifest'], end='')
elif args[0] == 'uninstall':
    s.setdefault('helm_calls', []).append(args)
    if s.get('uninstall_error'):
        save(); print('uninstall failed', file=sys.stderr); raise SystemExit(1)
    s['release']['status'] = 'uninstalled'
    for resource in s['manifest_resources']:
        keep = resource.get('annotations', {}).get('helm.sh/resource-policy') == 'keep'
        key = resource['kind']+'/'+resource['name']
        if not keep: s['objects'].pop(key, None)
    save()
else:
    print('unexpected Helm command', file=sys.stderr)
    raise SystemExit(2)
'''


def labels(release="pacman", version="1.2.3"):
    return {
        "app.kubernetes.io/part-of": "pacman",
        "app.kubernetes.io/instance": release,
        "app.kubernetes.io/managed-by": "Helm",
        "helm.sh/chart": "pacman-" + version,
    }


def resource(kind, name, keep=True, release="pacman", namespace="helm-test"):
    annotations = {"helm.sh/resource-policy": "keep"} if keep else {}
    return {
        "kind": kind,
        "name": name,
        "labels": labels(release),
        "annotations": annotations,
        "namespace": namespace,
    }


def render(resources):
    docs = []
    for item in resources:
        data = [
            "apiVersion: v1",
            "kind: " + item["kind"],
            "metadata:",
            "  name: " + item["name"],
            "  namespace: " + item["namespace"],
            "  labels:",
        ]
        data += [f"    {key}: {value}" for key, value in item["labels"].items()]
        if item["annotations"]:
            data.append("  annotations:")
            data += [f"    {key}: {value}" for key, value in item["annotations"].items()]
        if item["kind"] == "Secret":
            data += ["type: Opaque", "stringData:", "  generated: test-fixture"]
        docs.append("\n".join(data))
    return "\n---\n".join(docs) + "\n"


def live_object(item, uid=None):
    metadata = {
        "name": item["name"],
        "namespace": item["namespace"],
        "labels": dict(item["labels"]),
        "annotations": dict(item["annotations"]),
        "uid": uid or "uid-" + item["kind"] + "-" + item["name"],
        "resourceVersion": "42",
    }
    return {"apiVersion": "v1", "kind": item["kind"], "metadata": metadata}


class HelmUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = pathlib.Path(self.temp.name)
        (self.directory / "kubectl").write_text(FAKE_KUBECTL)
        (self.directory / "helm").write_text(FAKE_HELM)
        (self.directory / "kubectl").chmod(0o755)
        (self.directory / "helm").chmod(0o755)
        self.pvc = resource("PersistentVolumeClaim", "pacman-mongo", keep=True)
        self.secret = resource("Secret", "pacman-mongo", keep=True)
        user_secret = {
            "kind": "Secret", "name": "my-existing-secret", "namespace": "helm-test",
            "labels": {"app.kubernetes.io/managed-by": "user"}, "annotations": {},
        }
        self.manifest_resources = [self.pvc, self.secret]
        self.state = {
            "release": {"name": "pacman", "namespace": "helm-test", "status": "deployed",
                        "chart": "pacman-1.2.3"},
            "manifest_resources": self.manifest_resources,
            "manifest": render(self.manifest_resources),
            "objects": {
                "PersistentVolumeClaim/pacman-mongo": live_object(self.pvc),
                "Secret/pacman-mongo": live_object(self.secret),
                "Secret/my-existing-secret": live_object(user_secret, "user-secret-uid"),
            },
            "delete_attempts": [],
            "helm_calls": [],
        }
        self.state_path = self.directory / "state.json"
        self.write_state()

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def run_script(self, *args):
        env = {
            **os.environ,
            "PATH": str(self.directory) + os.pathsep + os.environ["PATH"],
            "FAKE_STATE": str(self.state_path),
            "NAMESPACE": "helm-test",
        }
        script = os.environ.get("PACMAN_UNINSTALL_SCRIPT", str(ROOT / "pacman-uninstall.sh"))
        return subprocess.run(["bash", script, *args], env=env, capture_output=True, text=True, timeout=20)

    def test_explicit_cleanup_removes_only_release_owned_data_and_is_repeatable(self):
        result = self.run_script("--helm-release", "pacman", "--delete-data")
        state = self.read_state()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["release"]["status"], "uninstalled")
        self.assertNotIn("PersistentVolumeClaim/pacman-mongo", state["objects"])
        self.assertNotIn("Secret/pacman-mongo", state["objects"])
        self.assertIn("Secret/my-existing-secret", state["objects"])
        self.assertEqual({(x["kind"], x["name"]) for x in state["delete_attempts"]}, {
            ("PersistentVolumeClaim", "pacman-mongo"), ("Secret", "pacman-mongo")})
        self.assertTrue(all(x["preconditions"].get("uid") and x["preconditions"].get("resourceVersion")
                            for x in state["delete_attempts"]))
        repeated = self.run_script("--helm-release", "pacman", "--delete-data")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(len(self.read_state()["delete_attempts"]), 2)

    def test_normal_mode_retains_chart_marked_data(self):
        result = self.run_script("--helm-release", "pacman")
        state = self.read_state()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["release"]["status"], "uninstalled")
        self.assertIn("PersistentVolumeClaim/pacman-mongo", state["objects"])
        self.assertIn("Secret/pacman-mongo", state["objects"])
        self.assertEqual(state["delete_attempts"], [])

    def test_normal_mode_rejects_unretained_legacy_data_before_uninstall(self):
        self.secret["annotations"] = {}
        self.manifest_resources[1] = self.secret
        self.state["manifest"] = render(self.manifest_resources)
        self.state["objects"]["Secret/pacman-mongo"] = live_object(self.secret)
        self.write_state()
        result = self.run_script("--helm-release", "pacman")
        state = self.read_state()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["helm_calls"], [])
        self.assertIn("Secret/pacman-mongo", state["objects"])

    def test_existing_absence_and_foreign_namespace_data_are_not_deleted(self):
        del self.state["objects"]["PersistentVolumeClaim/pacman-mongo"]
        self.state["objects"]["Secret/pacman-mongo"]["metadata"]["namespace"] = "other-ns"
        self.write_state()
        result = self.run_script("--helm-release", "pacman", "--delete-data")
        state = self.read_state()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["delete_attempts"], [])
        self.assertIn("Secret/my-existing-secret", state["objects"])

    def test_uid_race_cannot_delete_replacement_object(self):
        self.state["replace_on_delete"] = "Secret/pacman-mongo"
        self.write_state()
        result = self.run_script("--helm-release", "pacman", "--delete-data")
        state = self.read_state()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["objects"]["Secret/pacman-mongo"]["metadata"]["uid"], "replacement-uid")
        self.assertNotIn("PersistentVolumeClaim/pacman-mongo", state["objects"])
        self.assertEqual(len(state["delete_attempts"]), 2)
        retry = self.run_script("--helm-release", "pacman", "--delete-data")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertNotIn("Secret/pacman-mongo", self.read_state()["objects"])

    def test_release_identity_query_and_argument_errors_fail_before_mutation(self):
        cases = [
            (("--delete-data",), "delete-data alone"),
            (("--helm-release", "pacman", "--delete-data", "--delete-data"), "duplicate option"),
        ]
        for args, label in cases:
            with self.subTest(label=label):
                self.setUpStateForSubtest()
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.read_state()["helm_calls"], [])
        self.setUpStateForSubtest()
        self.state["release"]["namespace"] = "other-ns"
        self.write_state()
        result = self.run_script("--helm-release", "pacman", "--delete-data")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_state()["helm_calls"], [])
        self.setUpStateForSubtest()
        self.state["release"]["name"] = "someone-elses-release"
        self.write_state()
        result = self.run_script("--helm-release", "pacman", "--delete-data")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_state()["helm_calls"], [])
        self.setUpStateForSubtest()
        self.state["chart_metadata"] = {
            "name": "pacman", "namespace": "helm-test", "chart": "unrelated",
            "version": "1.2.3", "status": "deployed",
        }
        self.write_state()
        result = self.run_script("--helm-release", "pacman", "--delete-data")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_state()["helm_calls"], [])

    def test_query_failure_fails_before_helm_uninstall(self):
        for failure in ("list_error", "metadata_error", "manifest_error", "proxy_error"):
            with self.subTest(failure=failure):
                self.setUpStateForSubtest()
                self.state[failure] = True
                self.write_state()
                result = self.run_script("--helm-release", "pacman", "--delete-data")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.read_state()["helm_calls"], [])

    def test_foreign_live_release_ownership_rejects_before_uninstall(self):
        self.state["objects"]["Secret/pacman-mongo"]["metadata"]["labels"][
            "app.kubernetes.io/instance"] = "another-release"
        self.write_state()
        result = self.run_script("--helm-release", "pacman", "--delete-data")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_state()["helm_calls"], [])

    def test_raw_mode_rejects_helm_flag_mixing_without_helm_calls(self):
        for args in (("--delete-data",), ("keeppvc", "--helm-release", "pacman")):
            with self.subTest(args=args):
                self.setUpStateForSubtest()
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.read_state()["helm_calls"], [])

    def setUpStateForSubtest(self):
        self.pvc = resource("PersistentVolumeClaim", "pacman-mongo", keep=True)
        self.secret = resource("Secret", "pacman-mongo", keep=True)
        self.manifest_resources = [self.pvc, self.secret]
        self.state = {
            "release": {"name": "pacman", "namespace": "helm-test", "status": "deployed",
                        "chart": "pacman-1.2.3"},
            "manifest_resources": self.manifest_resources,
            "manifest": render(self.manifest_resources),
            "objects": {
                "PersistentVolumeClaim/pacman-mongo": live_object(self.pvc),
                "Secret/pacman-mongo": live_object(self.secret),
            },
            "delete_attempts": [],
            "helm_calls": [],
        }
        self.write_state()


if __name__ == "__main__":
    unittest.main()
