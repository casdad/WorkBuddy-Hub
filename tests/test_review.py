"""Regression evidence for the independently checked external review."""
import asyncio
import json
import sqlite3
import threading
import time

import httpx
import pytest

from hub.api import BoundedBody, create_app
from hub.providers import WorkBuddyCN, ProviderError, normalize_credential
from hub.security import password_ok
from hub.store import Store, SCHEMA
from .fakes import credential, Stream
from .test_hub import setup, payload, chat


async def wait_event(event):
    for _ in range(300):
        if event.is_set():
            return
        await asyncio.sleep(.01)
    raise AssertionError('Worker did not reach expected checkpoint')


async def test_locked_database_does_not_stop_stream_or_event_loop(env):
    hub,b,u,_=env
    aid,raw,_=await setup(env)
    await hub.store.aexecute('UPDATE accounts SET max_concurrency=2 WHERE id=?',(aid,))
    key=await hub.key(raw)
    response=await hub.gateway(key,payload(True))
    iterator=response.body_iterator
    assert await anext(iterator)
    blocker=hub.store.connect()
    blocker.execute('BEGIN IMMEDIATE')
    pending=asyncio.create_task(hub.gateway(key,payload()))
    try:
        # One new request waits for the real SQLite write lock while the first
        # request must continue emitting and the asyncio clock must advance.
        started=time.monotonic()
        ticks=0
        while time.monotonic()-started<.25:
            ticks+=1
            await asyncio.sleep(.01)
        assert ticks>=8
        assert not pending.done()
        assert 'data: ' in await asyncio.wait_for(anext(iterator),.5)
    finally:
        blocker.rollback()
        blocker.close()
        await iterator.aclose()
    assert (await asyncio.wait_for(pending,5)).status_code==200
    assert hub.inflight[aid]==0


@pytest.mark.parametrize('phase',['reserve','settle'])
async def test_cancellation_during_commit_is_completed_and_accounted(env,monkeypatch,phase):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    key=await hub.key(raw)
    response=await hub.gateway(key,payload(True)) if phase=='settle' else None
    entered=threading.Event(); release=threading.Event()
    original=hub.store.run
    async def blocked(function,*args):
        if function.__name__=='commit_'+phase:
            def operation():
                entered.set()
                assert release.wait(5)
                return function(*args)
            return await original(operation)
        return await original(function,*args)
    monkeypatch.setattr(hub.store,'run',blocked)
    task=asyncio.create_task(response.finalize() if response else hub.gateway(key,payload()))
    try:
        await wait_event(entered)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,5)
    row=await hub.store.aone('SELECT * FROM requests')
    budget=await hub.store.aone('SELECT used,reserved FROM api_keys WHERE id=?',(kid,))
    assert row['settled']==1
    assert budget=={'used':int(phase=='settle'),'reserved':0}
    assert hub.inflight[aid]==hub.key_inflight[kid]==0
    assert u.chat_calls==int(phase=='settle')
    if response:
        await response.finalize()
        assert u.last_stream.closed
        assert (await hub.store.aone('SELECT COUNT(*) AS n FROM ledger'))['n']==1


async def test_concurrent_key_limit_does_not_overreserve(env):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    await hub.store.aexecute('UPDATE accounts SET max_concurrency=10 WHERE id=?',(aid,))
    await hub.store.aexecute('UPDATE api_keys SET max_concurrency=1 WHERE id=?',(kid,))
    key=await hub.key(raw)
    results=await asyncio.gather(*(hub.gateway(key,payload(True)) for _ in range(10)),return_exceptions=True)
    accepted=[r for r in results if not isinstance(r,BaseException)]
    assert len(accepted)==1
    assert all(getattr(r,'status_code',None)==429 for r in results if isinstance(r,BaseException))
    await accepted[0].finalize()
    assert hub.key_inflight[kid]==hub.inflight[aid]==0
    assert (await hub.store.aone('SELECT reserved FROM api_keys WHERE id=?',(kid,)))['reserved']==0


async def test_concurrent_pool_edits_cannot_create_fallback_cycle(env):
    hub,b,u,_=env
    other=(await b.post('/api/pools',json={'name':'second'})).json()['id']
    results=await asyncio.gather(
        b.put('/api/pools/1',json={'name':'first','fallback_pool_id':other}),
        b.put(f'/api/pools/{other}',json={'name':'second','fallback_pool_id':1}))
    assert sorted(r.status_code for r in results)==[200,400]


@pytest.mark.parametrize('stored',[None,'','not-hex:bad',':','aa:bb',{'unexpected':'object'}])
def test_malformed_password_hash_is_rejected(stored):
    assert password_ok('password',stored) is False


async def test_corrupted_stored_password_returns_401(env):
    hub,b,u,_=env
    await hub.store.aset('password',None)
    assert (await b.post('/api/login',json={'password':'test-password-123'})).status_code==401


@pytest.mark.parametrize('value',['bad','NaN','Infinity',True,{},-1])
async def test_expiry_errors_are_safe_and_upstream_errors_are_distinct(value):
    data=credential();data['auth'].pop('expiresAt');data['auth']['expiresIn']=value
    with pytest.raises(ValueError,match='expiresIn'):
        normalize_credential(data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'code':0,'data':data['auth']}))) as client:
        with pytest.raises(ProviderError,match='invalid_refresh_credential'):
            await WorkBuddyCN(client).refresh(credential())


async def test_sticky_lru_is_bounded_and_long_identifiers_are_rejected(env):
    hub,b,u,_=env
    aid,raw,_=await setup(env)
    key=await hub.key(raw)
    hub.sticky_limit=3
    for sid in ('a','b','c','a','d'):
        await hub.acquire(key,'test-model',sid,payload())
        hub.inflight[aid]-=1
    from hub.security import digest
    assert len(hub.sticky)==3
    assert (key['id'],digest('b')) not in hub.sticky
    assert (key['id'],digest('a')) in hub.sticky
    for session in ('x'*513,'invalid id','tab\tvalue'):
        result=await b.post('/v1/chat/completions',headers={'Authorization':'Bearer '+raw,'X-Session-Id':session},json=payload())
        assert result.status_code==400
    assert u.chat_calls==0


@pytest.mark.parametrize('path,query,expected',[
    ('/api/restore',b'',200),('/api/restore',b'x=1',200),
    ('/api/restore/',b'',413),
    # Gateway paths now allow 32 MiB for long contexts, and a query string still
    # must not change which tier applies; the admin API stays at 2 MiB.
    ('/v1/chat/completions',b'x=1',200),('/api/keys',b'x=1',413)])
async def test_body_limits_with_query_trailing_slash_and_chunking(path,query,expected):
    messages=[]
    reached=[]
    async def downstream(scope,receive,send):
        reached.append(True)
        assert len((await receive())['body'])==3*1024*1024
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'ok'})
    chunks=iter([{'type':'http.request','body':b'x'*(1024*1024),'more_body':True} for _ in range(2)]+
                [{'type':'http.request','body':b'x'*(1024*1024),'more_body':False}])
    async def receive():return next(chunks)
    async def send(message):messages.append(message)
    await BoundedBody(downstream)({'type':'http','method':'POST','path':path,'query_string':query},receive,send)
    assert messages[0]['status']==expected
    assert bool(reached)==(expected==200)


@pytest.mark.parametrize('path,size,expected',[
    ('/v1/chat/completions',3*1024*1024,200),
    ('/v1/responses',3*1024*1024,200),
    ('/v1/messages',3*1024*1024,200),
    ('/v1/chat/completions/',3*1024*1024,413),
    ('/api/keys',3*1024*1024,413),
    ('/v1/chat/completions',33*1024*1024,413)])
async def test_gateway_body_limit_covers_long_context(path,size,expected):
    """~1M tokens of JSON is 4-5 MiB: the gateway paths must accept it, and only them."""
    messages=[]
    reached=[]
    async def downstream(scope,receive,send):
        reached.append(True)
        while True:
            message=await receive()
            if not message.get('more_body'):
                break
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'ok'})
    body=b'x'*size
    async def receive():return {'type':'http.request','body':body,'more_body':False}
    async def send(message):messages.append(message)
    await BoundedBody(downstream)({'type':'http','method':'POST','path':path,'query_string':b''},receive,send)
    assert messages[0]['status']==expected
    assert bool(reached)==(expected==200)


async def test_extra_allowed_host_enables_a_tls_front_end(tmp_path):
    """`tailscale serve` proxies to 127.0.0.1 but forwards its own Host header."""
    upstream=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    app=create_app(tmp_path,client=upstream,password='host-password-123',scheduler=False,
                   allowed_hosts=['Desk.Tailnet.TS.Net.'])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://desk.tailnet.ts.net') as client:
            # Normalised to lowercase without the trailing dot.
            assert (await client.get('/healthz')).status_code==200
            # Only the opted-in name is added; everything else stays rejected.
            assert (await client.get('/healthz',headers={'Host':'evil.example'})).status_code==400
            assert (await client.get('/healthz',headers={'Host':'other.tailnet.ts.net'})).status_code==400
    finally:
        await app.state.hub.store.aclose()
        await upstream.aclose()


def test_allow_host_argument_normalizes_and_rejects_junk():
    import argparse
    from main import host_argument
    assert host_argument('Desk.Tailnet.TS.Net.')=='desk.tailnet.ts.net'
    assert host_argument('desk.tailnet.ts.net:8792')=='desk.tailnet.ts.net'
    assert host_argument('[::1]')=='[::1]'
    assert host_argument('[::1]:8792')=='[::1]'
    assert host_argument('*.tailnet.ts.net')=='*.tailnet.ts.net'
    for bad in ('','   ','https://host','a/b','user@host','two words'):
        with pytest.raises(argparse.ArgumentTypeError):
            host_argument(bad)


def test_migration_rolls_back_ddl_and_version_on_failure(tmp_path,monkeypatch):
    with sqlite3.connect(tmp_path/'hub.db') as c:
        c.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
    original=Store.connect
    def fail_version(self):
        conn=original(self)
        conn.set_authorizer(lambda action,arg1,arg2,*rest: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_PRAGMA and arg1=='user_version' and arg2=='2' else sqlite3.SQLITE_OK)
        return conn
    with monkeypatch.context() as patch:
        patch.setattr(Store,'connect',fail_version)
        with pytest.raises(sqlite3.DatabaseError):Store(tmp_path)
    with sqlite3.connect(tmp_path/'hub.db') as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==1
        assert 'fallback_pool_id' not in [r[1] for r in c.execute('PRAGMA table_info(pools)')]
    upgraded=Store(tmp_path)
    assert upgraded.one('PRAGMA user_version')['user_version']==3
    columns={row['name'] for row in upgraded.all('PRAGMA table_info(requests)')}
    assert {'endpoint','cache_tokens'} <= columns
    assert 'secret' in {row['name'] for row in upgraded.all('PRAGMA table_info(api_keys)')}
    upgraded.worker.shutdown()


async def test_bulk_restart_recovery_is_idempotent(env):
    hub,b,u,folder=env
    aid,raw,kid=await setup(env)
    count=2000
    def seed():
        with hub.store.tx() as c:
            c.executemany("INSERT INTO requests(id,created,key_id,account_id,model,upstream_model,state) VALUES(?,?,?,?,?,?,'pending')",
                          [(f'recover-{i}',time.time(),kid,aid,'test-model','test-model') for i in range(count)])
            c.execute('UPDATE api_keys SET reserved=? WHERE id=?',(count,kid))
    await hub.store.run(seed)
    for _ in range(2):
        recovered=await asyncio.to_thread(Store,folder)
        assert recovered.one('SELECT used,reserved FROM api_keys WHERE id=?',(kid,))=={'used':count,'reserved':0}
        assert recovered.one('SELECT COUNT(*) AS n FROM ledger')['n']==count
        await recovered.aclose()


@pytest.mark.parametrize('wire',['json','sse'])
@pytest.mark.parametrize('field',['reasoning_content','reasoning'])
async def test_reasoning_message_variants_and_alias_catalog(env,wire,field):
    hub,b,u,_=env
    aid,raw,kid=await setup(env)
    await b.post('/api/models/aliases',json={'alias':'daily','model':'test-model'})
    await hub.store.aexecute('UPDATE api_keys SET models=? WHERE id=?',(json.dumps(['daily']),kid))
    listed=await b.get('/v1/models',headers={'Authorization':'Bearer '+raw})
    assert [m['id'] for m in listed.json()['data']]==['daily']
    async def answer(request):
        obj={'model':'test-model','choices':[{'index':0,'message':{'role':'assistant','content':'ok',field:'provided reasoning'},'finish_reason':'stop'}]}
        if wire=='json':return httpx.Response(200,json=obj)
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Stream([('data: '+json.dumps(obj)+'\n\ndata: [DONE]\n\n').encode()]))
    original=hub.client
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as temporary:
        hub.client=temporary
        try:
            result=await chat(b,raw,{**payload(),'model':'daily'})
            assert result.status_code==200,result.text
            assert result.json()['choices'][0]['message']['reasoning_content']=='provided reasoning'
            assert result.json()['model']=='daily'
        finally:hub.client=original
