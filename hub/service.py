import asyncio
import json
import secrets
import time
import uuid
from collections import defaultdict, OrderedDict

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .providers import Compatible, WorkBuddyCN, GENERATION_TIMEOUT, ProviderError, micros, normalize_credential, pick_effort, sse_events, upstream_error_detail, valid_token, validate_endpoint
from .security import Vault, digest, password_hash
from .store import Store
from .streaming import OwnedStreamingResponse
from .concurrency import finish_owned, serialized_admission


class Hub:
    def __init__(self, folder, client=None, password=None):
        self.store=Store(folder)
        self.vault=Vault(folder)
        self.client=client or httpx.AsyncClient(timeout=GENERATION_TIMEOUT,follow_redirects=False,trust_env=False)
        self.providers={'workbuddy_cn':WorkBuddyCN(self.client),'openai_compatible':Compatible(self.client)}
        self.locks=defaultdict(asyncio.Lock)
        self.sync_locks=defaultdict(asyncio.Lock)
        self.inflight=defaultdict(int)
        self.key_inflight=defaultdict(int)
        self.sticky=OrderedDict()
        self.sticky_limit=10000
        self.admission_lock=asyncio.Lock()
        self.login_lock=asyncio.Lock()
        self.oauth={}
        self.login_failures=defaultdict(list)
        self.stopping=False
        if not self.store.get('password'):
            initial=password or secrets.token_urlsafe(18)
            self.store.set('password',password_hash(initial))
            if not password:
                target=folder/'initial-admin.txt'
                target.write_text('管理员：admin\n首次密码：'+initial+'\n登录后请在系统设置修改密码。\n',encoding='utf-8')
                target.chmod(0o600)
        for key,value in {'sync_interval':900,'retention_days':30,'session_hours':12}.items():
            if self.store.get(key) is None:
                self.store.set(key,value)

    async def account(self, aid):
        result=(await self.store.aone('SELECT * FROM accounts WHERE id=?',(aid,)))
        if not result:
            raise HTTPException(404,'账号不存在')
        return result

    async def public_accounts(self):
        rows=(await self.store.aall('SELECT id,provider,uid,name,version,enabled,state,cooldown,max_concurrency,balance_micro,balance_at,balance_source,auth_expires,last_error FROM accounts ORDER BY id'))
        for row in rows:
            row['inflight']=self.inflight[row['id']]
            row['balance_stale']=not row['balance_at'] or time.time()-row['balance_at']>(await self.store.aget('sync_interval',900))*2
            row['groups']=[x['pool_id'] for x in (await self.store.aall('SELECT pool_id FROM pool_members WHERE account_id=?',(row['id'],)))]
            if not row['enabled']:
                row['state']='disabled'
            elif row['state']=='cooldown' and row['cooldown']<=time.time():
                row['state']='ready'
        return rows

    @serialized_admission
    async def add_account(self, name, provider, credential, pool_id=1):
        if provider=='workbuddy_cn':
            credential=normalize_credential(credential)
            uid=credential['account']['uid']
            expiry=credential['auth']['expiresAt']
        elif provider=='openai_compatible':
            endpoint=validate_endpoint(credential.get('endpoint',''))
            if not valid_token(credential.get('api_key')):
                raise ValueError('需要有效的上游 API Key')
            credential={'endpoint':endpoint,'api_key':credential['api_key']}
            uid=digest(endpoint+'|'+credential['api_key'])[:24]
            expiry=None
        else:
            raise ValueError('未知连接器')
        if not (await self.store.aone('SELECT id FROM pools WHERE id=?',(pool_id,))):
            raise ValueError('账号池不存在')
        def commit_add_account():
            with self.store.tx() as conn:
                existing=conn.execute('SELECT id FROM accounts WHERE provider=? AND uid=?',(provider,uid)).fetchone()
                if existing:
                    aid=existing['id']
                    if self.inflight[aid] or self.locks[aid].locked() or self.sync_locks[aid].locked():
                        raise ValueError('账号使用中，请稍后重新授权')
                    conn.execute("UPDATE accounts SET credential=?,version=version+1,name=?,auth_expires=?,state='ready',cooldown=0,last_error=NULL WHERE id=?",(self.vault.encrypt(credential),name,expiry,aid))
                else:
                    aid=conn.execute('INSERT INTO accounts(provider,uid,name,credential,auth_expires,created) VALUES(?,?,?,?,?,?)',
                                     (provider,uid,name,self.vault.encrypt(credential),expiry,time.time())).lastrowid
                conn.execute('INSERT OR IGNORE INTO pool_members VALUES(?,?)',(pool_id,aid))
            return aid
        aid=await self.store.run(commit_add_account)
        (await self.store.aaudit('account_authorized',aid))
        return aid

    async def credentials(self, aid, force=False, observed_version=None):
        async with self.locks[aid]:
            account=(await self.account(aid))
            credential=self.vault.decrypt(account['credential'])
            due=account['provider']=='workbuddy_cn' and (not account['auth_expires'] or account['auth_expires']<=time.time()+90)
            if force and observed_version is not None and account['version']!=observed_version:
                force=False
            if due or force:
                try:
                    credential=await self.providers[account['provider']].refresh(credential)
                except ProviderError as error:
                    if error.status in (401,403):
                        (await self.store.aexecute("UPDATE accounts SET state='reauth_required',last_error=? WHERE id=?",(error.code,aid)))
                    raise
                expiry=(credential.get('auth') or {}).get('expiresAt')
                (await self.store.aexecute("UPDATE accounts SET credential=?,version=version+1,auth_expires=?,state=CASE WHEN state='reauth_required' THEN 'ready' ELSE state END,last_error=NULL WHERE id=?",(self.vault.encrypt(credential),expiry,aid)))
            return credential

    async def metadata_call(self, aid, method):
        account=(await self.account(aid))
        credential=await self.credentials(aid)
        version=(await self.account(aid))['version']
        try:
            return await getattr(self.providers[account['provider']],method)(credential)
        except ProviderError as error:
            if error.status==401 and account['provider']=='workbuddy_cn':
                credential=await self.credentials(aid,True,version)
                return await getattr(self.providers[account['provider']],method)(credential)
            raise

    async def sync(self, aid):
        async with self.sync_locks[aid]:
            if self.inflight[aid]:
                raise HTTPException(409,'账号有进行中的请求，请稍后同步')
            errors=[]
            now=time.time()
            for kind in ('models','balance'):
                try:
                    data=await self.metadata_call(aid,kind)
                    def commit_sync():
                        with self.store.tx() as conn:
                            if kind=='models':
                                if not data:
                                    raise ProviderError('empty_model_catalog')
                                conn.execute('DELETE FROM models WHERE account_id=?',(aid,))
                                for model in data:
                                    conn.execute('INSERT INTO models VALUES(?,?,?,NULL)',(aid,model['id'],json.dumps(model['capabilities'])))
                            else:
                                conn.execute('DELETE FROM packages WHERE account_id=?',(aid,))
                                for package in data['packages']:
                                    conn.execute('INSERT INTO packages(account_id,package_id,name,remaining_micro,expires,observed,source,raw) VALUES(?,?,?,?,?,?,?,?)',
                                                 (aid,package['package_id'],package['name'],package['remaining_micro'],package['expires'],now,data['source'],package['raw']))
                                conn.execute('INSERT INTO snapshots(account_id,remaining_micro,observed,source) VALUES(?,?,?,?)',(aid,data['balance_micro'],now,data['source']))
                                conn.execute("UPDATE accounts SET balance_micro=?,balance_at=?,balance_source=?,state=CASE WHEN ?=0 THEN 'exhausted' WHEN state='exhausted' THEN 'ready' ELSE state END WHERE id=?",(data['balance_micro'],now,data['source'],data['balance_micro'],aid))
                    await self.store.run(commit_sync)
                    (await self.store.aexecute('INSERT INTO jobs(created,account_id,kind,state) VALUES(?,?,?,?)',(now,aid,kind,'success')))
                except (ProviderError,httpx.HTTPError) as error:
                    code=error.code if isinstance(error,ProviderError) else 'network_error'
                    errors.append(kind+': '+code)
                    (await self.store.aexecute('INSERT INTO jobs(created,account_id,kind,state,message) VALUES(?,?,?,?,?)',(now,aid,kind,'failed',code)))
                    (await self.store.aexecute('UPDATE accounts SET last_error=? WHERE id=?',(code,aid)))
            if not errors:
                (await self.store.aexecute('UPDATE accounts SET last_error=NULL WHERE id=?',(aid,)))
            return {'ok':not errors,'errors':errors}

    async def scheduler(self):
        last_cleanup=0
        while not self.stopping:
            try:
                now=time.time()
                interval=(await self.store.aget('sync_interval',900))
                for account in (await self.store.aall("SELECT id FROM accounts WHERE enabled=1 AND state!='reauth_required' AND (balance_at IS NULL OR balance_at<?)",(now-interval,))):
                    if self.inflight[account['id']] or self.sync_locks[account['id']].locked():
                        continue
                    recent=(await self.store.aone('SELECT created FROM jobs WHERE account_id=? ORDER BY id DESC LIMIT 1',(account['id'],)))
                    if recent and now-recent['created']<interval:
                        continue
                    await self.sync(account['id'])
                if now-last_cleanup>3600:
                    cutoff=now-(await self.store.aget('retention_days',30))*86400
                    def commit_scheduler():
                        with self.store.tx() as conn:
                            conn.execute('DELETE FROM requests WHERE created<? AND settled=1',(cutoff,))
                            conn.execute('DELETE FROM jobs WHERE created<?',(cutoff,))
                            conn.execute('DELETE FROM audit WHERE created<?',(cutoff,))
                            conn.execute('DELETE FROM snapshots WHERE observed<?',(cutoff,))
                            conn.execute('DELETE FROM sessions WHERE expires<?',(now,))
                    await self.store.run(commit_scheduler)
                    last_cleanup=now
                self.sticky=OrderedDict((k,v) for k,v in self.sticky.items() if v[1]>now)
                self.oauth={k:v for k,v in self.oauth.items() if v['expires']>now}
            except asyncio.CancelledError:
                raise
            except Exception:
                (await self.store.aexecute("INSERT INTO jobs(created,kind,state,message) VALUES(?,'scheduler','failed','scheduler_internal_error')",(time.time(),)))
            await asyncio.sleep(30)

    async def key(self, raw):
        key=(await self.store.aone('SELECT * FROM api_keys WHERE digest=?',(digest(raw),)))
        if not key or not key['enabled'] or (key['expires'] and key['expires']<=time.time()):
            raise HTTPException(401,'API Key 无效、已停用或已过期')
        return key

    async def pool_chain(self, pid):
        seen=set()
        pools=[]
        while pid and pid not in seen:
            pool=(await self.store.aone('SELECT * FROM pools WHERE id=?',(pid,)))
            if not pool:
                break
            seen.add(pid)
            pools.append(pool)
            pid=pool.get('fallback_pool_id')
        return pools

    async def key_models(self,key):
        allowed=json.loads(key['models'])
        rows=[]
        for pool in (await self.pool_chain(key['pool_id'])):
            rows+=(await self.store.aall('SELECT DISTINCT m.model FROM models m JOIN pool_members p ON p.account_id=m.account_id JOIN accounts a ON a.id=m.account_id WHERE p.pool_id=? AND a.enabled=1',(pool['id'],)))
        available={r['model'] for r in rows}
        for alias in (await self.store.aall('SELECT * FROM aliases')):
            if alias['model'] in available:
                available.add(alias['alias'])
        return sorted(m for m in available if not allowed or m in allowed)

    @serialized_admission
    async def acquire(self, key, model, session, body, exclude=None):
        return await self._acquire_pool(key,model,session,body,exclude=set(exclude or ()))

    async def _acquire_pool(self, key, model, session, body, pool=None, exclude=None):
        exclude=set(exclude or ())
        if pool is None:
            for candidate_pool in (await self.pool_chain(key['pool_id'])):
                try:
                    return (await self._acquire_pool(key,model,session,body,candidate_pool,exclude))
                except HTTPException as error:
                    if error.status_code!=503:
                        raise
            raise HTTPException(503,'主账号池与备用池均无可用账号')
        candidates=(await self.store.aall("SELECT a.*,m.capabilities,m.verified,MIN(p.expires) AS next_expiry FROM accounts a JOIN pool_members pm ON pm.account_id=a.id JOIN models m ON m.account_id=a.id LEFT JOIN packages p ON p.account_id=a.id AND p.remaining_micro>0 AND p.expires>? WHERE pm.pool_id=? AND m.model=? AND a.enabled=1 AND a.state IN ('ready','cooldown') AND a.cooldown<=? AND (a.balance_micro IS NULL OR a.balance_micro>0) GROUP BY a.id",(time.time(),pool['id'],model,time.time())))
        eligible=[]
        need_vision=any(isinstance(m.get('content'),list) and any(p.get('type')=='image_url' for p in m['content'] if isinstance(p,dict)) for m in body['messages'])
        for account in candidates:
            if account['id'] in exclude:
                continue
            caps=json.loads(account['capabilities'])
            if (body.get('tools') and not caps.get('tools')) or (need_vision and not caps.get('vision')):
                continue
            if self.inflight[account['id']]<account['max_concurrency'] and not self.sync_locks[account['id']].locked():
                eligible.append(account)
        if not eligible:
            raise HTTPException(503,'没有满足模型能力、余额、状态和并发要求的账号')
        sticky_key=(key['id'],digest(session)) if session else None
        preferred=self.sticky.get(sticky_key) if sticky_key else None
        def order(a):
            pinned=0 if preferred and preferred[1]>time.time() and preferred[0]==a['id'] else 1
            expiry=a['next_expiry'] or float('inf')
            # Random tie break avoids repeatedly preferring the first database row.
            return (pinned, expiry if pool['strategy']=='expiring' else self.inflight[a['id']],self.inflight[a['id']],secrets.randbelow(1000000))
        account=min(eligible,key=order)
        self.inflight[account['id']]+=1
        if sticky_key:
            self.sticky[sticky_key]=(account['id'],time.time()+3600)
            self.sticky.move_to_end(sticky_key)
            while len(self.sticky)>self.sticky_limit:
                self.sticky.popitem(last=False)
        return account

    @finish_owned
    async def reserve(self,key,rid,model,upstream_model,aid,endpoint=None):
        if self.key_inflight[key['id']]>=key['max_concurrency']:
            raise HTTPException(429,'API Key 并发已满',headers={'Retry-After':'1'})
        def commit_reserve():
            with self.store.tx() as conn:
                current=conn.execute('SELECT * FROM api_keys WHERE id=?',(key['id'],)).fetchone()
                if not current:
                    # Deleted between authentication and reservation.
                    raise HTTPException(401,'API Key 已被删除')
                if current['reserved']>=current['max_concurrency']:
                    raise HTTPException(429,'API Key 并发已满',headers={'Retry-After':'1'})
                recent=conn.execute('SELECT COUNT(*) FROM requests WHERE key_id=? AND created>?',(key['id'],time.time()-60)).fetchone()[0]
                if not current['enabled'] or (current['expires'] and current['expires']<=time.time()):
                    raise HTTPException(401,'API Key 已失效')
                if recent>=current['rpm']:
                    raise HTTPException(429,'API Key 速率限制',headers={'Retry-After':'60'})
                if current['request_limit'] is not None and current['used']+current['reserved']>=current['request_limit']:
                    raise HTTPException(429,'本地请求预算已用尽')
                conn.execute('UPDATE api_keys SET reserved=reserved+1 WHERE id=?',(key['id'],))
                conn.execute("INSERT INTO requests(id,created,key_id,account_id,model,upstream_model,state,endpoint) VALUES(?,?,?,?,?,?,'pending',?)",(rid,time.time(),key['id'],aid,model,upstream_model,endpoint))
        await self.store.run(commit_reserve)
        self.key_inflight[key['id']]+=1

    @finish_owned
    async def settle(self,rid,key,aid,state,status,started,usage,ttft,error=None,charge=1):
        def token_count(value):
            if isinstance(value,int) and not isinstance(value,bool) and 0<=value<=2**62:
                return value
            return None
        def cache_tokens(value):
            details=value.get('prompt_tokens_details')
            candidates=[details.get('cached_tokens') if isinstance(details,dict) else None,
                        value.get('prompt_cache_hit_tokens'),value.get('cached_tokens')]
            for candidate in candidates:
                number=token_count(candidate)
                if number is not None:
                    return number
            return None
        def commit_settle():
            with self.store.tx() as conn:
                changed=conn.execute('UPDATE requests SET state=?,status=?,input_tokens=?,output_tokens=?,cache_tokens=?,credits_micro=?,usage_source=?,latency_ms=?,ttft_ms=?,error=?,settled=1 WHERE id=? AND settled=0',
                                    (state,status,token_count(usage.get('prompt_tokens')),token_count(usage.get('completion_tokens')),cache_tokens(usage),micros(usage.get('credits',usage.get('credit'))),
                                     'upstream' if usage else 'unknown',int((time.monotonic()-started)*1000),ttft,error,rid)).rowcount
                if changed:
                    conn.execute('UPDATE api_keys SET reserved=MAX(0,reserved-1),used=used+? WHERE id=?',(charge,key['id']))
                    conn.execute('INSERT INTO ledger VALUES(?,?,?,?,?)',(rid,key['id'],charge,state,time.time()))
            return changed
        changed=await self.store.run(commit_settle)
        if changed:
            self.inflight[aid]=max(0,self.inflight[aid]-1)
            self.key_inflight[key['id']]=max(0,self.key_inflight[key['id']]-1)

    async def gateway(self,key,body,session='',endpoint='chat'):
        requested=body['model']
        allowed=json.loads(key['models'])
        if allowed and requested not in allowed:
            raise HTTPException(403,'此 API Key 不允许使用该模型')
        alias=(await self.store.aone('SELECT model FROM aliases WHERE alias=?',(requested,)))
        model=alias['model'] if alias else requested
        account=(await self.acquire(key,model,session,body))
        aid=account['id']
        # Sent exactly as requested; the only reason to rewrite the rung is an
        # explicit upstream 400/11150 rejection, handled inside the attempt loop.
        caps=json.loads(account['capabilities'])
        payload={**body,'model':model}
        # Stateless-only fields are accepted by the API layer but must never reach
        # the upstream: it would either reject them or start storing conversations.
        payload.pop('store',None)
        payload.pop('metadata',None)
        rid='req_'+uuid.uuid4().hex
        started=time.monotonic()
        try:
            (await self.reserve(key,rid,requested,model,aid,endpoint))
        except BaseException:
            # Cancellation can arrive after the reservation commit. The owned
            # reserve completes first; release that reservation without dispatch.
            if await self.store.aone('SELECT id FROM requests WHERE id=?',(rid,)):
                await self.settle(rid,key,aid,'failed',499,started,{},None,'cancelled_before_dispatch',charge=0)
            else:
                self.inflight[aid]-=1
            raise
        response=None
        sent=False
        try:
            credential=await self.credentials(aid)
            provider=self.providers[account['provider']]
            url,headers,forwarded=provider.request(credential,payload)
            version=(await self.account(aid))['version']
            fallback_used=False
            # Generation retries are limited to explicit 401 refreshes, one safe
            # pre-generation retry, and one alternate-account attempt for a
            # persistent channel block.
            for attempt in range(2):
                sent=True
                response=await self.client.send(self.client.build_request('POST',url,headers=headers,json=forwarded),stream=True)
                attempt_id=(await self.store.aexecute('INSERT INTO attempts(request_id,account_id,created,status) VALUES(?,?,?,?)',(rid,aid,time.time(),response.status_code)))
                if response.status_code==401 and attempt==0 and account['provider']=='workbuddy_cn':
                    await response.aclose()
                    response=None
                    credential=await self.credentials(aid,True,version)
                    url,headers,forwarded=provider.request(credential,payload)
                    continue
                # Two upstream rejections are worth exactly one retry, because
                # nothing has been generated yet and neither is a real answer:
                #   * 11150 — reasoning rung unsupported by this model (降一档)
                #   * 11-128 — risk control intermittently blocks the channel;
                #     re-sending the identical payload usually succeeds.
                if response.status_code==400 and attempt==0:
                    try:
                        detail=json.loads(await response.aread() or b'{}')
                    except ValueError:
                        detail={}
                    upstream_code=str(detail.get('code')) if isinstance(detail,dict) else ''
                    if upstream_code=='11150' and isinstance(payload.get('reasoning_effort'),str):
                        lowered=pick_effort(payload['reasoning_effort'],caps.get('efforts') or [])
                        if lowered and lowered!=payload['reasoning_effort']:
                            await response.aclose()
                            response=None
                            payload={**payload,'reasoning_effort':lowered}
                            url,headers,forwarded=provider.request(credential,payload)
                            continue
                    # Any other 400 has produced nothing and cost nothing, so one
                    # identical retry is always safe. The channel block (11-128) is
                    # intermittent, and the exact code spelling varies, so matching
                    # on the code string alone proved too brittle.
                    (await self.store.aexecute('INSERT INTO jobs(created,account_id,kind,state,message) VALUES(?,?,?,?,?)',
                                               (time.time(),aid,'upstream_400','retry',upstream_code[:80] or 'unparsed')))
                    await response.aclose()
                    response=None
                    await asyncio.sleep(0.8)
                    continue
                break
            if response.status_code>=300:
                code=response.status_code
                retry=response.headers.get('retry-after','60')
                retry=int(retry) if retry.isdigit() else 60
                # Persist the upstream's own code/message so a 4xx is diagnosable
                # without guessing; request content is never included.
                try:
                    raw=await response.aread()
                    reason=upstream_error_detail(raw)
                    detail=json.loads(raw or b'{}')
                except Exception:
                    reason=''
                    detail={}
                upstream_code=str(detail.get('code')) if isinstance(detail,dict) else ''
                is_channel_error=(code==400 and
                                  (upstream_code.replace('-','')=='11128' or
                                   'unapproved channel' in reason.lower()))
                if is_channel_error:
                    shape=[]
                    for item in forwarded.get('messages',[]):
                        if not isinstance(item,dict):
                            shape.append({'type':type(item).__name__})
                            continue
                        content=item.get('content')
                        shape.append({'role':item.get('role'),
                                      'keys':sorted(str(k) for k in item),
                                      'content_kind':('list' if isinstance(content,list) else
                                                      'string' if isinstance(content,str) else
                                                      'null' if content is None else type(content).__name__),
                                      'content_len':len(content) if isinstance(content,(str,list)) else None,
                                      'tool_calls':len(item.get('tool_calls') or []) if isinstance(item.get('tool_calls'),list) else 0})
                    tools_shape=[]
                    for tool in forwarded.get('tools',[]):
                        if isinstance(tool,dict):
                            function=tool.get('function') if isinstance(tool.get('function'),dict) else {}
                            tools_shape.append({'type':tool.get('type'),
                                                'keys':sorted(str(k) for k in tool),
                                                'function_keys':sorted(str(k) for k in function),
                                                'name':function.get('name')})
                        else:
                            tools_shape.append({'type':type(tool).__name__})
                    diagnostic=json.dumps({'top_keys':sorted(str(k) for k in forwarded),
                                           'message_count':len(forwarded.get('messages',[])),
                                           'tools':tools_shape,'messages':shape},ensure_ascii=False)[:3500]
                    (await self.store.aexecute('INSERT INTO jobs(created,account_id,kind,state,message) VALUES(?,?,?,?,?)',
                                               (time.time(),aid,'channel_shape','diagnostic',diagnostic)))
                channel_block=(code==400 and attempt==1 and not fallback_used and
                               (upstream_code.replace('-','')=='11128' or
                                'unapproved channel' in reason.lower()))
                if channel_block:
                    try:
                        alternate=await self.acquire(key,model,session,body,exclude={aid})
                    except HTTPException:
                        alternate=None
                    if alternate:
                        previous_aid=aid
                        aid=alternate['id']
                        account=alternate
                        caps=json.loads(account['capabilities'])
                        await self.store.aexecute('UPDATE requests SET account_id=? WHERE id=? AND settled=0',(aid,rid))
                        self.inflight[previous_aid]=max(0,self.inflight[previous_aid]-1)
                        provider=self.providers[account['provider']]
                        credential=await self.credentials(aid)
                        version=(await self.account(aid))['version']
                        url,headers,forwarded=provider.request(credential,payload)
                        await response.aclose()
                        response=await self.client.send(self.client.build_request('POST',url,headers=headers,json=forwarded),stream=True)
                        attempt_id=(await self.store.aexecute('INSERT INTO attempts(request_id,account_id,created,status) VALUES(?,?,?,?)',(rid,aid,time.time(),response.status_code)))
                        fallback_used=True
                        if response.status_code<300:
                            pass
                        else:
                            code=response.status_code
                            retry=response.headers.get('retry-after','60')
                            retry=int(retry) if retry.isdigit() else 60
                            try:
                                reason=upstream_error_detail(await response.aread())
                            except Exception:
                                reason=''
                if response.status_code>=300:
                    if reason:
                        (await self.store.aexecute('UPDATE attempts SET error=? WHERE id=?',(reason,attempt_id)))
                    if code in (401,403):
                        (await self.store.aexecute("UPDATE accounts SET state='reauth_required',last_error=? WHERE id=?",('upstream_http_'+str(code),aid)))
                    elif code==429 or code>=500:
                        (await self.store.aexecute("UPDATE accounts SET state='cooldown',cooldown=?,last_error=? WHERE id=?",(time.time()+max(1,min(retry,86400)),'upstream_http_'+str(code),aid)))
                    elif code==402:
                        (await self.store.aexecute("UPDATE accounts SET state='exhausted',last_error='upstream_quota' WHERE id=?",(aid,)))
                    await response.aclose()
                    (await self.settle(rid,key,aid,'failed',code,started,{},None,'upstream_http_'+str(code),0 if code in (400,401,402,403,404,429) else 1))
                    raise HTTPException(code if 400<=code<600 else 502,
                                        '上游拒绝请求；详见请求日志'+(f'（上游 {reason[:80]}）' if reason else ''),
                                        headers={'X-Request-Id':rid,'Retry-After':str(retry)})
        except HTTPException:
            raise
        except BaseException as error:
            if response:
                await response.aclose()
            code=error.code if isinstance(error,ProviderError) else type(error).__name__
            (await self.store.aexecute('INSERT INTO attempts(request_id,account_id,created,error) VALUES(?,?,?,?)',(rid,aid,time.time(),code)))
            (await self.settle(rid,key,aid,'interrupted' if sent else 'failed',502,started,{},None,code,1 if sent else 0))
            if isinstance(error,asyncio.CancelledError):
                raise
            raise HTTPException(502,'连接上游失败；请查看脱敏日志',headers={'X-Request-Id':rid}) from None

        usage={}
        ttft=None
        completed=False
        finish=False
        failure=None
        @finish_owned
        async def finalize():
            await response.aclose()
            (await self.settle(rid,key,aid,'success' if completed else 'interrupted',200 if completed else 502,started,usage,ttft,failure or (None if completed else 'client_cancelled')))
        async def chunks():
            nonlocal usage,ttft,completed,finish
            def normalize_chunk(event):
                event['model']=requested
                for choice in event.get('choices',[]):
                    delta=choice.get('delta') or choice.pop('message',{})
                    if not isinstance(delta,dict):
                        raise ProviderError('invalid_chat_delta')
                    if 'reasoning_content' not in delta and isinstance(delta.get('reasoning'),str):
                        delta['reasoning_content']=delta['reasoning']
                    choice['delta']=delta
                return event
            if 'text/event-stream' not in response.headers.get('content-type',''):
                data=bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data)>8*1024*1024:
                        raise ProviderError('response_too_large')
                obj=json.loads(data)
                if not isinstance(obj,dict) or obj.get('error') or not obj.get('choices'):
                    raise ProviderError('invalid_chat_response')
                usage=obj.get('usage') or {}
                for choice in obj['choices']:
                    choice['delta']=choice.pop('message',{})
                    for index,call in enumerate(choice['delta'].get('tool_calls') or []):
                        call.setdefault('index',index)
                obj['object']='chat.completion.chunk'
                ttft=int((time.monotonic()-started)*1000)
                yield normalize_chunk(obj)
                completed=True
                return
            async for event in sse_events(response):
                if event is None:
                    if not finish:
                        raise ProviderError('missing_finish_reason')
                    completed=True
                    return
                if ttft is None and event.get('choices'):
                    ttft=int((time.monotonic()-started)*1000)
                if isinstance(event.get('usage'),dict):
                    usage=event['usage']
                for choice in event.get('choices',[]):
                    if choice.get('finish_reason') is not None:
                        finish=True
                yield normalize_chunk(event)
            # EOF without the terminal marker is ambiguous, even with HTTP 200.
            raise ProviderError('stream_interrupted')

        async def stream():
            nonlocal failure
            try:
                async for chunk in chunks():
                    yield 'data: '+json.dumps(chunk,ensure_ascii=False)+'\n\n'
                yield 'data: [DONE]\n\n'
            except asyncio.CancelledError:
                failure='client_cancelled'
                raise
            except Exception as error:
                failure=error.code if isinstance(error,ProviderError) else 'stream_parse_error'
                yield 'data: '+json.dumps({'error':{'message':'上游流中断','code':failure},'request_id':rid})+'\n\n'
            finally:
                import anyio
                with anyio.CancelScope(shield=True):
                    await finalize()
        if body.get('stream'):
            return OwnedStreamingResponse(stream(),finalize=finalize,media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no','X-Request-Id':rid})
        content=[]
        reasoning=[]
        tools={}
        finish_reason=None
        size=0
        failure=None
        try:
            async for chunk in chunks():
                for choice in chunk.get('choices',[]):
                    delta=choice.get('delta') or {}
                    for field,target in (('content',content),('reasoning_content',reasoning)):
                        if isinstance(delta.get(field),str):
                            target.append(delta[field])
                            size+=len(delta[field].encode())
                    for call in delta.get('tool_calls') or []:
                        index=call.get('index',0)
                        item=tools.setdefault(index,{'id':'','type':'function','function':{'name':'','arguments':''}})
                        if call.get('id'):
                            item['id']=call['id']
                        for field in ('name','arguments'):
                            value=call.get('function',{}).get(field,'')
                            item['function'][field]+=value
                            size+=len(value.encode())
                    finish_reason=choice.get('finish_reason') or finish_reason
                    if size>8*1024*1024:
                        raise ProviderError('response_too_large')
            message={'role':'assistant','content':''.join(content) or None}
            if reasoning:
                message['reasoning_content']=''.join(reasoning)
            if tools:
                message['tool_calls']=[tools[k] for k in sorted(tools)]
            result={'id':rid,'object':'chat.completion','created':int(time.time()),'model':requested,
                    'choices':[{'index':0,'message':message,'finish_reason':finish_reason}]}
            if usage:
                result['usage']=usage
            (await self.store.aexecute('UPDATE models SET verified=? WHERE account_id=? AND model=?',(time.time(),aid,model)))
            return JSONResponse(result,headers={'X-Request-Id':rid})
        except asyncio.CancelledError:
            failure='client_cancelled'
            raise
        except Exception as error:
            failure=error.code if isinstance(error,ProviderError) else 'stream_parse_error'
            raise HTTPException(502,'上游响应不完整',headers={'X-Request-Id':rid}) from None
        finally:
            await response.aclose()
            (await self.settle(rid,key,aid,'success' if completed else 'interrupted',200 if completed else 502,started,usage,ttft,failure))
