#!/usr/bin/env bash
# Destructive only in the explicitly named disposable kind context.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${KUBECONFIG:?Supply the isolated disposable-cluster kubeconfig}"
[[ "${PACMAN_TEST_CONTEXT:-}" == kind-pacman-role-integration ]] || {
  echo 'PACMAN_TEST_CONTEXT must be kind-pacman-role-integration' >&2; exit 1;
}
exec python3 - <<'PY'
import base64, hashlib, json, os, pathlib, re, socket, subprocess, tempfile, time, urllib.request, uuid

root = pathlib.Path.cwd()
context = os.environ['PACMAN_TEST_CONTEXT']
base = ['kubectl', '--context='+context, '--request-timeout=30s']
baseline = '503b87cb4fadd6dd392b06546aec0939a9f10f26'

def run(args, data=None, env=None, timeout=240):
    r = subprocess.run(args, input=data, text=True, capture_output=True,
                       env=env, timeout=timeout)
    if r.returncode:
        # Do not include stdin, Secret JSON or credential-bearing commands.
        raise AssertionError(r.stderr or r.stdout or 'command failed')
    return r.stdout

def k(*args, data=None): return run(base+list(args), data)
def obj(ns, kind, name):
    out=k('-n', ns, 'get', kind+'/'+name, '--ignore-not-found', '-o', 'json')
    return json.loads(out) if out.strip() else None
def apply(ns, value): k('-n',ns,'apply','-f','-',data=json.dumps(value))
def install(ns, *args):
    return run(['bash','pacman-install.sh',*args],env={**os.environ,'NAMESPACE':ns})
def uninstall(ns, *args):
    return run(['bash','pacman-uninstall.sh',*args],env={**os.environ,'NAMESPACE':ns})
def ready(ns, *names):
    for name in names: k('-n',ns,'rollout','status','deployment/'+name,'--timeout=180s')
def mark(text): print('PASS '+text, flush=True)
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def identity(ns):
    pvc=obj(ns,'pvc','mongo-storage'); secret=obj(ns,'secret','mongodb-users-secret')
    return [pvc['metadata']['uid'],pvc['spec']['volumeName'],secret['metadata']['uid'],digest(secret['data'])]
def specs(ns):
    return {kind+'/'+name:obj(ns,kind,name)['spec'] for kind in ['deploy','svc']
            for name in ['pacman-web','pacman-score','pacman-user']}

assert run(['kubectl','config','current-context']).strip()==context
server=run(['kubectl','config','view','--minify','-o','jsonpath={.clusters[0].cluster.server}'])
assert server.startswith('https://127.0.0.1:'), 'Expected a local disposable kind API'
nodes=json.loads(k('get','nodes','-o','json'))['items']
assert nodes and all(n['status']['nodeInfo']['architecture']=='arm64' and
                     n['metadata']['name'].startswith('pacman-role-integration-') for n in nodes)
mark('isolated ARM64 kind context verified')

def seed(ns, service='pacman-web', port=8080):
    name='lifecycle-'+uuid.uuid4().hex[:10]
    script='''(async()=>{
      const base=BASE;
      const req=async(path,body)=>{const r=await fetch(base+path,{...(body?{method:'POST',body:new URLSearchParams(body)}:{}),signal:AbortSignal.timeout(5000)});if(!r.ok)throw Error(path+': '+r.status);return r.json()};
      for(let attempt=0;;attempt++){try{await req('/readyz');break}catch(e){if(attempt===29)throw e;await new Promise(r=>setTimeout(r,500))}}
      await req('/highscores',{name:NAME,score:'500',level:'1'});
      const userId=await req('/user/id?name='+NAME);
      await req('/user/stats',{userId,name:NAME,score:'77',level:'1',lives:'3',elapsedTime:'42'});
      const scores=await req('/highscores/list');if(!scores.some(x=>x.name===NAME))throw Error('missing score');
      console.log(JSON.stringify({name:NAME,userId}));
    })().catch(e=>{console.error(e.message);process.exit(1)});'''.replace('BASE',json.dumps('http://'+service+':'+str(port))).replace('NAME',json.dumps(name))
    deployment='pacman' if service=='pacman' else 'pacman-web'
    return json.loads(k('-n',ns,'exec','deployment/'+deployment,'--','node','-e',script))

def stored(ns, record):
    expr='JSON.stringify({scores:db.highscore.find({name:'+json.dumps(record['name'])+'}).toArray(),users:db.userstats.find({_id:ObjectId('+json.dumps(record['userId'])+')}).toArray()})'
    out=k('-n',ns,'exec','deployment/mongo','--','sh','-c',
          'mongosh --quiet --username "$APP_DB_USER" --password "$APP_DB_PWD" --authenticationDatabase "$APP_DB_NAME" "$APP_DB_NAME" --eval "$1"', 'sh',expr)
    value=json.loads(out.strip()); assert len(value['scores'])==1 and len(value['users'])==1
    assert value['scores'][0]['score']==500 and value['users'][0]['score']==77
    return value

def http_access(ns, service='pacman-web', port=8080):
    with socket.socket() as s: s.bind(('127.0.0.1',0)); local=s.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        p=subprocess.Popen(base+['-n',ns,'port-forward','svc/'+service,str(local)+':'+str(port)],stdout=log,stderr=log)
        try:
            for attempt in range(60):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:'+str(local)+'/',timeout=2) as res: assert res.status==200
                    break
                except OSError:
                    if p.poll() is not None: raise AssertionError('port-forward exited')
                    time.sleep(.25)
            else: raise AssertionError('port-forward unavailable')
            for path in ['/pacman-canvas.js','/highscores/list','/user/stats']:
                with urllib.request.urlopen('http://127.0.0.1:'+str(local)+path,timeout=10) as res: assert res.status==200
        finally:
            p.terminate();p.wait(timeout=10)

def legacy_fixture(ns):
    k('create','namespace',ns)
    # Custom credentials exercise reuse without logging their values.
    values={'database-name':'pacman','database-user':'app'+uuid.uuid4().hex[:8],
            'database-password':uuid.uuid4().hex,'database-admin-name':'admin'+uuid.uuid4().hex[:8],
            'database-admin-password':uuid.uuid4().hex}
    apply(ns,{'apiVersion':'v1','kind':'Secret','metadata':{'name':'mongodb-users-secret'},
              'data':{key:base64.b64encode(v.encode()).decode() for key,v in values.items()}})
    env={**os.environ,'NAMESPACE':ns}
    rbac=run(['envsubst','${NAMESPACE}'],(root/'security/rbac.yaml').read_text(),env)
    k('apply','-f','-',data=rbac)
    for f in ['persistentvolumeclaim/mongo-pvc.yaml','deployments/mongo-deployment.yaml','services/mongo-service.yaml']:
        k('-n',ns,'apply','-f',f)
    ready(ns,'mongo')
    for f in ['deployments/pacman-deployment.yaml','services/pacman-service.yaml']:
        k('-n',ns,'apply','-f','-',data=run(['git','show',baseline+':'+f]))
    ready(ns,'pacman')

# Default and custom fresh installs, repeatability and stable credentials/PVC.
install('pacman-demo'); first=identity('pacman-demo')
install('pacman-demo'); assert identity('pacman-demo')==first
http_access('pacman-demo')
record=seed('pacman-demo'); data=stored('pacman-demo',record)
k('-n','pacman-demo','rollout','restart','deployment/pacman-web','deployment/pacman-score','deployment/pacman-user','deployment/mongo')
ready('pacman-demo','mongo','pacman-score','pacman-user','pacman-web')
assert stored('pacman-demo',record)==data
mark('fresh/repeated default install, port-forward and role/Mongo restart persistence')

# Render only Kubernetes-native demo objects; no Cilium/BPF installation.
demo='pacman-east-west'; before=specs('pacman-demo')
apply(demo,{'apiVersion':'v1','kind':'Namespace','metadata':{'name':demo}})
with tempfile.TemporaryDirectory(dir=root/'cilium-demos') as d:
    path=pathlib.Path(d)
    (path/'workloads.yaml').write_text((root/'cilium-demos/microservices-east-west/workloads.yaml').read_text())
    (path/'kustomization.yaml').write_text('apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: '+demo+'\nlabels:\n  - pairs:\n      app.kubernetes.io/part-of: pacman-cilium-demo\n    includeSelectors: false\nresources:\n  - workloads.yaml\n')
    k('apply','-k',str(path))
ready(demo,'pacman-score','pacman-user','pacman-web')
for role in ['score','user']:
    env=obj(demo,'deploy','pacman-'+role)['spec']['template']['spec']['containers'][0]['env']
    assert next(v['value'] for v in env if v['name']=='DB_TYPE')=='memory'
r=subprocess.run(['bash','cilium-demos/scripts/deploy-demo.sh','microservices-east-west'],env={**os.environ,'NAMESPACE':'pacman-demo'},capture_output=True,text=True)
assert r.returncode!=0
inverse=subprocess.run(['bash','pacman-install.sh'],env={**os.environ,'NAMESPACE':demo},capture_output=True,text=True)
assert inverse.returncode!=0
assert specs('pacman-demo')==before and identity('pacman-demo')==first and stored('pacman-demo',record)==data
mark('isolated memory demo, both collision directions, base specs/data unchanged')

for mode in ['legacy','split','mixed']:
    ns='lifecycle-'+mode
    if mode=='split': install(ns)
    else: legacy_fixture(ns)
    original=identity(ns)
    record=seed(ns,'pacman' if mode!='split' else 'pacman-web',80 if mode!='split' else 8080)
    data=stored(ns,record)
    if mode!='split':
        http_access(ns,'pacman',80)
        install(ns)
        assert obj(ns,'deploy','pacman') and obj(ns,'svc','pacman')
        assert identity(ns)==original and stored(ns,record)==data
        # Invalid new Service routing must block retirement and leave legacy usable.
        k('-n',ns,'patch','svc/pacman-web','--type=merge','-p',json.dumps({'spec':{'selector':{'app':'no-such-web'}}}))
        failed=subprocess.run(['bash','pacman-install.sh','--retire-legacy'],env={**os.environ,'NAMESPACE':ns},capture_output=True,text=True,timeout=100)
        assert failed.returncode!=0 and obj(ns,'deploy','pacman') and obj(ns,'svc','pacman')
        http_access(ns,'pacman',80)
        k('-n',ns,'apply','-f','services/pacman-service.yaml');http_access(ns)
        install(ns,'--retire-legacy');install(ns,'--retire-legacy')
        assert not obj(ns,'deploy','pacman') and not obj(ns,'svc','pacman')
        assert stored(ns,record)==data
        mark(mode+' additive upgrade, failed-routing preservation and repeated explicit retirement')
    # Exercise keeppvc from each exact shape, including mixed resources.
    if mode in ['legacy','mixed']:
        for f in ['deployments/pacman-deployment.yaml','services/pacman-service.yaml']:
            k('-n',ns,'apply','-f','-',data=run(['git','show',baseline+':'+f]))
        ready(ns,'pacman')
    if mode=='legacy':
        k('-n',ns,'delete','deployment','pacman-web','pacman-score','pacman-user','--cascade=foreground','--wait=true')
        k('-n',ns,'delete','service','pacman-web','pacman-score','pacman-user')
    uninstall(ns,'keeppvc');uninstall(ns,'keeppvc')
    assert identity(ns)==original
    for kind in ['deploy','svc']:
        for name in ['pacman','pacman-web','pacman-score','pacman-user','mongo']: assert not obj(ns,kind,name)
    assert not obj(ns,'configmap','mongo-init')
    assert obj(ns,'clusterrole','pacman-clusterrole')
    assert obj('pacman-demo','clusterrolebinding','pacman-clusterrole-pacman-demo')
    install(ns);assert identity(ns)==original and stored(ns,record)==data
    mark(mode+' keeppvc twice, stable PVC/credentials and data recovery')
    # Full uninstall also covers the union of legacy and split resources.
    if mode in ['legacy','mixed']:
        for f in ['deployments/pacman-deployment.yaml','services/pacman-service.yaml']:
            k('-n',ns,'apply','-f','-',data=run(['git','show',baseline+':'+f]))
    if mode=='legacy':
        k('-n',ns,'delete','deployment','pacman-web','pacman-score','pacman-user','--cascade=foreground','--wait=true')
        k('-n',ns,'delete','service','pacman-web','pacman-score','pacman-user')
    uninstall(ns);uninstall(ns)
    assert not obj(ns,'namespace',ns) and not obj(ns,'pvc','mongo-storage')
    assert not obj(ns,'clusterrolebinding','pacman-clusterrole-'+ns)
    assert obj(ns,'clusterrole','pacman-clusterrole') and obj(demo,'deploy','pacman-score')
    assert k('auth','can-i','list','pods','--as=system:serviceaccount:pacman-demo:default').strip()=='yes'
    ready('pacman-demo','mongo','pacman-web','pacman-score','pacman-user')
    mark(mode+' full uninstall twice; other base permissions and isolated demo intact')

mark('portable lifecycle suite complete; A5 external LoadBalancer and native amd64 BPF remain pending/deferred')
PY
