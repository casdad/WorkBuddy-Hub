import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from hub.providers import WorkBuddyCN,ProviderError,micros,normalize_package,normalize_credential,timestamp,validate_endpoint
from hub.security import make_backup,restore_backup,Vault
from hub.store import Store
from .fakes import credential


async def setup(env,limit=None,uid='fixture-1'):
    hub,browser,upstream,folder=env
    r=await browser.post('/api/accounts',json={'name':'账号 '+uid,'credential':credential(uid)})
    assert r.status_code==200,r.text
    assert r.json()['sync']['ok'],r.text
    aid=r.json()['id']
    key=await browser.post('/api/keys',json={'name':'test','request_limit':limit})
    assert key.status_code==200,key.text
    return aid,key.json()['key'],key.json()['id']


def payload(stream=False):
    return {'model':'test-model','messages':[{'role':'user','content':'你好'}],'stream':stream}


async def chat(browser,key,body=None):
    return await browser.post('/v1/chat/completions',json=body or payload(),headers={'Authorization':'Bearer '+key})


async def test_oauth_and_encrypted_storage(env):
    hub,b,u,folder=env
    started=await b.post('/api/oauth/start',json={})
    assert started.status_code==200
    assert started.json()['qr'].startswith('data:image/png')
    polled=await b.post('/api/oauth/'+started.json()['login_id']+'/poll',json={})
    assert polled.json()['done'] is True
    second=await b.post('/api/oauth/'+started.json()['login_id']+'/poll',json={})
    assert second.json()['id']==polled.json()['id']
    assert len((await hub.public_accounts()))==1
    account=(await hub.account(polled.json()['id']))
    assert 'fixture-access' not in account['credential']
    assert hub.vault.decrypt(account['credential'])['auth']['accessToken'].startswith('fixture-access')
    assert 'fixture-access' not in (await b.get('/api/accounts')).text
    assert b'fixture-access' not in (folder/'hub.db').read_bytes()


async def test_sync_precision_cycle_and_models(env):
    hub,b,u,_=env
    aid,key,_=await setup(env)
    a=(await hub.account(aid))
    assert a['balance_micro']==123456789
    package=hub.store.one('SELECT * FROM packages WHERE account_id=?',(aid,))
    assert 2*86400<package['expires']-time.time()<4*86400
    listed=await b.get('/v1/models',headers={'Authorization':'Bearer '+key})
    assert listed.json()['data'][0]['id']=='test-model'
    summary=(await b.get('/api/summary')).json()
    assert summary['expiring_micro']==123456789


async def test_snapshot_retained_on_failed_sync(env):
    hub,b,u,_=env
    aid,_,_=await setup(env)
    before=(await hub.account(aid))['balance_at']
    u.credit_failure=True
    result=await b.post(f'/api/accounts/{aid}/sync',json={})
    assert result.json()['ok'] is False
    assert (await hub.account(aid))['balance_micro']==123456789
    assert (await hub.account(aid))['balance_at']==before
    assert len(hub.store.all('SELECT * FROM packages'))==1


async def test_singleflight_refresh_and_atomic_failure(env):
    hub,b,u,_=env
    aid=(await hub.add_account('refresh','workbuddy_cn',credential(expires=time.time()-30)))
    await asyncio.gather(*(hub.credentials(aid) for _ in range(20)))
    assert u.refreshes==1
    assert (await hub.account(aid))['version']==2
    before=(await hub.account(aid))['credential']
    u.partial_refresh=True
    with pytest.raises(ProviderError):
        await hub.credentials(aid,True)
    assert (await hub.account(aid))['credential']==before


@pytest.mark.parametrize('stream',[False,True])
async def test_chat_usage_and_ledger(env,stream):
    hub,b,u,_=env
    aid,key,kid=await setup(env,limit=2)
    result=await chat(b,key,payload(stream))
    assert result.status_code==200,result.text
    if stream:
        assert 'data: [DONE]' in result.text
    else:
        assert result.json()['choices'][0]['message']['content']=='连接成功。'
    row=hub.store.one('SELECT * FROM requests')
    assert row['state']=='success' and row['credits_micro']==12345
    assert row['input_tokens']==12 and row['output_tokens']==5
    assert row['ttft_ms'] is not None
    keyrow=hub.store.one('SELECT * FROM api_keys WHERE id=?',(kid,))
    assert keyrow['used']==1 and keyrow['reserved']==0
    assert hub.inflight[aid]==0
    assert u.last_stream.closed
    assert len(hub.store.all('SELECT * FROM ledger'))==1


async def test_unknown_usage_stays_null(env):
    hub,b,u,_=env
    _,key,_=await setup(env)
    u.usage=False
    assert (await chat(b,key)).status_code==200
    row=hub.store.one('SELECT * FROM requests')
    assert row['input_tokens'] is None and row['credits_micro'] is None
    assert row['usage_source']=='unknown'


async def test_budget_and_key_digest(env):
    hub,b,u,_=env
    _,key,kid=await setup(env,limit=1)
    assert (await chat(b,key)).status_code==200
    assert (await chat(b,key)).status_code==429
    assert u.chat_calls==1
    assert key not in (await b.get('/api/keys')).text
    assert key not in json.dumps(hub.store.all('SELECT * FROM api_keys'))


@pytest.mark.parametrize('mode,status,state',[('429',429,'cooldown'),('403',403,'reauth_required'),('500',500,'cooldown')])
async def test_errors_no_rotation_and_no_secret_leak(env,mode,status,state):
    hub,b,u,_=env
    aid,key,kid=await setup(env)
    u.mode=mode
    result=await chat(b,key)
    assert result.status_code==status
    assert 'must-not-leak' not in result.text
    assert u.chat_calls==1
    assert (await hub.account(aid))['state']==state
    if mode=='429':
        assert 120<(await hub.account(aid))['cooldown']-time.time()<=123
        assert hub.store.one('SELECT used FROM api_keys WHERE id=?',(kid,))['used']==0


async def test_explicit_401_refresh_once(env):
    hub,b,u,_=env
    _,key,_=await setup(env)
    u.mode='401-once'
    result=await chat(b,key)
    assert result.status_code==200
    assert u.chat_calls==2 and u.refreshes==1
    assert len(hub.store.all('SELECT * FROM requests'))==1
    assert len(hub.store.all('SELECT * FROM attempts'))==2


@pytest.mark.parametrize('stream',[False,True])
async def test_truncated_stream_never_retried(env,stream):
    hub,b,u,_=env
    aid,key,kid=await setup(env)
    u.mode='truncated'
    result=await chat(b,key,payload(stream))
    assert result.status_code==(200 if stream else 502)
    assert u.chat_calls==1
    row=hub.store.one('SELECT * FROM requests')
    assert row['state']=='interrupted'
    assert row['credits_micro'] is None
    assert hub.store.one('SELECT used FROM api_keys WHERE id=?',(kid,))['used']==1
    assert hub.inflight[aid]==0


async def test_pool_concurrency_cancel_release_and_maintenance(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    key=(await hub.key(raw))
    response=await hub.gateway(key,payload(True))
    iterator=response.body_iterator
    assert await anext(iterator)
    with pytest.raises(HTTPException) as err:
        await hub.gateway(key,payload())
    assert err.value.status_code==503
    assert (await b.post(f'/api/accounts/{aid}/sync',json={})).status_code==409
    await iterator.aclose()
    assert u.last_stream.closed
    assert hub.inflight[aid]==0 and hub.key_inflight[kid]==0
    assert hub.store.one('SELECT state FROM requests')['state']=='interrupted'


async def test_pools_model_alias_and_key_permissions(env):
    hub,b,u,_=env
    aid,key,kid=await setup(env)
    pool=await b.post('/api/pools',json={'name':'专用','strategy':'expiring','account_ids':[aid]})
    assert pool.status_code==200
    assert (await b.post('/api/models/aliases',json={'alias':'daily','model':'test-model'})).status_code==200
    restricted=(await b.post('/api/keys',json={'name':'restricted','pool_id':pool.json()['id'],'models':['daily']})).json()['key']
    assert (await chat(b,restricted)).status_code==403
    body={**payload(),'model':'daily'}
    result=await chat(b,restricted,body)
    assert result.status_code==200 and result.json()['model']=='daily'
    upstream_body=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert upstream_body['model']=='test-model'
    await b.patch(f'/api/accounts/{aid}',json={'enabled':False})
    assert (await chat(b,key)).status_code==503


async def test_session_affinity_and_expiry_priority(env):
    hub,b,u,_=env
    a,key,_=await setup(env,uid='one')
    second=(await hub.add_account('two','workbuddy_cn',credential('two')))
    await hub.sync(second)
    hub.store.execute("UPDATE pools SET strategy='expiring' WHERE id=1")
    hub.store.execute('UPDATE packages SET expires=? WHERE account_id=?',(time.time()+30,second))
    k=(await hub.key(key))
    selected=(await hub.acquire(k,'test-model','session',payload()))
    assert selected['id']==second
    hub.inflight[second]-=1
    assert (await hub.acquire(k,'test-model','session',payload()))['id']==second


@pytest.mark.parametrize('kind',['responses','messages'])
@pytest.mark.parametrize('stream',[False,True])
async def test_protocols(env,kind,stream):
    hub,b,u,_=env
    _,key,_=await setup(env)
    body={'model':'test-model','stream':stream}
    body.update({'input':'你好'} if kind=='responses' else {'messages':[{'role':'user','content':'你好'}],'max_tokens':100})
    result=await b.post('/v1/'+kind,json=body,headers={'Authorization':'Bearer '+key})
    assert result.status_code==200,result.text
    assert '连接成功' in result.text
    if stream:
        assert ('response.completed' if kind=='responses' else 'message_stop') in result.text
    else:
        assert ('output' if kind=='responses' else 'content') in result.json()


@pytest.mark.parametrize('kind',['chat/completions','responses','messages'])
async def test_tools_preserved(env,kind):
    hub,b,u,_=env
    _,key,_=await setup(env)
    if kind=='responses':
        body={'model':'test-model','input':'天气','tools':[{'type':'function','name':'weather','parameters':{'type':'object'}}]}
    elif kind=='messages':
        body={'model':'test-model','messages':[{'role':'user','content':'天气'}],'tools':[{'name':'weather','input_schema':{'type':'object'}}],'max_tokens':100}
    else:
        body={**payload(),'tools':[{'type':'function','function':{'name':'weather','parameters':{'type':'object'}}}]}
    body['tool_choice']=({'type':'function','name':'weather'} if kind=='responses' else
                         {'type':'tool','name':'weather'} if kind=='messages' else
                         {'type':'function','function':{'name':'weather'}})
    result=await b.post('/v1/'+kind,json=body,headers={'Authorization':'Bearer '+key})
    assert result.status_code==200,result.text
    assert 'weather' in result.text and '上海' in result.text
    forwarded=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert forwarded['tool_choice']=='weather'


async def test_developer_role_and_invalid_forced_tool(env):
    hub,b,u,_=env
    _,key,_=await setup(env)
    body=payload()
    body['messages'].insert(0,{'role':'developer','content':'只回复 OK'})
    assert (await chat(b,key,body)).status_code==200
    forwarded=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert forwarded['messages'][0]=={'role':'system','content':'只回复 OK'}
    for choice in ({'type':'function','function':{'name':'missing'}}, {'type':'allowed_tools'}, 1, []):
        assert (await chat(b,key,{**body,'tool_choice':choice})).status_code==400
    assert u.chat_calls==1


async def test_unsupported_fields_rejected_and_vision_preserved(env):
    hub,b,u,_=env
    _,key,_=await setup(env)
    headers={'Authorization':'Bearer '+key}
    for body in [{'model':'test-model','input':'hi','store':True},{'model':'test-model','input':'hi','previous_response_id':'x'},
                 {'model':'test-model','input':[{'role':'user','content':[{'type':'input_image','image_url':'https://example.com/a.png'}]}]}]:
        assert (await b.post('/v1/responses',json=body,headers=headers)).status_code==400
    body=payload()
    body['messages']=[{'role':'user','content':[{'type':'text','text':'图像'},{'type':'image_url','image_url':{'url':'https://example.com/a.png'}}]}]
    assert (await chat(b,key,body)).status_code==200
    forwarded=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert forwarded['messages']==body['messages']


async def test_csrf_origin_host_and_validation_redaction(env):
    hub,b,u,_=env
    assert (await b.post('/api/keys',json={'name':'x'},headers={'x-csrf-token':'wrong'})).status_code==403
    assert (await b.post('/api/keys',json={'name':'x'},headers={'Origin':'https://evil.example'})).status_code==403
    assert (await b.get('/healthz',headers={'Host':'evil.example'})).status_code==400
    result=await b.post('/api/accounts',json={'credential':{'accessToken':'never-echo-secret'}})
    assert result.status_code==422 and 'never-echo-secret' not in result.text
    result=await b.post('/api/login',content=b'x'*(2*1024*1024+1))
    assert result.status_code==413
    b.cookies.clear()
    b.cookies.set('hub_session','invalid')
    result=await b.get('/api/settings')
    assert result.status_code==401


async def test_key_expiry_disabled_rpm(env):
    hub,b,u,_=env
    _,raw,kid=await setup(env)
    hub.store.execute('UPDATE api_keys SET expires=? WHERE id=?',(time.time()-1,kid))
    assert (await chat(b,raw)).status_code==401
    hub.store.execute('UPDATE api_keys SET expires=NULL,enabled=0 WHERE id=?',(kid,))
    assert (await chat(b,raw)).status_code==401
    hub.store.execute('UPDATE api_keys SET enabled=1,rpm=1 WHERE id=?',(kid,))
    assert (await chat(b,raw)).status_code==200
    assert (await chat(b,raw)).status_code==429


async def test_backup_restore_and_restart_recovery(env,tmp_path):
    hub,b,u,folder=env
    aid,raw,kid=await setup(env)
    await chat(b,raw)
    backup=make_backup(hub.store,hub.vault,'portable-backup-password')
    assert b'fixture-access' not in backup and b'SQLite' not in backup
    target=folder/'restored'
    with pytest.raises(Exception):
        restore_backup(backup,'wrong-passphrase-123',target)
    assert not target.exists()
    restore_backup(backup,'portable-backup-password',target)
    restored=Store(target)
    vault=Vault(target)
    assert restored.one('SELECT COUNT(*) AS n FROM sessions')['n']==0
    assert vault.decrypt(restored.one('SELECT credential FROM accounts')['credential'])['account']['uid']=='fixture-1'
    assert restored.one('SELECT used FROM api_keys WHERE id=?',(kid,))['used']==1
    with pytest.raises(ValueError):
        restore_backup(backup,'portable-backup-password',target)
    # Simulate process death after reserving and dispatching a request.
    (await hub.reserve((await hub.key(raw)),'interrupted-fixture','test-model','test-model',aid))
    reopened=Store(folder)
    assert reopened.one('SELECT state FROM requests WHERE id=?',('interrupted-fixture',))['state']=='interrupted'
    assert reopened.one('SELECT used,reserved FROM api_keys WHERE id=?',(kid,))=={'used':2,'reserved':0}
    Store(folder)
    assert reopened.one('SELECT used FROM api_keys WHERE id=?',(kid,))['used']==2


async def test_logs_filter_csv_jobs_settings_and_password(env):
    hub,b,u,_=env
    aid,key,_=await setup(env)
    await chat(b,key)
    logs=await b.get('/api/logs',params={'account_id':aid,'state':'success','limit':1})
    assert len(logs.json())==1
    detail=await b.get('/api/logs/'+logs.json()[0]['id'])
    assert len(detail.json()['attempts'])==1 and detail.json()['ledger']['units']==1
    assert (await b.get('/api/logs/export')).headers['content-type'].startswith('text/csv')
    assert len((await b.get('/api/jobs')).json())>=2
    assert (await b.put('/api/settings',json={'sync_interval':120,'retention_days':7})).status_code==200
    assert hub.store.get('sync_interval')==120
    assert (await b.post('/api/password',json={'current':'test-password-123','password':'changed-password-123'})).status_code==200
    assert (await b.get('/api/session')).status_code==401
    assert (await b.post('/api/login',json={'password':'changed-password-123'})).status_code==200


@pytest.mark.parametrize('url',['http://example.com/v1','https://name:secret@example.com/v1','https://example.com/v1?key=x','file:///tmp/test'])
def test_endpoint_validation(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


@pytest.mark.parametrize('value,expected',[('0.000001',1),('123.456789',123456789),(None,None),('NaN',None),(-1,None),(False,None)])
def test_credit_precision(value,expected):
    assert micros(value)==expected


def test_time_and_credential_validation():
    assert timestamp('2026-10-26 00:00:00')==timestamp('2026-10-25T16:00:00Z')
    assert timestamp(1793000000000)==1793000000
    assert normalize_package({'CapacityRemain':100,'CycleCapacityRemain':0})['remaining_micro']==0
    with pytest.raises(ValueError):
        normalize_credential({'auth':{'accessToken':{'encrypted':True}}})


async def test_scheduler_sync_and_cleanup(env):
    hub,b,u,_=env
    aid=(await hub.add_account('scheduled','workbuddy_cn',credential()))
    hub.store.execute("INSERT INTO jobs(created,kind,state) VALUES(?,'old','success')",(time.time()-100*86400,))
    task=asyncio.create_task(hub.scheduler())
    for _ in range(100):
        if (await hub.account(aid))['balance_at'] and not await hub.store.aone("SELECT id FROM jobs WHERE kind='old'"):
            break
        await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await hub.account(aid))['balance_micro']==123456789
    assert not hub.store.one("SELECT * FROM jobs WHERE kind='old'")


async def test_fallback_pool_and_cycle_rejection(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    backup=(await b.post('/api/pools',json={'name':'备用','account_ids':[aid]})).json()['id']
    assert (await b.put('/api/pools/1',json={'name':'默认账号池','account_ids':[],'fallback_pool_id':backup})).status_code==200
    assert 'test-model' in (await hub.key_models((await hub.key(raw))))
    assert (await chat(b,raw)).status_code==200
    assert (await b.put(f'/api/pools/{backup}',json={'name':'备用','account_ids':[aid],'fallback_pool_id':1})).status_code==400


async def test_reasoning_effort_never_pre_emptively_downgraded(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    # The upstream catalog declares the rungs; sync must keep them for the fallback.
    stored=json.loads((await hub.store.aone('SELECT capabilities FROM models WHERE account_id=? AND model=?',(aid,'test-model')))['capabilities'])
    assert stored['efforts']==['low','medium','high']
    assert stored['default_effort']=='high'
    listed=(await b.get('/api/models')).json()['models']
    assert json.loads(listed[0]['capabilities'])['efforts']==['low','medium','high']
    # A catalog may under-report, so a supported-but-undeclared rung must reach the
    # upstream untouched: "max" is not rewritten to "high" before the first attempt.
    for effort in ('max','xhigh','medium','minimal','ultra','off'):
        assert (await chat(b,raw,{**payload(),'reasoning_effort':effort})).status_code==200
        sent=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
        assert sent['reasoning_effort']==effort


async def test_reasoning_effort_falls_back_once_on_invalid_effort(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    u.mode='effort-reject'
    result=await chat(b,raw,{**payload(),'reasoning_effort':'max'})
    assert result.status_code==200
    # One rejected attempt, then one coordinated downgrade on the same account.
    assert u.chat_calls==2
    sent=[json.loads(r.content) for r in u.requests if r.url.path.endswith('/chat/completions')]
    assert [body['reasoning_effort'] for body in sent]==['max','high']
    row=await hub.store.aone('SELECT * FROM requests')
    assert row['state']=='success' and row['settled']==1
    attempts=await hub.store.aall('SELECT status FROM attempts WHERE request_id=? ORDER BY id',(row['id'],))
    assert [a['status'] for a in attempts]==[400,200]
    assert (await hub.store.aone('SELECT used,reserved FROM api_keys WHERE id=?',(kid,)))=={'used':1,'reserved':0}
    assert (await hub.store.aone('SELECT COUNT(*) AS n FROM ledger'))['n']==1


async def test_pool_deletion_guards_and_fallback_detach(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    spare=(await b.post('/api/pools',json={'name':'备用','account_ids':[aid]})).json()['id']
    assert (await b.put('/api/pools/1',json={'name':'默认账号池','account_ids':[],'fallback_pool_id':spare})).status_code==200
    # New accounts and keys default to pool 1, so it is never removable.
    assert (await b.delete('/api/pools/1')).status_code==400
    assert (await b.delete('/api/pools/9999')).status_code==404
    # A pool still pointed at by a key is protected instead of cascading away.
    pinned=(await b.post('/api/keys',json={'name':'pinned','pool_id':spare})).json()['id']
    blocked=await b.delete(f'/api/pools/{spare}')
    assert blocked.status_code==409 and '1 个 API Key' in blocked.text
    assert (await hub.store.aone('SELECT id FROM pools WHERE id=?',(spare,)))
    await hub.store.aexecute('UPDATE api_keys SET pool_id=1 WHERE id=?',(pinned,))
    # Removing the pool drops its members and clears incoming fallback links.
    removed=await b.delete(f'/api/pools/{spare}')
    assert removed.status_code==200 and removed.json()['detached_fallbacks']==1
    assert not (await hub.store.aone('SELECT id FROM pools WHERE id=?',(spare,)))
    assert (await hub.store.aone('SELECT fallback_pool_id FROM pools WHERE id=1'))['fallback_pool_id'] is None
    assert not (await hub.store.aone('SELECT * FROM pool_members WHERE pool_id=?',(spare,)))
    assert (await hub.store.aone("SELECT target FROM audit WHERE action='pool_deleted'"))['target']==str(spare)
    # The account itself survives; only its pool membership was removed.
    assert (await hub.store.aone('SELECT id FROM accounts WHERE id=?',(aid,)))
    await hub.store.aexecute('INSERT OR IGNORE INTO pool_members VALUES(1,?)',(aid,))
    assert (await chat(b,raw)).status_code==200


async def test_key_model_scope_is_editable_and_enforced(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    # Unknown names are rejected instead of silently creating a Key that can call
    # nothing at all.
    assert (await b.post('/api/keys',json={'name':'typo','models':['no-such-model']})).status_code==400
    # Restricting to one model: the catalog shrinks and everything else is refused.
    assert (await b.patch(f'/api/keys/{kid}',json={'models':['test-model']})).status_code==200
    listed=await b.get('/v1/models',headers={'Authorization':'Bearer '+raw})
    assert [m['id'] for m in listed.json()['data']]==['test-model']
    assert (await chat(b,raw,{**payload(),'model':'other-model'})).status_code==403
    assert (await chat(b,raw)).status_code==200
    # Clearing the scope restores the pool-wide default instead of locking it down.
    assert (await b.patch(f'/api/keys/{kid}',json={'models':[]})).status_code==200
    assert (await hub.key(raw))['models']=='[]'
    # enabled and models are independent fields.
    assert (await b.patch(f'/api/keys/{kid}',json={'enabled':False})).status_code==200
    assert (await chat(b,raw)).status_code==401
    assert (await b.patch(f'/api/keys/{kid}',json={})).status_code==400


async def test_key_deletion_revokes_and_detaches_history(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    assert (await chat(b,raw)).status_code==200
    assert (await b.delete('/api/keys/9999')).status_code==404
    assert (await b.delete(f'/api/keys/{kid}')).status_code==200
    assert not (await hub.store.aone('SELECT id FROM api_keys WHERE id=?',(kid,)))
    # The log and ledger outlive the key; only the attribution is detached.
    assert (await hub.store.aone('SELECT COUNT(*) AS n FROM requests'))['n']==1
    assert (await hub.store.aone('SELECT key_id FROM requests'))['key_id'] is None
    assert (await hub.store.aone('SELECT key_id FROM ledger'))['key_id'] is None
    assert (await hub.store.aone("SELECT target FROM audit WHERE action='key_deleted'"))['target']=='test'
    # The revoked key can no longer authenticate, and its name is reusable.
    assert (await chat(b,raw)).status_code==401
    assert u.chat_calls==1
    assert (await b.post('/api/keys',json={'name':'test'})).status_code==200


async def test_key_deletion_blocked_while_request_in_flight(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    key=await hub.key(raw)
    await hub.reserve(key,'rid-inflight','test-model','test-model',aid)
    blocked=await b.delete(f'/api/keys/{kid}')
    assert blocked.status_code==409 and '进行中' in blocked.text
    assert (await hub.store.aone('SELECT id FROM api_keys WHERE id=?',(kid,)))
    await hub.settle('rid-inflight',key,aid,'failed',499,time.monotonic(),{},None,'test',charge=0)
    assert (await b.delete(f'/api/keys/{kid}')).status_code==200


async def test_key_secret_can_be_revealed_and_rotated(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    assert (await b.get(f'/api/keys/{kid}/secret')).json()['key']==raw
    # The list endpoint must never carry the plaintext.
    assert raw not in (await b.get('/api/keys')).text
    # Keys created before schema 3 have no stored secret and must say so.
    await hub.store.aexecute('UPDATE api_keys SET secret=NULL WHERE id=?',(kid,))
    missing=await b.get(f'/api/keys/{kid}/secret')
    assert missing.status_code==409 and '重新生成' in missing.text
    rotated=(await b.post(f'/api/keys/{kid}/rotate')).json()['key']
    assert rotated!=raw and rotated.startswith('wbh_')
    assert (await b.get(f'/api/keys/{kid}/secret')).json()['key']==rotated
    assert (await chat(b,raw)).status_code==401
    assert (await chat(b,rotated)).status_code==200
    actions={row['action'] for row in (await hub.store.aall('SELECT action FROM audit'))}
    assert {'key_secret_viewed','key_rotated'} <= actions


async def test_usage_dashboard_aggregates_by_model_and_endpoint(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    assert (await chat(b,raw)).status_code==200
    assert (await b.post('/v1/responses',json={'model':'test-model','input':'你好'},
                         headers={'Authorization':'Bearer '+raw})).status_code==200
    data=(await b.get('/api/usage',params={'hours':24})).json()
    assert data['requests']==2 and data['totals']['successes']==2
    assert data['totals']['input_tokens']==24 and data['totals']['output_tokens']==10
    assert data['totals']['tokens_total']==34
    assert data['totals']['credits_micro']==24690
    assert {row['name'] for row in data['by_endpoint']}=={'chat','responses'}
    assert data['by_model'][0]['name']=='test-model' and data['by_model'][0]['requests']==2
    assert len(data['series'])==1
    assert data['totals']['cache_hit_rate'] is None


async def test_cache_tokens_recorded_and_aggregated(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    key=await hub.key(raw)
    await hub.reserve(key,'rid-cache','test-model','test-model',aid,'chat')
    await hub.settle('rid-cache',key,aid,'success',200,time.monotonic(),
                     {'prompt_tokens':100,'completion_tokens':10,
                      'prompt_tokens_details':{'cached_tokens':80}},5)
    row=await hub.store.aone('SELECT * FROM requests WHERE id=?',('rid-cache',))
    assert row['cache_tokens']==80 and row['endpoint']=='chat'
    data=(await b.get('/api/usage')).json()
    assert data['totals']['cache_tokens']==80
    assert data['totals']['cache_hit_rate']==0.8


async def test_store_and_metadata_accepted_but_never_forwarded(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    # OpenAI-compatible clients send these by default; rejecting them broke real clients.
    assert (await chat(b,raw,{**payload(),'store':False,'metadata':{'trace':'abc'}})).status_code==200
    sent=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert 'store' not in sent and 'metadata' not in sent
    # Asking the server to keep the conversation is refused rather than faked.
    refused=await chat(b,raw,{**payload(),'store':True})
    assert refused.status_code==400 and 'store=true' in refused.text
    assert (await chat(b,raw,{**payload(),'store':'yes'})).status_code==400
    assert (await chat(b,raw,{**payload(),'metadata':[1,2]})).status_code==400
    assert u.chat_calls==1


async def test_cn_connector_forwards_only_supported_fields(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    # The CN endpoint answers unknown fields with 400, so the connector filters.
    assert (await chat(b,raw,{**payload(),'parallel_tool_calls':True,'temperature':0.2})).status_code==200
    sent=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert 'parallel_tool_calls' not in sent
    assert sent['temperature']==0.2
    assert sent['stream'] is True and sent['stream_options']=={'include_usage':True}
    assert 'store' not in sent and 'metadata' not in sent


async def test_upstream_error_detail_is_recorded_and_surfaced(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    u.mode='unknown-field'
    result=await chat(b,raw)
    assert result.status_code==400
    assert '11001' in result.text
    # A 400 is retried once (nothing was generated), so the detail lands on the
    # last attempt rather than the first.
    assert u.chat_calls==2
    row=await hub.store.aone('SELECT * FROM requests')
    assert row['error']=='upstream_http_400' and row['settled']==1
    attempt=await hub.store.aone('SELECT * FROM attempts WHERE request_id=? ORDER BY id DESC LIMIT 1',(row['id'],))
    assert '11001' in (attempt['error'] or '')
    assert 'invalid request body' in (attempt['error'] or '')


async def test_cn_normalises_tool_history_and_empty_content(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    # Orphan call + empty content: the upstream answers 11148 / 11151 and the
    # client replays that history every turn, so the gateway must repair it.
    body={'model':'test-model','messages':[
        {'role':'developer','content':'be brief'},
        {'role':'user','content':'天气?'},
        {'role':'assistant','content':'',
         'tool_calls':[{'id':'call_x','type':'function','function':{'name':'weather','arguments':'{}'}}]},
        {'role':'assistant','content':''},
    ]}
    assert (await chat(b,raw,body)).status_code==200
    sent=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert [m['role'] for m in sent['messages']]==['system','user','assistant','assistant']
    assert 'tool_calls' not in sent['messages'][2]
    assert sent['messages'][2]['content']==' '
    assert sent['messages'][3]['content']==' '
    # A properly paired round trip is preserved untouched.
    paired={'model':'test-model','messages':[
        {'role':'user','content':'天气?'},
        {'role':'assistant','content':None,
         'tool_calls':[{'id':'call_ok','type':'function','function':{'name':'weather','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'call_ok','content':'晴'}]}
    assert (await chat(b,raw,paired)).status_code==200
    sent=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert sent['messages'][1]['tool_calls'][0]['id']=='call_ok'
    assert sent['messages'][1]['content'] is None
    assert sent['messages'][2]['content']=='晴'

    dsh={'model':'test-model','messages':[
        {'role':'system','content':'You are an AI agent powered by DeepSeek Harness.'},
        {'role':'user','content':'ping'}]}
    assert (await chat(b,raw,dsh)).status_code==200
    sent=json.loads([r for r in u.requests if r.url.path.endswith('/chat/completions')][-1].content)
    assert [m['role'] for m in sent['messages']]==['user','user']


async def test_transient_channel_block_is_retried_once(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    # Risk control blocks intermittently; the identical payload usually succeeds.
    u.channel_blocks=1
    assert (await chat(b,raw)).status_code==200
    assert u.chat_calls==2
    row=await hub.store.aone('SELECT * FROM requests')
    assert row['state']=='success' and row['settled']==1
    attempts=await hub.store.aall('SELECT status FROM attempts WHERE request_id=? ORDER BY id',(row['id'],))
    assert [a['status'] for a in attempts]==[400,200]
    assert (await hub.store.aone('SELECT used,reserved FROM api_keys WHERE id=?',(kid,)))=={'used':1,'reserved':0}


async def test_persistent_channel_block_surfaces_upstream_code(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    u.channel_blocks=2
    result=await chat(b,raw)
    assert result.status_code==400 and '11-128' in result.text
    assert u.chat_calls==2
    row=await hub.store.aone('SELECT * FROM requests')
    assert row['state']=='failed' and row['error']=='upstream_http_400'
    assert (await hub.store.aone('SELECT used FROM api_keys WHERE id=?',(kid,)))['used']==0


async def test_persistent_channel_block_falls_back_to_other_account(env):
    hub,b,u,_=env
    first,raw,kid=await setup(env)
    second=await hub.add_account('账号 two','workbuddy_cn',credential('two'))
    await hub.sync(second)
    from hub.security import digest
    hub.sticky[(kid,digest('fallback-session'))]=(first,time.time()+3600)
    u.channel_blocks=2
    result=await hub.gateway(await hub.key(raw),payload(),session='fallback-session')
    assert result.status_code==200
    attempts=await hub.store.aall('SELECT account_id,status FROM attempts ORDER BY id')
    assert [(row['account_id'],row['status']) for row in attempts]==[(first,400),(first,400),(second,200)]
    row=await hub.store.aone('SELECT account_id,state,settled FROM requests')
    assert row=={'account_id':second,'state':'success','settled':1}
    assert (await hub.store.aone('SELECT used,reserved FROM api_keys WHERE id=?',(kid,)))=={'used':1,'reserved':0}


async def test_disconnect_before_first_chunk_cleanup(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    response=await hub.gateway((await hub.key(raw)),payload(True))
    assert hub.inflight[aid]==1
    await response.finalize()
    await response.finalize()
    assert hub.inflight[aid]==0 and hub.key_inflight[kid]==0
    assert u.last_stream.closed
    assert hub.store.one('SELECT used FROM api_keys WHERE id=?',(kid,))['used']==1
