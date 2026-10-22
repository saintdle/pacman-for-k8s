#!/usr/bin/env bash
# Offline lifecycle regressions. No cluster or container runtime is contacted.
set -euo pipefail
ROOT="${LIFECYCLE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
exec python3 - "$ROOT" <<'PY'
import json, os, pathlib, subprocess, sys, tempfile

root = pathlib.Path(sys.argv[1])
fake = r'''#!/usr/bin/env python3
import json, os, pathlib, re, sys
p = pathlib.Path(os.environ['FAKE_STATE'])
s = json.loads(p.read_text()); args = sys.argv[1:]
if args[:1] == ['-n']: args = args[2:]
cmd = args[0]
s['calls'].append(args)
def save(): p.write_text(json.dumps(s))
def done(text='', code=0):
    save()
    if text: print(text)
    sys.exit(code)
if cmd == 'get':
    if s.get('read_error'): done('API unavailable', 1)
    obj = args[1]
    if '/' not in obj and len(args)>2 and not args[2].startswith('-'): obj += '/' + args[2]
    obj = obj.replace('deployment/', 'deploy/').replace('service/', 'svc/').replace('secret/', 'secret/').replace('persistentvolumeclaim/', 'pvc/')
    if obj.startswith('endpointslice'): done('web-test')
    if obj == 'pod/web-test': done('pacman-web')
    if s.get('read_error_object') == obj: done('API unavailable', 1)
    value = s['objects'].get(obj)
    if value is None: done()
    if '-o' in args and args[args.index('-o')+1] == 'name': done(obj)
    if obj.startswith('secret/'): done('valid')
    if obj.startswith('svc/') and '-o' in args:
        template = args[args.index('-o')+1]
        expected_template = 'go-template={{.metadata.name}}|{{index .metadata.labels "app.kubernetes.io/part-of"}}|{{index .metadata.labels "app.kubernetes.io/name"}}|{{index .metadata.labels "app.kubernetes.io/component"}}|{{len .metadata.labels}}|{{.spec.type}}|{{len .spec.selector}}|{{index .spec.selector "app.kubernetes.io/name"}}|{{index .spec.selector "app.kubernetes.io/component"}}|{{len .spec.ports}}|{{range .spec.ports}}{{.name}},{{.port}},{{.targetPort}},{{.protocol}}{{end}}'
        if template == expected_template:
            spec = s.get('service_specs', {}).get(obj)
            if spec is None: done(code=1)
            labels, selector = spec['labels'], spec['selector']
            owner = labels.get('app.kubernetes.io/part-of', '<no value>')
            ports = ''.join(','.join(str(port.get(field, '<no value>')) for field in ('name','port','targetPort','protocol')) for port in spec['ports'])
            done('|'.join((obj.split('/')[-1],owner,labels.get('app.kubernetes.io/name','<no value>'),labels.get('app.kubernetes.io/component','<no value>'),str(len(labels)),spec.get('type','<no value>'),str(len(selector)),selector.get('app.kubernetes.io/name','<no value>'),selector.get('app.kubernetes.io/component','<no value>'),str(len(spec['ports'])),ports)))
    done(obj.split('/')[-1] + '|' + value)
if cmd in ('rollout', 'exec'):
    if cmd == 'exec' and s.get('exec_failures',0):
        s['exec_failures']-=1
        done(code=1)
    done(code=1 if s.get('not_ready') or (cmd == 'exec' and s.get('exec_error')) else 0)
if cmd == 'create' and '--dry-run=client' in args: done('namespace')
if cmd in ('apply', 'label', 'create', 'delete', 'set'):
    s['mutations'].append(args)
    if cmd in ('apply', 'delete') and '-f' in args:
        name = args[args.index('-f')+1]
        if name == '-': content = sys.stdin.read()
        else: content = pathlib.Path(name).read_text()
        # Identify actual manifest objects without a test-only YAML dependency.
        objects = []
        for doc in re.split(r'^---\s*$', content, flags=re.M):
            kind = re.search(r'^kind: (\S+)', doc, re.M)
            meta = re.search(r'^metadata:\n(.*?)(?=^\S|\Z)', doc, re.M|re.S)
            n = re.search(r'^  name: (\S+)', meta.group(1), re.M) if meta else None
            if kind and n:
                kinds = {'Deployment':'deploy','Service':'svc','Secret':'secret','PersistentVolumeClaim':'pvc','ConfigMap':'configmap','ClusterRole':'clusterrole','ClusterRoleBinding':'clusterrolebinding'}
                if kind.group(1) in kinds: objects.append(kinds[kind.group(1)]+'/'+n.group(1))
        for obj in objects:
            if cmd == 'delete': s['objects'].pop(obj, None)
            else: s['objects'][obj] = 'pacman'
    elif cmd == 'delete':
        kind = args[1].replace('deployment','deploy').replace('service','svc')
        for name in args[2:]:
            if name.startswith('-'): break
            for n in name.split(','): s['objects'].pop(kind+'/'+n, None)
    done()
done()
'''

def objects(mode):
    o = {'pvc/mongo-storage':'pacman', 'secret/mongodb-users-secret':'',
         'deploy/mongo':'pacman', 'svc/mongo':'pacman', 'configmap/mongo-init':'pacman',
         'clusterrole/pacman-clusterrole':'pacman',
         'clusterrolebinding/pacman-clusterrole-test':'pacman',
         'clusterrolebinding/pacman-clusterrole-other':'pacman'}
    if mode in ('legacy','mixed'): o.update({'deploy/pacman':'pacman','svc/pacman':'pacman'})
    if mode in ('split','mixed'):
        for role in ('web','score','user'):
            for kind in ('deploy','svc'): o[kind+'/pacman-'+role] = 'pacman'
    return o

def legacy_demo_inventory():
    objects = {}
    services = {}
    for role in ('web','score','user'):
        name = 'pacman-'+role
        objects['deploy/'+name] = 'pacman-cilium-demo'
        # Baseline rendered Services carry name/component labels but no part-of label.
        objects['svc/'+name] = '<no value>'
        services['svc/'+name] = {
            'labels': {'app.kubernetes.io/name':'pacman','app.kubernetes.io/component':role},
            'selector': {'app.kubernetes.io/name':'pacman','app.kubernetes.io/component':role},
            'type':'ClusterIP',
            'ports':[{'name':'http','port':8080,'targetPort':'http','protocol':'TCP'}],
        }
    return objects, services

failures = []
with tempfile.TemporaryDirectory() as t:
    p = pathlib.Path(t); (p/'kubectl').write_text(fake); (p/'kubectl').chmod(0o755)
    (p/'envsubst').write_text('#!/usr/bin/env python3\nimport os,sys\nsys.stdout.write(sys.stdin.read().replace("${NAMESPACE}",os.environ["NAMESPACE"]))\n')
    (p/'envsubst').chmod(0o755)
    (p/'sleep').write_text('#!/usr/bin/env bash\nexit 0\n'); (p/'sleep').chmod(0o755)
    def run(script, args=(), initial=None, **flags):
        state = {'objects':initial or {},'calls':[],'mutations':[],**flags}
        f=p/'state.json';f.write_text(json.dumps(state))
        env={**os.environ,'PATH':str(p)+os.pathsep+os.environ['PATH'],'FAKE_STATE':str(f),'NAMESPACE':'test'}
        r=subprocess.run(['bash',str(root/script),*args],env=env,cwd=root,capture_output=True,text=True)
        return r,json.loads(f.read_text())
    def check(name, condition):
        print(('PASS ' if condition else 'FAIL ')+name)
        if not condition: failures.append(name)
    for mode in ('legacy','split','mixed'):
        r,s=run('pacman-uninstall.sh',('keeppvc',),objects(mode))
        check(mode+' keeppvc removes union',r.returncode==0 and not any(k.startswith(('deploy/','svc/','configmap/')) for k in s['objects']))
        check(mode+' keeppvc preserves data credentials and shared RBAC',all(k in s['objects'] for k in ('pvc/mongo-storage','secret/mongodb-users-secret','clusterrole/pacman-clusterrole','clusterrolebinding/pacman-clusterrole-other')))
        check(mode+' removes only target binding','clusterrolebinding/pacman-clusterrole-test' not in s['objects'])
    # Explicit foreign namespace ownership must win over stale Pac-Man leftovers.
    for mode in ('full','keeppvc'):
        args = ('keeppvc',) if mode == 'keeppvc' else ()
        for leftover,resource in (('stale binding','clusterrolebinding/pacman-clusterrole-test'),
                                 ('retained PVC','pvc/mongo-storage')):
            r,s=run('pacman-uninstall.sh',args,{'namespace/test':'another-app',resource:'pacman'})
            check('foreign namespace with '+leftover+' rejects '+mode+' uninstall before writes',
                  r.returncode!=0 and not s['mutations'])
    baseline_objects,baseline_services=legacy_demo_inventory()
    for demo in ('microservices-east-west','load-simulation'):
        r,s=run('cilium-demos/scripts/deploy-demo.sh',(demo,),baseline_objects,service_specs=baseline_services)
        check('accepts exact baseline legacy Services for '+demo,
              r.returncode==0 and any(a[:2]==['apply','-k'] for a in s['mutations']))
    owned_objects,owned_services=legacy_demo_inventory()
    for role in ('web','score','user'): owned_objects['svc/pacman-'+role]='pacman-cilium-demo'
    r,s=run('cilium-demos/scripts/deploy-demo.sh',('microservices-east-west',),owned_objects,service_specs=owned_services)
    check('accepts currently owned canonical demo Services',r.returncode==0 and bool(s['mutations']))
    for label,change in (
        ('extra selector',lambda o,s:s['svc/pacman-web']['selector'].update({'environment':'prod'})),
        ('extra port',lambda o,s:s['svc/pacman-web']['ports'].append({'name':'admin','port':9090,'targetPort':9090,'protocol':'TCP'})),
        ('wrong target port',lambda o,s:s['svc/pacman-web']['ports'][0].update({'targetPort':8081})),
        ('missing corresponding Deployment',lambda o,s:o.pop('deploy/pacman-web')),
        ('non-demo corresponding Deployment',lambda o,s:o.update({'deploy/pacman-web':'pacman'})),
        ('explicit foreign Service owner',lambda o,s:o.update({'svc/pacman-web':'another-app'})),
        ('persistent base marker',lambda o,s:o.update({'pvc/mongo-storage':'pacman'})),
        ('foreign namespace owner',lambda o,s:o.update({'namespace/test':'another-app'})),
    ):
        bad_objects,bad_services=legacy_demo_inventory();change(bad_objects,bad_services)
        r,s=run('cilium-demos/scripts/deploy-demo.sh',('microservices-east-west',),bad_objects,service_specs=bad_services)
        check('rejects legacy demo with '+label+' before writes',r.returncode!=0 and not s['mutations'])
    r,s=run('cilium-demos/scripts/deploy-demo.sh',('microservices-east-west',),baseline_objects,service_specs=baseline_services,read_error_object='svc/pacman-web')
    check('rejects API read failure on legacy Service before writes',r.returncode!=0 and not s['mutations'])
    for script,args,initial,flags in [
        ('pacman-uninstall.sh',('keppvc',),objects('mixed'),{}),
        ('pacman-install.sh',('--unknown',),{},{}),
        ('pacman-install.sh',(),objects('split'),{'read_error':True}),
        ('pacman-uninstall.sh',(),objects('split'),{'read_error':True}),
        ('pacman-install.sh',(),{'pvc/mongo-storage':'pacman'},{}),
        ('pacman-install.sh',(),{'deploy/pacman-score':'pacman-cilium-demo'},{}),
        ('pacman-install.sh',(),{'namespace/test':'another-app'},{}),
        ('pacman-uninstall.sh',(),{'deploy/pacman-score':'pacman-cilium-demo'},{}),
        ('pacman-uninstall.sh',(),{'namespace/test':'unrelated'},{}),
        ('cilium-demos/scripts/deploy-demo.sh',('microservices-east-west',),objects('split'),{}),
        ('cilium-demos/scripts/deploy-demo.sh',('microservices-east-west',),{'namespace/test':'pacman'},{}),
        ('cilium-demos/scripts/deploy-demo.sh',('microservices-east-west',),{}, {'read_error':True}),
    ]:
        r,s=run(script,args,initial,**flags)
        check(script+' rejects '+str(args or flags or initial)+' before writes',r.returncode!=0 and not s['mutations'])
    for ready in (False,True):
        r,s=run('pacman-install.sh',('--retire-legacy',),objects('mixed'),not_ready=not ready)
        check('retirement '+('succeeds' if ready else 'failure preserves legacy'),
              (r.returncode==0 and 'deploy/pacman' not in s['objects'] and 'svc/pacman' not in s['objects']) if ready else
              (r.returncode!=0 and not s['mutations'] and 'deploy/pacman' in s['objects']))
    r,s=run('pacman-install.sh',('--retire-legacy',),objects('mixed'),exec_error=True)
    check('failed proxy probe preserves legacy',r.returncode!=0 and not s['mutations'] and 'deploy/pacman' in s['objects'] and 'svc/pacman' in s['objects'])
    r,s=run('pacman-install.sh',('--retire-legacy',),objects('mixed'),exec_failures=1)
    check('transient Service/proxy startup is retried',r.returncode==0 and 'deploy/pacman' not in s['objects'] and sum(a[0]=='exec' for a in s['calls'])==2)
    r,s=run('pacman-install.sh',(),objects('legacy'))
    check('prepare keeps legacy and existing Secret',r.returncode==0 and 'deploy/pacman' in s['objects'] and 'svc/pacman' in s['objects'] and not any(a[0]=='apply' and 'security/secret.yaml' in a for a in s['mutations']))
    r,s=run('pacman-uninstall.sh',(),objects('mixed'))
    check('full uninstall retains shared role and other binding',r.returncode==0 and all(k in s['objects'] for k in ('clusterrole/pacman-clusterrole','clusterrolebinding/pacman-clusterrole-other')) and any(a[:2]==['delete','namespace'] for a in s['mutations']))
    r,s=run('pacman-uninstall.sh',('keeppvc',),{})
    check('keeppvc absent namespace is repeatable',r.returncode==0)
print(str(len(failures))+' failing checks')
sys.exit(bool(failures))
PY
