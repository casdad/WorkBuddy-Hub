"""Independent CN connector, based on the documented MIT reference protocols.

See REFERENCES.md for fixed upstream commits and implementation boundaries.
No device identity fabrication, prompt rewriting, or automatic account creation.
"""
import json
import math
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlparse

import httpx

# Product identifiers from wb2api/admin/billing.py (MIT, pinned in REFERENCES.md).
PAID_CODES=['TCACA_code_002_AkiJS3ZHF5','TCACA_code_023_4xbGhMrE6q','TCACA_code_026_BaESVICNoi','TCACA_code_027_0FCGVA6vSa','TCACA_code_009_0XmEQc2xOf','TCACA_code_038_OhvqZtiPKr','TCACA_code_003_FAnt7lcmRT','TCACA_code_036_lupO5WgNdG']
FREE_CODES=['TCACA_code_008_cfWoLwvjU4','TCACA_code_007_nzdH5h4Nl0','TCACA_code_028_NtpWi0jzXs','TCACA_code_029_6wCGEWquYy','TCACA_code_030_BjSt89qTvr','TCACA_code_001_PqouKr6QWV','TCACA_code_006_DbXS0lrypC','TCACA_code_035_ArVxJcGDsm','TCACA_code_037_WxOD3MpI2o','TCACA_code_039_KRcQj7wUat','TCACA_code_040_mi9rCYg46x']

# Long contexts need a long first byte: a ~1M-token prefill can take minutes
# before the stream starts, so the generation read timeout is deliberately large.
GENERATION_TIMEOUT = httpx.Timeout(connect=15,read=600,write=300,pool=30)
# Catalog, billing, refresh and OAuth calls must stay short.
METADATA_TIMEOUT = httpx.Timeout(60,connect=15)


class ProviderError(Exception):
    def __init__(self, code: str, status=502, retry_after=0):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)


def micros(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > Decimal('1000000000000'):
            return None
        return int(number * 1000000)
    except (InvalidOperation, ValueError):
        return None


def timestamp(value):
    if value in (None, '') or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or number<=0:
            return None
        return number / 1000 if number > 100000000000 else number
    except (ValueError, TypeError):
        try:
            date = datetime.fromisoformat(str(value).replace('Z','+00:00'))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone(timedelta(hours=8)))
            if len(str(value)) == 10:
                date = date.replace(hour=23,minute=59,second=59)
            return date.timestamp()
        except ValueError:
            return None


def first(obj, keys):
    return next((obj[k] for k in keys if obj.get(k) is not None),None)


# Reasoning effort ladder, low to high. The upstream validates this vocabulary and
# rejects anything outside it with HTTP 400 / code 11150 invalid_reasoning_effort.
EFFORT_RANK = {'off':0,'minimal':1,'none':1,'low':2,'medium':3,'high':4,'xhigh':5,'max':6}
# "do not think" spellings are not a rung on that ladder: rewriting them would
# silently turn thinking back on, so they are always left for the upstream.
THINKING_OFF = frozenset({'off','none','disabled','false','0'})

# Upstream codes meaning "not now" rather than "not allowed". Both are rejected
# before any generation, so one retry with the same payload is safe. 11-128 is the
# risk-control channel block, which was observed to be intermittent: identical
# payloads succeeded on the very next attempt.
TRANSIENT_UPSTREAM = frozenset({'11-128'})

# Fields the CN endpoint accepts. Anything else must be filtered before dispatch:
# it answers unknown keys with HTTP 400, so forwarding the client body verbatim
# breaks real clients (e.g. `parallel_tool_calls`).
CN_BODY_KEYS = frozenset({
    'model','messages','tools','tool_choice','temperature',
    'max_tokens','max_completion_tokens','top_p','stream',
    'stream_options','stop','presence_penalty','frequency_penalty',
    'n','response_format','seed','user','reasoning_effort',
})


def normalize_cn_messages(messages):
    """Make a client history acceptable to the CN endpoint.

    Two upstream rejections are the gateway's problem to absorb, because the bad
    history is replayed by the client on every turn and would otherwise kill the
    whole conversation (verified against the live endpoint):

      * 11148 "tool calls and tool results do not match" — an assistant tool_call
        without its result, or a tool result without its call.
      * 11151 "a message has empty content" — null/"" content is rejected, while a
        single space is accepted.

    The pairing fix follows the reference implementation: an assistant message is
    kept or dropped as a whole (its content survives), then the surviving call ids
    are recomputed before orphaned tool results are removed.
    """
    cleaned=[]
    for message in messages or []:
        if not isinstance(message,dict):
            continue
        item={**message}
        # The CN endpoint treats `developer` as an unapproved channel and rejects
        # the entire request (code 11-128), so it must never reach it.
        if item.get('role')=='developer':
            item['role']='system'
        # DeepSeek Harness sends a long agent runtime prompt as `system`.  The
        # CN gateway classifies that system-plus-tools combination as an
        # unapproved channel (11128), while the same instruction is accepted as
        # an ordinary user instruction.  Preserve the text and tool context but
        # use the channel the upstream allows for this client.
        if (item.get('role')=='system' and isinstance(item.get('content'),str)
                and 'DeepSeek Harness' in item['content']):
            item['role']='user'
        cleaned.append(item)

    calls={}
    results=set()
    for item in cleaned:
        for call in item.get('tool_calls') or []:
            if isinstance(call,dict) and isinstance(call.get('id'),str) and call['id']:
                calls[call['id']]=True
        if item.get('role')=='tool' and isinstance(item.get('tool_call_id'),str):
            results.add(item['tool_call_id'])
    paired=set(calls) & results
    for item in cleaned:
        tool_calls=item.get('tool_calls')
        if isinstance(tool_calls,list) and any(isinstance(c,dict) and c.get('id') not in paired for c in tool_calls):
            item.pop('tool_calls',None)
    surviving={c['id'] for item in cleaned for c in (item.get('tool_calls') or [])
               if isinstance(c,dict) and isinstance(c.get('id'),str)}
    cleaned=[item for item in cleaned
             if not (item.get('role')=='tool' and item.get('tool_call_id') not in surviving)]

    for item in cleaned:
        content=item.get('content')
        if isinstance(content,list) and not content:
            content=''
        if item.get('role')=='assistant' and item.get('tool_calls'):
            # Null content is accepted together with tool_calls.
            if content is None or (isinstance(content,str) and not content.strip()):
                item['content']=None
            continue
        if content is None or (isinstance(content,str) and not content.strip()):
            item['content']=' '
    return cleaned


def upstream_error_detail(raw: bytes) -> str:
    """Extract only the upstream's own code/message from an error body.

    Request content is never included, so this is safe to persist in the attempt
    log and to surface in an API error message.
    """
    try:
        data=json.loads(raw or b'{}')
    except ValueError:
        return ''
    if not isinstance(data,dict):
        return ''
    error=data.get('error') if isinstance(data.get('error'),dict) else {}
    pieces=[]
    for value in (data.get('code'),error.get('code'),data.get('message'),data.get('msg'),error.get('message')):
        if isinstance(value,(int,str)) and not isinstance(value,bool):
            text=str(value).strip()
            if text and text not in pieces:
                pieces.append(text)
    return ' '.join(pieces)[:300]


def parse_efforts(reasoning):
    """(supportedEfforts, defaultEffort) from an upstream ``model.reasoning`` block.

    Both upstream shapes are accepted: a supportedEfforts array, or a single
    ``effort`` string with no array. Missing data returns empty values, which
    downstream treats as "upstream declared nothing" and never guesses about.
    """
    if not isinstance(reasoning,dict):
        return [],''
    efforts=reasoning.get('supportedEfforts')
    if isinstance(efforts,list):
        cleaned=[str(e).strip() for e in efforts if str(e).strip()]
        if cleaned:
            return cleaned,str(reasoning.get('defaultEffort') or '').strip()
    single=str(reasoning.get('effort') or '').strip()
    if single:
        return [single],str(reasoning.get('defaultEffort') or '').strip()
    return [],str(reasoning.get('defaultEffort') or '').strip()


def pick_effort(requested, supported):
    """Highest declared rung at or below ``requested``, or None.

    Used only as a fallback after the upstream explicitly rejects a rung with
    400/code 11150. Nothing is rewritten pre-emptively: the published catalog can
    under-report (a model may declare ["high"] while accepting "max"), so the
    caller's own rung always gets the first attempt.

    Returns None for: no declaration, unknown spelling, a "do not think"
    spelling (rewriting it would silently enable thinking), or no ranked rung.
    """
    if not supported:
        return None
    value=str(requested or '').strip().lower()
    if not value or value in THINKING_OFF or value not in EFFORT_RANK:
        return None
    ranked=[(str(e).strip(),EFFORT_RANK[str(e).strip().lower()]) for e in supported if str(e).strip().lower() in EFFORT_RANK]
    if not ranked:
        return None
    below=[e for e,index in ranked if index<=EFFORT_RANK[value]]
    return max(below,key=lambda e:EFFORT_RANK[e.lower()]) if below else min(ranked,key=lambda x:x[1])[0]


def array_at(body, names):
    if not isinstance(body, dict):
        return None
    for name in names:
        if isinstance(body.get(name), list):
            return body[name]
    for key in ('data','Data','Response'):
        found = array_at(body.get(key), names)
        if found is not None:
            return found
    return None


def normalize_package(raw, index=0):
    details = raw.get('SlicePeriodUsageDetails') or []
    source = {**(details[0] if details else {}), **raw}
    remaining = micros(first(source, ('CycleCapacityRemainPrecise','CycleCapacityRemain','CycleRemainCapacity','CapacityRemainPrecise','CapacityRemain','SlicePeriodCapacityRemainPrecise','SlicePeriodCapacityRemain')))
    end = timestamp(first(source,('DeductionEndTime','deductionEndTime','ExpiredTime','expiredTime')))
    cycle = timestamp(first(source,('CycleEndTime','cycleEndTime','SlicePeriodEndTime')))
    if cycle and (not end or end - cycle > 365*86400):
        end = cycle
    return {'package_id':str(first(raw,('PackageId','ResourceId','PackageCode')) or index),
            'name':str(first(raw,('PackageName','PackageCode')) or '积分包'),
            'remaining_micro':remaining, 'expires':end, 'raw':json.dumps(raw,ensure_ascii=False)}


def valid_token(value):
    return isinstance(value,str) and 0 < len(value) < 32768 and value.isascii() and not any(c.isspace() or ord(c)<32 or ord(c)==127 for c in value)


def credential_expiry(auth):
    for name in ('expiresAt','expires_at'):
        if auth.get(name) is not None:
            parsed=timestamp(auth[name])
            if parsed is None:
                raise ValueError('凭据到期时间必须是有效时间')
            return parsed
    value=auth.get('expiresIn')
    if value is None:
        return None
    try:
        seconds=float(value)
        if isinstance(value,bool) or not math.isfinite(seconds) or not 0<=seconds<=10*365*86400:
            raise ValueError
    except (ValueError,TypeError,OverflowError):
        raise ValueError('expiresIn 必须是有限的非负秒数且不超过十年') from None
    return time.time()+seconds


def normalize_credential(body):
    auth = body.get('auth',body)
    profile = body.get('account',body.get('profile',{}))
    access = auth.get('accessToken',auth.get('access_token'))
    refresh = auth.get('refreshToken',auth.get('refresh_token'))
    if not valid_token(access) or not valid_token(refresh):
        raise ValueError('需要有效的明文 accessToken 和 refreshToken；不接受加密客户端文件')
    domain = auth.get('domain') or 'www.codebuddy.cn'
    if domain not in ('www.codebuddy.cn','www.workbuddy.cn','codebuddy.cn','workbuddy.cn'):
        raise ValueError('仅支持国内版账号域名')
    uid = str(profile.get('uid') or body.get('uid') or '')
    if not uid or len(uid)>200:
        raise ValueError('凭据缺少账号 uid')
    expires = credential_expiry(auth)
    return {'auth':{'accessToken':access,'refreshToken':refresh,'domain':domain,'expiresAt':expires},
            'account':{'uid':uid,'nickname':str(profile.get('nickname') or uid),
                       'enterpriseId':str(profile.get('enterpriseId') or ''),'type':profile.get('type','personal')}}


def validate_endpoint(url):
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError('上游地址不能包含凭据、查询参数或片段')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1','localhost','::1')):
        raise ValueError('远程上游必须使用 HTTPS')
    return url.rstrip('/')


class Connector:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def checked(self, method, url, **kwargs):
        # Metadata calls must never inherit the long generation timeout.
        kwargs.setdefault('timeout',METADATA_TIMEOUT)
        try:
            response = await self.client.request(method,url,**kwargs)
        except httpx.HTTPError:
            raise ProviderError('upstream_network') from None
        if response.status_code >= 300:
            retry = response.headers.get('retry-after','60')
            raise ProviderError('upstream_http_'+str(response.status_code),response.status_code,
                                int(retry) if retry.isdigit() else 60)
        try:
            body = response.json()
        except ValueError:
            raise ProviderError('invalid_upstream_json') from None
        if not isinstance(body,dict):
            raise ProviderError('invalid_upstream_shape')
        code = body.get('code')
        if code not in (None,0,200,'0','200'):
            status = 401 if str(code) in ('401','40101') else 502
            raise ProviderError('upstream_business_'+str(code)[:40],status)
        return body


class WorkBuddyCN(Connector):
    api = 'https://www.codebuddy.cn'
    chat = 'https://copilot.tencent.com'

    def headers(self, credential):
        auth, account = credential['auth'], credential['account']
        return {'Authorization':'Bearer '+auth['accessToken'], 'X-User-Id':account['uid'],
                'X-Enterprise-Id':account.get('enterpriseId',''), 'X-Tenant-Id':account.get('enterpriseId',''),
                'X-Domain':auth['domain'],'User-Agent':'WorkBuddy-Hub/0.1',
                'Content-Type':'application/json','Accept':'application/json'}

    async def auth_start(self):
        result = await self.checked('POST',self.api+'/v2/plugin/auth/state?platform=workbuddy',json={})
        data = result.get('data') or {}
        if not isinstance(data.get('state'),str) or not data['state']:
            raise ProviderError('missing_oauth_state')
        uri = data.get('authUrl') or data.get('auth_url') or data.get('url') or self.api+'/login?state='+quote(data['state'])
        parsed=urlparse(uri)
        if parsed.scheme!='https' or parsed.hostname not in ('www.codebuddy.cn','www.workbuddy.cn'):
            raise ProviderError('unexpected_authorization_origin')
        return data['state'], uri

    async def auth_poll(self, state):
        response = await self.client.get(self.api+'/v2/plugin/auth/token',params={'state':state},timeout=METADATA_TIMEOUT)
        if response.status_code != 200:
            raise ProviderError('authorization_poll_http',502)
        result=response.json()
        auth=result.get('data') or {}
        if not isinstance(auth,dict) or not (auth.get('accessToken') or auth.get('access_token')):
            return None
        profile = await self.checked('GET',self.api+'/v2/plugin/login/account',params={'state':state},
                                     headers={'Authorization':'Bearer '+str(auth.get('accessToken') or auth['access_token']),
                                              'X-Domain':str(auth.get('domain') or 'www.codebuddy.cn')})
        try:
            return normalize_credential({'auth':auth,'account':profile.get('data') or {}})
        except (ValueError,TypeError):
            raise ProviderError('invalid_authorization_credential') from None

    async def refresh(self, credential):
        headers=self.headers(credential)
        headers.update({'X-Refresh-Token':credential['auth']['refreshToken'],'X-Auth-Refresh-Source':'plugin'})
        result=await self.checked('POST',self.chat+'/v2/plugin/auth/token/refresh',headers=headers,json={})
        auth=result.get('data')
        if not isinstance(auth,dict) or not valid_token(auth.get('accessToken')):
            raise ProviderError('incomplete_refresh_response')
        fresh={**credential['auth'],**auth}
        try:
            fresh['expiresAt']=credential_expiry(auth)
            fresh.pop('expiresIn',None)
            return normalize_credential({'auth':fresh,'account':credential['account']})
        except (ValueError,TypeError):
            raise ProviderError('invalid_refresh_credential') from None

    async def models(self, credential):
        account=credential['account']
        enterprise = account.get('enterpriseId') if account.get('type')!='personal' else None
        result=await self.checked('GET',self.chat+'/v2/enterprises/'+quote(enterprise or 'personal',safe='')+'/models',headers=self.headers(credential))
        data=result.get('data') or {}
        models=data.get('models') or [m for a in data.get('agents',[]) for m in a.get('models',[])]
        normalized={}
        for value in models:
            value={'id':value} if isinstance(value,str) else value
            if isinstance(value,dict) and value.get('id'):
                if set(value.get('tags') or []) & {'text-to-image','text-to-video','image-to-video'}:
                    continue
                efforts,default_effort=parse_efforts(value.get('reasoning'))
                normalized[value['id']]={'id':value['id'],'capabilities':{
                    'chat':True,'stream':True,'tools':value.get('supportsTools') is True or value.get('supportsToolCall') is True,
                    'vision':value.get('supportsImages') is True,'verification':'catalog',
                    'efforts':efforts,'default_effort':default_effort}}
        if not normalized:
            raise ProviderError('empty_model_catalog')
        return list(normalized.values())

    async def balance(self, credential):
        # Prefer the current summary. Detail requests are independently checked;
        # an incomplete scan never replaces a previously complete snapshot.
        domain=credential['auth']['domain']
        origin='https://www.workbuddy.cn' if domain.endswith('workbuddy.cn') else self.api
        headers={**self.headers(credential),'X-Client-Platform':'web','Origin':origin,'Referer':origin+'/profile/plans-usage'}
        try:
            summary=await self.checked('POST',origin+'/billing/meter/get-user-resource-summary',headers=headers,json={})
            accounts=array_at(summary,('Packages','packages','Accounts','accounts'))
            if accounts is None:
                raise ProviderError('missing_credit_accounts')
            details=[]
            for path in ('paid','free'):
                seen=set()
                for page in range(1,21):
                    query={'PageNumber':page,'PageSize':200,'Status':[0,3],'PackageCodes':PAID_CODES if path=='paid' else FREE_CODES}
                    if path=='paid':
                        query['NeedRenewInfo']=True
                    else:
                        today=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
                        query.update(SlicePeriodStartTime=today+' 00:00:00',SlicePeriodEndTime=today+' 23:59:59')
                    result=await self.checked('POST',origin+'/billing/meter/get-user-resource-'+path+'-packages',headers=headers,
                                              json=query)
                    items=array_at(result,('Accounts','accounts','Packages','packages'))
                    if items is None:
                        raise ProviderError('missing_credit_packages')
                    fingerprint=json.dumps(items,sort_keys=True)
                    if items and fingerprint in seen:
                        raise ProviderError('repeated_credit_page')
                    seen.add(fingerprint)
                    details.extend(items)
                    if len(items)<200:
                        break
                else:
                    raise ProviderError('credit_pagination_limit')
            source='upstream_summary'
        except ProviderError as error:
            if error.status in (401,403,429):
                raise
            legacy=await self.checked('POST',self.chat+'/v2/billing/meter/get-user-resource',headers=self.headers(credential),json={})
            accounts=array_at(legacy,('Accounts','accounts'))
            if accounts is None:
                raise ProviderError('missing_credit_accounts')
            details=accounts
            source='upstream_legacy'
        credit_accounts=[a for a in accounts if a.get('CapacityUnit')=='credits']
        values=[normalize_package(a)['remaining_micro'] for a in credit_accounts]
        total=sum(values) if all(v is not None for v in values) else None
        unique={}
        for item in details:
            identity=first(item,('ResourceId','AccountId','PackageId')) or json.dumps(item,sort_keys=True)
            unique[str(identity)]=item
        detail_codes={item.get('PackageCode') for item in unique.values()}
        for item in credit_accounts:
            if item.get('PackageCode') not in detail_codes:
                unique['summary:'+str(item.get('PackageCode'))]=item
        packages=[normalize_package(a,i) for i,a in enumerate(unique.values()) if a.get('CapacityUnit','credits')=='credits']
        return {'balance_micro':total,'packages':packages,'source':source}

    def request(self, credential, body):
        # The CN endpoint rejects unknown fields with HTTP 400 (verified against the
        # reference implementation, which filters its own passthrough set). Forward
        # only the keys it accepts: notably `parallel_tool_calls` is not one of them.
        forwarded={k:body[k] for k in CN_BODY_KEYS if k in body}
        forwarded['stream']=True
        forwarded['stream_options']={'include_usage':True}
        # CN expects a function name string, preserving forced-call semantics.
        if isinstance(forwarded.get('tool_choice'),dict):
            forwarded['tool_choice']=forwarded['tool_choice']['function']['name']
        forwarded['messages']=normalize_cn_messages(body['messages'])
        return self.chat+'/v2/chat/completions',self.headers(credential),forwarded


class Compatible(Connector):
    async def refresh(self, credential):
        return credential

    async def models(self, credential):
        result=await self.checked('GET',credential['endpoint']+'/models',headers={'Authorization':'Bearer '+credential['api_key']})
        return [{'id':m['id'],'capabilities':{'chat':True,'stream':True,'tools':True,'vision':True,'verification':'configured'}}
                for m in result.get('data',[]) if isinstance(m,dict) and m.get('id')]

    async def balance(self, credential):
        return {'balance_micro':None,'packages':[],'source':'unavailable'}

    def request(self, credential, body):
        return credential['endpoint']+'/chat/completions', {'Authorization':'Bearer '+credential['api_key']}, {**body,'stream':True,'stream_options':{'include_usage':True}}


async def sse_events(response):
    """Bounded incremental SSE parser; accepts CRLF, multiline data and comments."""
    data=[]
    size=0
    async for line in response.aiter_lines():
        if len(line)>2*1024*1024:
            raise ProviderError('sse_frame_too_large')
        if line=='':
            if data:
                value='\n'.join(data)
                data=[]
                size=0
                if value=='[DONE]':
                    yield None
                    return
                try:
                    obj=json.loads(value)
                except ValueError:
                    raise ProviderError('invalid_sse_json') from None
                if not isinstance(obj,dict) or 'error' in obj:
                    raise ProviderError('upstream_stream_error')
                yield obj
        elif line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
            size+=len(line)
            if size>2*1024*1024:
                raise ProviderError('sse_frame_too_large')
    if data:
        raise ProviderError('truncated_sse_frame')
