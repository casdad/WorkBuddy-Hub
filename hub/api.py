import asyncio
import base64
import csv
import io
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import qrcode
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .providers import ProviderError
from .security import digest, password_hash, password_ok, make_backup, restore_backup
from .service import Hub


# Hosts that are always accepted. Anything else must be opted in via --allow-host.
LOOPBACK_HOSTS=('127.0.0.1','localhost','[::1]','testserver')
DEFAULT_BODY_LIMIT=2*1024*1024
# ~1M tokens of JSON is 4-5 MiB, so long-context generation needs far more room
# than the admin API. Only these exact paths get the larger allowance.
GATEWAY_PATHS={'/v1/chat/completions','/v1/responses','/v1/messages'}
GATEWAY_BODY_LIMIT=32*1024*1024
BODY_LIMITS={'/api/restore':64*1024*1024}


class BoundedBody:
    def __init__(self, app):
        self.app=app

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope['method'] not in ('POST','PUT','PATCH'):
            return await self.app(scope,receive,send)
        # Exact-match sets only: a trailing slash or extra segment must not borrow
        # the larger allowance, so unmatched paths fall back to the small limit.
        path=scope['path']
        if path in BODY_LIMITS:
            limit=BODY_LIMITS[path]
        elif path in GATEWAY_PATHS:
            limit=GATEWAY_BODY_LIMIT
        else:
            limit=DEFAULT_BODY_LIMIT
        parts=[]
        size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect':
                return
            part=message.get('body',b'')
            size+=len(part)
            if size>limit:
                return await JSONResponse({'error':{'message':'请求体超过限制'}},status_code=413)(scope,receive,send)
            parts.append(part)
            if not message.get('more_body'):
                break
        used=False
        async def replay():
            nonlocal used
            if not used:
                used=True
                return {'type':'http.request','body':b''.join(parts),'more_body':False}
            return await receive()
        return await self.app(scope,replay,send)


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')


class Login(Input):
    password: str=Field(min_length=1,max_length=1024)


class AccountInput(Input):
    name: str=Field(min_length=1,max_length=100)
    provider: Literal['workbuddy_cn','openai_compatible']='workbuddy_cn'
    credential: dict
    pool_id: int=1


class AccountEdit(Input):
    name: str|None=Field(None,min_length=1,max_length=100)
    enabled: bool|None=None
    max_concurrency: int|None=Field(None,ge=1,le=32)


class PoolInput(Input):
    name: str=Field(min_length=1,max_length=100)
    strategy: Literal['balanced','expiring']='balanced'
    account_ids: list[int]=Field(default_factory=list,max_length=1000)
    fallback_pool_id: int|None=None


class KeyInput(Input):
    name: str=Field(min_length=1,max_length=100)
    pool_id: int=1
    models: list[str]=Field(default_factory=list,max_length=1000)
    expires: float|None=None
    request_limit: int|None=Field(None,ge=1)
    rpm: int=Field(60,ge=1,le=10000)
    max_concurrency: int=Field(2,ge=1,le=100)


class KeyEdit(Input):
    enabled: bool|None=None
    models: list[str]|None=Field(None,max_length=1000)


class SettingsInput(Input):
    sync_interval: int=Field(900,ge=60,le=86400)
    retention_days: int=Field(30,ge=1,le=3650)


class BackupInput(Input):
    passphrase: str=Field(min_length=12,max_length=1024)


class RestoreInput(BackupInput):
    data: str=Field(max_length=60*1024*1024)


class PasswordInput(Input):
    current: str
    password: str=Field(min_length=12,max_length=1024)


class AliasInput(Input):
    alias: str=Field(min_length=1,max_length=200)
    model: str=Field(min_length=1,max_length=200)


def validate_chat(body):
    if not isinstance(body,dict) or not isinstance(body.get('model'),str) or not body['model']:
        raise HTTPException(400,'model 必须是非空字符串')
    if len(body['model'])>200 or not isinstance(body.get('messages'),list) or not body['messages']:
        raise HTTPException(400,'messages 必须是非空数组')
    # `store` and `metadata` are sent by OpenAI-compatible clients by default. The
    # hub keeps no server-side conversation, so the stateless spelling is accepted
    # and dropped before forwarding; store=true is refused rather than silently
    # ignored, because nothing could ever be retrieved.
    allowed={'model','messages','stream','stream_options','tools','tool_choice','parallel_tool_calls','temperature','top_p','max_tokens','max_completion_tokens','stop','seed','presence_penalty','frequency_penalty','response_format','reasoning_effort','user','n','store','metadata'}
    unsupported=set(body)-allowed
    if unsupported:
        raise HTTPException(400,'不支持的请求字段：'+', '.join(sorted(unsupported)))
    if 'store' in body:
        if not isinstance(body['store'],bool):
            raise HTTPException(400,'store 必须是布尔值')
        if body['store']:
            raise HTTPException(400,'本服务不保存服务端对话，不支持 store=true；请设为 false 或省略该字段')
    if 'metadata' in body and not isinstance(body['metadata'],(dict,type(None))):
        raise HTTPException(400,'metadata 必须是对象')
    if body.get('n',1)!=1 or not isinstance(body.get('stream',False),bool):
        raise HTTPException(400,'仅支持 n=1，stream 必须是布尔值')
    tools=body.get('tools',[])
    if not isinstance(tools,list):
        raise HTTPException(400,'tools 必须是数组')
    names=set()
    for tool in tools:
        function=tool.get('function') if isinstance(tool,dict) else None
        if not isinstance(function,dict) or tool.get('type')!='function' or not isinstance(function.get('name'),str) or not function['name'].strip():
            raise HTTPException(400,'仅支持带名称的 function 工具')
        names.add(function['name'])
    if 'tool_choice' in body:
        choice=body['tool_choice']
        if isinstance(choice,dict):
            function=choice.get('function')
            if choice.get('type')!='function' or not isinstance(function,dict) or not isinstance(function.get('name'),str) or function['name'] not in names:
                raise HTTPException(400,'tool_choice 必须指定 tools 中的函数')
        elif choice not in ('auto','none','required'):
            raise HTTPException(400,'不支持此 tool_choice')
    for message in body['messages']:
        if not isinstance(message,dict) or message.get('role') not in ('system','developer','user','assistant','tool'):
            raise HTTPException(400,'不支持的消息角色')
        content=message.get('content')
        if not isinstance(content,(str,list)) and not (content is None and message.get('tool_calls')):
            raise HTTPException(400,'不支持的消息内容')
        if isinstance(content,list):
            for block in content:
                if not isinstance(block,dict) or block.get('type') not in ('text','image_url'):
                    raise HTTPException(400,'仅支持文本和 image_url 内容块')
    return body


def session_id(request):
    value=request.headers.get('x-session-id','')
    if len(value)>512 or any(ord(char)<33 or ord(char)>126 for char in value):
        raise HTTPException(400,'X-Session-Id 必须是不超过 512 字符的可打印 ASCII 标识')
    return value


def create_app(folder: Path, client=None, password=None, scheduler=True, static=None, allowed_hosts=None):
    hub=Hub(folder,client,password)
    @asynccontextmanager
    async def lifespan(app):
        task=asyncio.create_task(hub.scheduler()) if scheduler else None
        yield
        hub.stopping=True
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await hub.client.aclose()
        await hub.store.aclose()
    app=FastAPI(title='WorkBuddy Hub',version=__version__,lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.hub=hub
    app.add_middleware(BoundedBody)
    # Loopback always stays allowed. Extra hostnames are opt-in (--allow-host) so a
    # TLS front end such as `tailscale serve` can reach the hub without widening the
    # default surface. Starlette also accepts wildcard entries like *.example.com.
    hosts=list(LOOPBACK_HOSTS)
    for value in allowed_hosts or []:
        host=str(value).strip().lower().rstrip('.')
        if host and host not in hosts:
            hosts.append(host)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts)

    @app.exception_handler(HTTPException)
    async def http_error(request,error):
        return JSONResponse({'error':{'message':str(error.detail)}},status_code=error.status_code,headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,error):
        # Pydantic errors may include raw passwords/credentials in input.
        return JSONResponse({'error':{'message':'参数不合法','fields':['.'.join(str(x) for x in e['loc']) for e in error.errors()]}},status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request,error):
        return JSONResponse({'error':{'message':str(error)}},status_code=400)

    @app.exception_handler(ProviderError)
    async def provider_error(request,error):
        return JSONResponse({'error':{'message':error.code}},status_code=502)

    @app.middleware('http')
    async def headers(request,call_next):
        origin=request.headers.get('origin')
        if request.method not in ('GET','HEAD','OPTIONS') and origin:
            p=urlparse(origin)
            if p.netloc!=request.headers.get('host') or p.scheme!=request.url.scheme:
                return JSONResponse({'error':{'message':'跨站请求被拒绝'}},status_code=403)
        response=await call_next(request)
        response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
                                 'Content-Security-Policy':"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"})
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        return response

    async def admin(request:Request):
        token=request.cookies.get('hub_session','')
        session=(await hub.store.aone('SELECT * FROM sessions WHERE token=? AND expires>?',(digest(token),time.time())))
        if not session:
            raise HTTPException(401,'请先登录管理台')
        if request.method not in ('GET','HEAD') and not secrets.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']):
            raise HTTPException(403,'CSRF 校验失败')
        return session

    async def api_key(request:Request):
        auth=request.headers.get('authorization','')
        raw=auth[7:] if auth.startswith('Bearer ') else request.headers.get('x-api-key','')
        return (await hub.key(raw))

    @app.get('/healthz')
    async def health():
        return {'status':'ok','version':__version__,'check':'process_liveness'}

    @app.post('/api/login')
    async def login(body:Login,request:Request):
        async with hub.login_lock:
            host=request.client.host if request.client else 'local'
            failures=[t for t in hub.login_failures[host] if t>time.time()-300]
            hub.login_failures[host]=failures
            if len(failures)>=5:
                raise HTTPException(429,'登录失败次数过多，5 分钟后重试')
            if not await asyncio.to_thread(password_ok,body.password,(await hub.store.aget('password'))):
                failures.append(time.time())
                raise HTTPException(401,'密码错误')
            hub.login_failures[host]=[]
            token=secrets.token_urlsafe(32)
            csrf=secrets.token_urlsafe(32)
            (await hub.store.aexecute('INSERT INTO sessions VALUES(?,?,?)',(digest(token),csrf,time.time()+12*3600)))
            result=JSONResponse({'csrf':csrf})
            result.set_cookie('hub_session',token,httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=12*3600)
            return result

    @app.get('/api/session')
    async def session(current=Depends(admin)):
        return {'csrf':current['csrf']}

    @app.post('/api/logout')
    async def logout(current=Depends(admin)):
        (await hub.store.aexecute('DELETE FROM sessions WHERE token=?',(current['token'],)))
        response=JSONResponse({'ok':True})
        response.delete_cookie('hub_session')
        return response

    @app.get('/api/summary',dependencies=[Depends(admin)])
    async def summary():
        accounts=(await hub.public_accounts())
        stats=(await hub.store.aone("SELECT COUNT(*) AS requests,SUM(state='success') AS successes,SUM(state='interrupted') AS interrupted,SUM(credits_micro) AS credits_micro,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,SUM(usage_source='unknown') AS unknown_usage,AVG(ttft_ms) AS ttft_ms FROM requests WHERE created>?",(time.time()-86400,)))
        expiry=(await hub.store.aone('SELECT SUM(remaining_micro) AS amount FROM packages WHERE expires>? AND expires<=?',(time.time(),time.time()+7*86400)))
        daily=(await hub.store.aall("SELECT date(created,'unixepoch','+8 hours') AS day,COUNT(*) AS requests,SUM(state='success') AS successes,SUM(credits_micro) AS credits_micro FROM requests WHERE created>? GROUP BY day ORDER BY day",(time.time()-14*86400,)))
        return {'accounts':len(accounts),'ready':sum(a['state']=='ready' for a in accounts),
                'balance_micro':sum(a['balance_micro'] for a in accounts if a['balance_micro'] is not None),
                'unknown_balances':sum(a['balance_micro'] is None for a in accounts),
                'stale_balances':sum(a['balance_stale'] for a in accounts),'expiring_micro':expiry['amount'] or 0,
                'last24h':stats,'daily':daily,'alerts':[a for a in accounts if a['enabled'] and (a['state']!='ready' or a['last_error'])]}

    @app.get('/api/accounts',dependencies=[Depends(admin)])
    async def accounts():
        return (await hub.public_accounts())

    @app.post('/api/accounts',dependencies=[Depends(admin)])
    async def add_account(body:AccountInput):
        aid=(await hub.add_account(body.name,body.provider,body.credential,body.pool_id))
        return {'id':aid,'sync':await hub.sync(aid)}

    @app.patch('/api/accounts/{aid}',dependencies=[Depends(admin)])
    async def edit_account(aid:int,body:AccountEdit):
        (await hub.account(aid))
        fields=body.model_dump(exclude_none=True)
        if fields:
            (await hub.store.aexecute('UPDATE accounts SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),aid)))
        (await hub.store.aaudit('account_update',aid))
        return {'ok':True}

    @app.get('/api/accounts/{aid}',dependencies=[Depends(admin)])
    async def account_detail(aid:int):
        (await hub.account(aid))
        return {'packages':(await hub.store.aall('SELECT id,package_id,name,remaining_micro,expires,observed,source FROM packages WHERE account_id=? ORDER BY expires',(aid,))),
                'snapshots':(await hub.store.aall('SELECT remaining_micro,observed,source FROM snapshots WHERE account_id=? ORDER BY id DESC LIMIT 100',(aid,))),
                'models':(await hub.store.aall('SELECT model,capabilities,verified FROM models WHERE account_id=?',(aid,))),
                'requests':(await hub.store.aall('SELECT * FROM requests WHERE account_id=? ORDER BY created DESC LIMIT 20',(aid,)))}

    @app.post('/api/accounts/{aid}/sync',dependencies=[Depends(admin)])
    async def sync(aid:int):
        (await hub.account(aid))
        return await hub.sync(aid)

    @app.post('/api/accounts/{aid}/refresh',dependencies=[Depends(admin)])
    async def refresh(aid:int):
        if hub.inflight[aid] or hub.sync_locks[aid].locked():
            raise HTTPException(409,'账号使用中')
        version=(await hub.account(aid))['version']
        await hub.credentials(aid,True,version)
        (await hub.store.aaudit('credential_refresh',aid))
        return {'ok':True}

    @app.post('/api/oauth/start',dependencies=[Depends(admin)])
    async def oauth_start():
        state,uri=await hub.providers['workbuddy_cn'].auth_start()
        lid=secrets.token_urlsafe(24)
        hub.oauth[lid]={'state':state,'expires':time.time()+600,'result':None,'last_poll':0,'lock':asyncio.Lock()}
        out=io.BytesIO()
        qrcode.make(uri).save(out,format='PNG')
        return {'login_id':lid,'url':uri,'expires_in':600,'qr':'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()}

    @app.post('/api/oauth/{lid}/poll',dependencies=[Depends(admin)])
    async def oauth_poll(lid:str):
        login=hub.oauth.get(lid)
        if not login or login['expires']<time.time():
            raise HTTPException(410,'授权会话已过期')
        async with login['lock']:
            if login['result']:
                return {'done':True,'id':login['result']}
            if time.time()-login['last_poll']<2:
                return {'done':False}
            login['last_poll']=time.time()
            credential=await hub.providers['workbuddy_cn'].auth_poll(login['state'])
            if not credential:
                return {'done':False}
            aid=(await hub.add_account(credential['account']['nickname'],'workbuddy_cn',credential))
            login['result']=aid
            await hub.sync(aid)
            return {'done':True,'id':aid}

    @app.get('/api/pools',dependencies=[Depends(admin)])
    async def pools():
        rows=(await hub.store.aall('SELECT * FROM pools'))
        for row in rows:
            row['account_ids']=[x['account_id'] for x in (await hub.store.aall('SELECT account_id FROM pool_members WHERE pool_id=?',(row['id'],)))]
        return rows

    async def save_pool(body,pid=None):
        def commit_pool(pid):
            with hub.store.tx() as conn:
                # Validate and mutate in the same transaction: concurrent pool
                # edits must not both validate an older graph and create a cycle.
                seen={pid} if pid else set()
                fallback=body.fallback_pool_id
                while fallback:
                    if fallback in seen:
                        raise HTTPException(400,'备用池不能形成循环')
                    seen.add(fallback)
                    pool=conn.execute('SELECT fallback_pool_id FROM pools WHERE id=?',(fallback,)).fetchone()
                    if not pool:
                        raise HTTPException(400,'备用池不存在')
                    fallback=pool['fallback_pool_id']
                if pid:
                    if not conn.execute('SELECT id FROM pools WHERE id=?',(pid,)).fetchone():
                        raise HTTPException(404,'账号池不存在')
                    conn.execute('UPDATE pools SET name=?,strategy=? WHERE id=?',(body.name,body.strategy,pid))
                else:
                    pid=conn.execute('INSERT INTO pools(name,strategy) VALUES(?,?)',(body.name,body.strategy)).lastrowid
                conn.execute('DELETE FROM pool_members WHERE pool_id=?',(pid,))
                for aid in set(body.account_ids):
                    if not conn.execute('SELECT id FROM accounts WHERE id=?',(aid,)).fetchone():
                        raise HTTPException(404,'账号不存在')
                    conn.execute('INSERT INTO pool_members VALUES(?,?)',(pid,aid))
                conn.execute('UPDATE pools SET fallback_pool_id=? WHERE id=?',(body.fallback_pool_id,pid))
            return pid
        pid=await hub.store.run(commit_pool,pid)
        (await hub.store.aaudit('pool_update',pid))
        return {'id':pid}

    @app.post('/api/pools',dependencies=[Depends(admin)])
    async def add_pool(body:PoolInput):
        return (await save_pool(body))

    @app.put('/api/pools/{pid}',dependencies=[Depends(admin)])
    async def edit_pool(pid:int,body:PoolInput):
        return (await save_pool(body,pid))

    @app.delete('/api/pools/{pid}',dependencies=[Depends(admin)])
    async def delete_pool(pid:int):
        def commit_delete_pool():
            with hub.store.tx() as conn:
                if not conn.execute('SELECT id FROM pools WHERE id=?',(pid,)).fetchone():
                    raise HTTPException(404,'账号池不存在')
                # New accounts and keys default to pool 1, so removing it would
                # leave their default target dangling.
                if pid==1:
                    raise HTTPException(400,'默认账号池是新建账号与 Key 的默认目标，不能删除')
                used=conn.execute('SELECT COUNT(*) FROM api_keys WHERE pool_id=?',(pid,)).fetchone()[0]
                if used:
                    raise HTTPException(409,'该账号池仍有 '+str(used)+' 个 API Key 使用，请先修改或删除这些 Key')
                # Clear incoming fallback links in the same transaction: the column
                # has no ON DELETE action, and a dangling link would break the chain.
                detached=conn.execute('UPDATE pools SET fallback_pool_id=NULL WHERE fallback_pool_id=?',(pid,)).rowcount
                conn.execute('DELETE FROM pool_members WHERE pool_id=?',(pid,))
                conn.execute('DELETE FROM pools WHERE id=?',(pid,))
            return detached
        detached=await hub.store.run(commit_delete_pool)
        (await hub.store.aaudit('pool_deleted',pid))
        return {'ok':True,'detached_fallbacks':detached}

    @app.get('/api/keys',dependencies=[Depends(admin)])
    async def keys():
        return (await hub.store.aall('SELECT id,name,prefix,pool_id,models,enabled,expires,request_limit,used,reserved,rpm,max_concurrency,created FROM api_keys ORDER BY id DESC'))

    async def checked_model_scope(values):
        """Normalize a Key's model allow-list; empty means "everything in the pool".

        Names outside the catalog are rejected so a typo cannot silently create a
        Key that is allowed to call nothing.
        """
        scope=[]
        for value in values:
            name=str(value).strip()
            if name and name not in scope:
                scope.append(name)
        if not scope:
            return []
        known={row['model'] for row in (await hub.store.aall('SELECT DISTINCT model FROM models'))}
        known|={row['alias'] for row in (await hub.store.aall('SELECT alias FROM aliases'))}
        unknown=[name for name in scope if name not in known]
        if unknown:
            raise HTTPException(400,'模型不在目录中：'+', '.join(unknown[:10]))
        return scope

    @app.post('/api/keys',dependencies=[Depends(admin)])
    async def add_key(body:KeyInput):
        if not (await hub.store.aone('SELECT id FROM pools WHERE id=?',(body.pool_id,))):
            raise HTTPException(400,'账号池不存在')
        scope=await checked_model_scope(body.models)
        raw='wbh_'+secrets.token_urlsafe(32)
        kid=(await hub.store.aexecute('INSERT INTO api_keys(name,prefix,digest,secret,pool_id,models,expires,request_limit,rpm,max_concurrency,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                              (body.name,raw[:10],digest(raw),hub.vault.encrypt({'key':raw}),body.pool_id,json.dumps(scope),body.expires,body.request_limit,body.rpm,body.max_concurrency,time.time())))
        (await hub.store.aaudit('key_created',kid))
        return {'id':kid,'key':raw}

    @app.get('/api/keys/{kid}/secret',dependencies=[Depends(admin)])
    async def key_secret(kid:int):
        row=await hub.store.aone('SELECT id,name,secret FROM api_keys WHERE id=?',(kid,))
        if not row:
            raise HTTPException(404,'API Key 不存在')
        if not row['secret']:
            raise HTTPException(409,'该 Key 创建于旧版本，明文未保存，请使用「重新生成」换一个新密钥')
        (await hub.store.aaudit('key_secret_viewed',row['name']))
        return {'key':hub.vault.decrypt(row['secret'])['key']}

    @app.post('/api/keys/{kid}/rotate',dependencies=[Depends(admin)])
    async def rotate_key(kid:int):
        row=await hub.store.aone('SELECT id,name FROM api_keys WHERE id=?',(kid,))
        if not row:
            raise HTTPException(404,'API Key 不存在')
        raw='wbh_'+secrets.token_urlsafe(32)
        (await hub.store.aexecute('UPDATE api_keys SET prefix=?,digest=?,secret=? WHERE id=?',
                                  (raw[:10],digest(raw),hub.vault.encrypt({'key':raw}),kid)))
        (await hub.store.aaudit('key_rotated',row['name']))
        return {'ok':True,'key':raw}

    @app.patch('/api/keys/{kid}',dependencies=[Depends(admin)])
    async def edit_key(kid:int,body:KeyEdit):
        if not (await hub.store.aone('SELECT id FROM api_keys WHERE id=?',(kid,))):
            raise HTTPException(404,'Key 不存在')
        fields={}
        if body.enabled is not None:
            fields['enabled']=int(body.enabled)
        if body.models is not None:
            fields['models']=json.dumps(await checked_model_scope(body.models))
        if not fields:
            raise HTTPException(400,'没有可修改的字段')
        (await hub.store.aexecute('UPDATE api_keys SET '+','.join(name+'=?' for name in fields)+' WHERE id=?',(*fields.values(),kid)))
        (await hub.store.aaudit('key_update',kid))
        return {'ok':True}

    @app.delete('/api/keys/{kid}',dependencies=[Depends(admin)])
    async def delete_key(kid:int):
        def commit_delete_key():
            with hub.store.tx() as conn:
                key=conn.execute('SELECT * FROM api_keys WHERE id=?',(kid,)).fetchone()
                if not key:
                    raise HTTPException(404,'API Key 不存在')
                # reserved>0 means a request is still unsettled, so the key must
                # survive until its ledger entry lands.
                if key['reserved']>0:
                    raise HTTPException(409,'该 API Key 有进行中的请求，请稍后再删除')
                # History outlives the key: detach the references instead of taking
                # the request log and ledger down with it.
                conn.execute('UPDATE requests SET key_id=NULL WHERE key_id=?',(kid,))
                conn.execute('UPDATE ledger SET key_id=NULL WHERE key_id=?',(kid,))
                conn.execute('DELETE FROM api_keys WHERE id=?',(kid,))
            return key['name']
        name=await hub.store.run(commit_delete_key)
        hub.key_inflight.pop(kid,None)
        (await hub.store.aaudit('key_deleted',name))
        return {'ok':True}

    @app.get('/api/models',dependencies=[Depends(admin)])
    async def models():
        return {'models':(await hub.store.aall('SELECT m.*,a.name AS account_name FROM models m JOIN accounts a ON a.id=m.account_id ORDER BY m.model')),
                'aliases':(await hub.store.aall('SELECT * FROM aliases')),'protocols':{'chat':'text, tools, images (per model)','responses':'stateless text and function tools','messages':'text, tools, images (per model)'}}

    @app.post('/api/models/aliases',dependencies=[Depends(admin)])
    async def alias(body:AliasInput):
        if body.alias==body.model or not (await hub.store.aone('SELECT model FROM models WHERE model=?',(body.model,))):
            raise HTTPException(400,'目标模型必须存在且与别名不同')
        if (await hub.store.aone('SELECT model FROM models WHERE model=?',(body.alias,))):
            raise HTTPException(400,'别名不能覆盖真实模型')
        (await hub.store.aexecute('INSERT INTO aliases VALUES(?,?) ON CONFLICT(alias) DO UPDATE SET model=excluded.model',(body.alias,body.model)))
        (await hub.store.aaudit('model_alias',body.alias))
        return {'ok':True}

    async def log_rows(account_id=None,key_id=None,model=None,state=None,since=None,until=None,limit=100,offset=0):
        clauses=[]
        values=[]
        for field,value in [('account_id',account_id),('key_id',key_id),('model',model),('state',state)]:
            if value is not None and value!='':
                clauses.append(field+'=?');values.append(value)
        for field,value in [('created>=',since),('created<=',until)]:
            if value is not None:
                clauses.append(field+'?');values.append(value)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        return (await hub.store.aall('SELECT * FROM requests'+where+' ORDER BY created DESC LIMIT ? OFFSET ?',(*values,min(max(limit,1),5000),max(offset,0))))

    @app.get('/api/logs',dependencies=[Depends(admin)])
    async def logs(account_id:int|None=None,key_id:int|None=None,model:str|None=None,state:str|None=None,since:float|None=None,until:float|None=None,limit:int=100,offset:int=0):
        return (await log_rows(account_id,key_id,model,state,since,until,limit,offset))

    @app.get('/api/usage',dependencies=[Depends(admin)])
    async def usage(hours:int=24):
        hours=min(max(hours,1),24*30)
        since=time.time()-hours*3600
        rows=(await hub.store.aall('SELECT model,endpoint,account_id,input_tokens,output_tokens,cache_tokens,credits_micro,latency_ms,ttft_ms,state,created FROM requests WHERE created>=?',(since,)))
        names={row['id']:row['name'] for row in (await hub.store.aall('SELECT id,name FROM accounts'))}
        def blank():
            return {'requests':0,'input_tokens':0,'output_tokens':0,'cache_tokens':0,'credits_micro':0}
        def add(target,row):
            target['requests']+=1
            target['input_tokens']+=row['input_tokens'] or 0
            target['output_tokens']+=row['output_tokens'] or 0
            target['cache_tokens']+=row['cache_tokens'] or 0
            target['credits_micro']+=row['credits_micro'] or 0
        totals=blank()
        by_model={}
        by_account={}
        by_endpoint={}
        series={}
        latencies=[]
        ttfts=[]
        # Hourly buckets for a day, daily buckets beyond that.
        width=3600 if hours<=48 else 86400
        for row in rows:
            add(totals,row)
            add(by_model.setdefault(row['model'],blank()),row)
            add(by_account.setdefault(names.get(row['account_id']) or '已删除账号',blank()),row)
            add(by_endpoint.setdefault(row['endpoint'] or 'chat',blank()),row)
            add(series.setdefault(int(row['created']//width*width),blank()),row)
            if row['latency_ms'] is not None:
                latencies.append(row['latency_ms'])
            if row['ttft_ms'] is not None:
                ttfts.append(row['ttft_ms'])
        cache_reports=sum(1 for row in rows if row['cache_tokens'] is not None)
        def average(values):
            return int(sum(values)/len(values)) if values else None
        def ranked(groups):
            return [{'name':name,**values} for name,values in
                    sorted(groups.items(),key=lambda item:(-item[1]['requests'],item[0]))]
        return {'range_hours':hours,'bucket_seconds':width,'requests':len(rows),
                'totals':{**totals,'successes':sum(1 for row in rows if row['state']=='success'),
                          'tokens_total':totals['input_tokens']+totals['output_tokens'],
                          'avg_latency_ms':average(latencies),'avg_ttft_ms':average(ttfts),
                          # Distinguish "no cache reported" from "0% hit rate".
                          'cache_hit_rate':(totals['cache_tokens']/totals['input_tokens'])
                              if totals['input_tokens'] and cache_reports else None},
                'by_model':ranked(by_model),'by_account':ranked(by_account),'by_endpoint':ranked(by_endpoint),
                'series':[{'slot':slot,**values} for slot,values in sorted(series.items())]}

    @app.get('/api/logs/export',dependencies=[Depends(admin)])
    async def export(account_id:int|None=None,key_id:int|None=None,model:str|None=None,state:str|None=None,since:float|None=None,until:float|None=None):
        rows=(await log_rows(account_id,key_id,model,state,since,until,5000))
        out=io.StringIO()
        if rows:
            writer=csv.DictWriter(out,fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                writer.writerow({k: ("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v) for k,v in row.items()})
        return Response('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="requests.csv"'})

    @app.get('/api/logs/{rid}',dependencies=[Depends(admin)])
    async def log_detail(rid:str):
        return {'request':(await hub.store.aone('SELECT * FROM requests WHERE id=?',(rid,))),
                'attempts':(await hub.store.aall('SELECT * FROM attempts WHERE request_id=?',(rid,))),
                'ledger':(await hub.store.aone('SELECT * FROM ledger WHERE request_id=?',(rid,)))}

    @app.get('/api/jobs',dependencies=[Depends(admin)])
    async def jobs():
        return (await hub.store.aall('SELECT * FROM jobs ORDER BY id DESC LIMIT 100'))

    @app.get('/api/settings',dependencies=[Depends(admin)])
    async def settings():
        return {**{k:(await hub.store.aget(k)) for k in ('sync_interval','retention_days')},'version':__version__,'data_dir':str(folder),
                'schema':(await hub.store.aone('PRAGMA user_version'))['user_version'],
                'credential_protection':'Windows DPAPI + Fernet' if __import__('os').name=='nt' else 'Fernet + file permissions',
                'audit':(await hub.store.aall('SELECT * FROM audit ORDER BY id DESC LIMIT 30'))}

    @app.put('/api/settings',dependencies=[Depends(admin)])
    async def edit_settings(body:SettingsInput):
        for key,value in body.model_dump().items():
            (await hub.store.aset(key,value))
        (await hub.store.aaudit('settings_update'))
        return {'ok':True}

    @app.post('/api/password',dependencies=[Depends(admin)])
    async def edit_password(body:PasswordInput):
        if not await asyncio.to_thread(password_ok,body.current,(await hub.store.aget('password'))):
            raise HTTPException(403,'当前密码错误')
        (await hub.store.aset('password',await asyncio.to_thread(password_hash,body.password)))
        (await hub.store.aexecute('DELETE FROM sessions'))
        initial=folder/'initial-admin.txt'
        if initial.exists():
            initial.unlink()
        (await hub.store.aaudit('password_changed'))
        return {'ok':True}

    @app.post('/api/backup',dependencies=[Depends(admin)])
    async def backup(body:BackupInput):
        data=await asyncio.to_thread(make_backup,hub.store,hub.vault,body.passphrase)
        (await hub.store.aaudit('encrypted_backup'))
        return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="workbuddy-hub.wbh"'})

    @app.post('/api/restore',dependencies=[Depends(admin)])
    async def restore(body:RestoreInput):
        destination=folder.parent/('restored-'+secrets.token_hex(5))
        try:
            await asyncio.to_thread(restore_backup,base64.b64decode(body.data,validate=True),body.passphrase,destination)
        except Exception:
            raise HTTPException(400,'恢复失败：备份密码、文件或数据库校验不通过') from None
        (await hub.store.aaudit('backup_restored',destination.name))
        return {'directory':str(destination),'instruction':'关闭当前程序，用 --data-dir 指向此目录启动；原数据保持不变。'}

    @app.get('/v1/models')
    async def api_models(key=Depends(api_key)):
        return {'object':'list','data':[{'id':m,'object':'model','owned_by':'workbuddy-hub'} for m in (await hub.key_models(key))]}

    @app.get('/v1/usage')
    async def api_usage(key=Depends(api_key)):
        return {k:key[k] for k in ('name','used','reserved','request_limit','expires')}

    @app.post('/v1/chat/completions')
    async def chat(request:Request,key=Depends(api_key)):
        try:
            body=validate_chat(await request.json())
        except json.JSONDecodeError:
            raise HTTPException(400,'无效 JSON') from None
        return await hub.gateway(key,body,session_id(request))

    from .protocols import protocol_response
    @app.post('/v1/responses')
    async def responses(request:Request,key=Depends(api_key)):
        return await protocol_response(hub,key,await request.json(),'responses',validate_chat,session_id(request))

    @app.post('/v1/messages')
    async def messages(request:Request,key=Depends(api_key)):
        return await protocol_response(hub,key,await request.json(),'messages',validate_chat,session_id(request))

    static=static or Path(__file__).resolve().parent.parent/'web'/'dist'
    # Require the real entry points: a partially built or cleaned dist must not
    # crash startup inside StaticFiles(directory=...).
    if (static/'assets').is_dir() and (static/'index.html').is_file():
        app.mount('/assets',StaticFiles(directory=static/'assets'),name='assets')
        @app.get('/')
        async def index():
            return FileResponse(static/'index.html')
    return app
