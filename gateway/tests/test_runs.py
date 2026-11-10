from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from modal_bridge.app import create_app
from modal_bridge.runs import RunSpec, Runs

TOKEN = 'x' * 32
HEADERS = {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': 'run-1'}

class Backend:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.submit_error = False
        self.poll_error = False
    def submit(self, target, payload):
        self.calls.append(payload)
        if self.submit_error:
            raise ConnectionError('uncertain')
        call_id = f'fc-{len(self.calls)}'
        self.results[call_id] = {'status':'pending'}
        return call_id
    def poll(self, call_id):
        if self.poll_error:
            raise ConnectionError('offline')
        return self.results[call_id]


def setup(tmp_path, concurrency=2):
    backend = Backend()
    app = create_app(token=TOKEN,targets={'documents':('docs','embed')},db_path=str(tmp_path/'db'),backend=backend)
    spec = RunSpec(name='handbook',function='documents',revision='v1',max_in_flight=concurrency,
                   items=[{'key':'a','input':{'text':'hello'}},{'key':'b','input':{'text':'world'}}])
    return app.state.runs, backend, spec, TestClient(app)


def test_run_reuses_only_matching_successes(tmp_path):
    runs,b,spec,_ = setup(tmp_path)
    first = runs.create(spec,'first')
    runs.tick()
    assert len(b.calls)==2
    for key in b.results: b.results[key]={'status':'succeeded','result':{'embedding':[1,2]}}
    runs.tick()
    assert runs.get(first['id'])['status']=='completed'
    changed = spec.model_copy(deep=True)
    changed.items[1].input={'text':'new'}
    assert runs.preview(changed)['reusable']==1
    second = runs.create(changed,'second')
    runs.tick()
    assert len(b.calls)==3
    items=runs.items(second['id'])
    assert items[0]['state']=='reused' and items[0]['reused_from'].startswith(first['id'])
    assert items[0]['attempts']==[]
    changed.revision='v2'
    assert runs.preview(changed)['reusable']==0
    changed.revision='v1'; changed.namespace='another'
    assert runs.preview(changed)['reusable']==0


def test_concurrency_pause_and_restart(tmp_path):
    runs,b,spec,_=setup(tmp_path,concurrency=1)
    run=runs.create(spec,'first')
    runs.tick(); assert len(b.calls)==1
    runs.mode(run['id'],'paused')
    b.results['fc-1']={'status':'succeeded','result':None}
    restarted=Runs(runs.store,runs.targets,b)
    restarted.recover();restarted.tick()
    assert len(b.calls)==1
    assert restarted.get(run['id'])['counts']=={'queued':1,'succeeded':1}
    restarted.mode(run['id'],'active');restarted.tick()
    assert len(b.calls)==2
    assert restarted.items(run['id'])[0]['result'] is None


def test_failure_replay_keeps_success_and_history(tmp_path):
    runs,b,spec,client=setup(tmp_path)
    run=runs.create(spec,'first');runs.tick()
    b.results['fc-1']={'status':'succeeded','result':1}
    b.results['fc-2']={'status':'failed','error':'bad input'}
    runs.tick()
    assert runs.get(run['id'])['status']=='needs_attention'
    assert client.post(f'/v1/runs/{run["id"]}/retry-failed',headers=HEADERS,json={'keys':['a']}).status_code==409
    runs.retry(run['id'],None);runs.tick()
    assert len(b.calls)==3
    assert len(runs.items(run['id'])[1]['attempts'])==2
    assert runs.items(run['id'])[0]['attempt']==1


def test_uncertain_submission_is_never_repeated(tmp_path):
    runs,b,spec,client=setup(tmp_path,concurrency=1)
    b.submit_error=True
    run=runs.create(spec,'first');runs.tick();runs.tick()
    assert len(b.calls)==1
    assert runs.items(run['id'])[0]['state']=='submission_unknown'
    assert client.post(f'/v1/runs/{run["id"]}/retry-failed',headers=HEADERS,json={'keys':['a']}).status_code==409


def test_crash_after_reservation_is_quarantined(tmp_path):
    runs,b,spec,_=setup(tmp_path)
    run=runs.create(spec,'first')
    with runs.store.connect() as db:
        db.execute("UPDATE run_items SET state='submitting' WHERE run_id=? AND item_key='a'",(run['id'],))
    runs.recover();runs.tick()
    assert len(b.calls)==1 and b.calls[0]=={'text':'world'}
    assert runs.items(run['id'])[0]['state']=='submission_unknown'


def test_transport_failure_does_not_replay(tmp_path):
    runs,b,spec,_=setup(tmp_path)
    run=runs.create(spec,'first');runs.tick()
    b.poll_error=True
    runs.tick();runs.tick()
    assert len(b.calls)==2
    assert runs.get(run['id'])['counts']=={'pending':2}


def test_atomic_manifest_creation_and_conflicts(tmp_path):
    runs,b,spec,client=setup(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids=list(pool.map(lambda _:runs.create(spec,'same')['id'],range(4)))
    assert len(set(ids))==1
    changed=spec.model_dump();changed['revision']='v2'
    assert client.post('/v1/runs',headers={**HEADERS,'Idempotency-Key':'same'},json=changed).status_code==409
    duplicate=spec.model_dump();duplicate['items'][1]['key']='a'
    assert client.post('/v1/runs',headers=HEADERS,json=duplicate).status_code==422
    assert client.post('/v1/runs/preview',json=spec.model_dump()).status_code==401
    assert client.post('/v1/runs/preview',headers=HEADERS,json=spec.model_dump()).json()['to_compute']==2
    assert b.calls==[]


def test_changed_target_pauses_dispatch(tmp_path):
    runs,b,spec,_=setup(tmp_path)
    run=runs.create(spec,'first')
    runs.targets['documents']=('other','embed')
    runs.tick()
    assert not b.calls and runs.get(run['id'])['mode']=='paused'


def test_global_cap_includes_unknown_and_pending(tmp_path):
    runs,b,spec,_=setup(tmp_path)
    runs.create(spec,'one');runs.create(spec,'two')
    runs.tick(global_limit=1);runs.tick(global_limit=1)
    assert len(b.calls)==1
