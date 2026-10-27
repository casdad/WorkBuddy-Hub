"""Deterministic protocol fixtures. Never loaded by the production application."""
import asyncio
import json
import time
import httpx


class Stream(httpx.AsyncByteStream):
    def __init__(self,parts,delay=0):
        self.parts=parts
        self.delay=delay
        self.closed=False
    async def __aiter__(self):
        for part in self.parts:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield part
    async def aclose(self):
        self.closed=True


def credential(uid='fixture-1',expires=None):
    return {'auth':{'accessToken':'fixture-access-'+uid,'refreshToken':'fixture-refresh-'+uid,'domain':'www.codebuddy.cn','expiresAt':expires or time.time()+3600},
            'account':{'uid':uid,'nickname':'测试账号 '+uid,'type':'personal'}}


class Upstream:
    def __init__(self):
        self.requests=[]
        self.chat_calls=0
        self.refreshes=0
        self.mode='normal'
        self.channel_blocks=0
        self.usage=True
        self.credit_failure=False
        self.partial_refresh=False
        self.last_stream=None

    async def handle(self,request):
        self.requests.append(request)
        path=request.url.path
        if path.endswith('/auth/state'):
            return httpx.Response(200,json={'code':0,'data':{'state':'fixture-state','authUrl':'https://www.codebuddy.cn/login?state=fixture-state'}})
        if path.endswith('/auth/token'):
            return httpx.Response(200,json={'code':0,'data':credential()['auth']})
        if path.endswith('/login/account'):
            return httpx.Response(200,json={'code':0,'data':credential()['account']})
        if path.endswith('/token/refresh'):
            self.refreshes+=1
            await asyncio.sleep(.01)
            payload={'refreshToken':'refresh-new'} if self.partial_refresh else {'accessToken':'access-new','refreshToken':'refresh-new','expiresIn':3600}
            return httpx.Response(200,json={'code':0,'data':payload})
        if path.endswith('/models'):
            model={'id':'test-model','supportsToolCall':True,'supportsImages':True,
                   'reasoning':{'supportedEfforts':['low','medium','high'],'defaultEffort':'high'}}
            return httpx.Response(200,json={'code':0,'data':{'models':[model]}} if '/enterprises/' in path else {'data':[model]})
        if 'get-user-resource' in path:
            if self.credit_failure:
                return httpx.Response(503,json={'error':'maintenance'})
            package={'PackageId':'p1','PackageCode':'monthly','PackageName':'月度额度','CapacityUnit':'credits',
                     'CycleCapacityRemainPrecise':'123.456789','CycleCapacityRemain':123,'CapacityRemain':999,
                     'DeductionEndTime':'2034-12-31','CycleEndTime':time.time()+3*86400}
            key='Packages' if path.endswith('summary') else 'Accounts'
            if path.endswith('packages'):
                body=json.loads(request.content)
                assert body.get('PackageCodes'), 'live API requires PackageCodes'
                if path.endswith('free-packages'):
                    assert body.get('SlicePeriodStartTime'), 'live API requires slice period'
            data=[] if path.endswith('free-packages') else [package]
            return httpx.Response(200,json={'code':0,'data':{'Response':{'Data':{key:data}}}})
        if path.endswith('/chat/completions'):
            self.chat_calls+=1
            body=json.loads(request.content)
            if self.channel_blocks>0:
                self.channel_blocks-=1
                return httpx.Response(400,json={'code':'11-128','msg':'Illegal API invocation from an unapproved channel'})
            if isinstance(body.get('tool_choice'),dict):
                return httpx.Response(400,json={'code':11101})
            if self.mode=='effort-reject' and body.get('reasoning_effort') not in (None,'low','medium','high'):
                return httpx.Response(400,json={'code':11150})
            if self.mode=='unknown-field':
                return httpx.Response(400,json={'code':11001,'message':'invalid request body'})
            if self.mode=='429':
                return httpx.Response(429,headers={'Retry-After':'123'},json={'secret':'must-not-leak'})
            if self.mode=='401-once' and self.chat_calls==1:
                return httpx.Response(401,json={})
            if self.mode=='403':
                return httpx.Response(403,json={'secret':'must-not-leak'})
            if self.mode=='500':
                return httpx.Response(500,json={})
            if self.mode=='connect-error':
                raise httpx.ConnectError('hidden upstream info',request=request)
            delta={'role':'assistant','content':'连接成功。'}
            if body.get('tools'):
                delta={'role':'assistant','tool_calls':[{'index':0,'id':'call_fixture','type':'function','function':{'name':'weather','arguments':'{"city":"上海"}'}}]}
            first={'id':'chat-fixture','object':'chat.completion.chunk','model':'test-model','choices':[{'index':0,'delta':delta,'finish_reason':None}]}
            last={'id':'chat-fixture','object':'chat.completion.chunk','model':'test-model','choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if body.get('tools') else 'stop'}]}
            if self.usage:
                last['usage']={'prompt_tokens':12,'completion_tokens':5,'credits':'0.012345'}
            raw=('data: '+json.dumps(first,ensure_ascii=False)+'\r\n\r\n').encode()
            end=('data: '+json.dumps(last)+'\n\n'+'data: [DONE]\n\n').encode()
            parts=[raw[:13],raw[13:]]+([] if self.mode=='truncated' else [end])
            self.last_stream=Stream(parts,.005 if self.mode=='slow' else 0)
            return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=self.last_stream)
        return httpx.Response(404,json={})
