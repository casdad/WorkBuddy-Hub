"""Opt-in acceptance against an already authorized account. Never prints secrets."""
import argparse
import getpass
import json
import time
from pathlib import Path
import httpx


def run():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8792')
    parser.add_argument('--account',type=int,required=True)
    parser.add_argument('--model',default='deepseek-v4.1-flash')
    parser.add_argument('--password-file',type=Path)
    parser.add_argument('--output',type=Path,default=Path('test-results/live-acceptance.json'))
    parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args()
    password=getpass.getpass('管理员密码：') if not args.password_file else args.password_file.read_text(encoding='utf-8').split('首次密码：')[1].splitlines()[0]
    report={'timestamp':time.time(),'upstream':'real_workbuddy_cn','account_id':args.account,'model':args.model,'checks':[]}
    def record(name,passed,**detail):
        row={'name':name,'passed':passed,**detail};report['checks'].append(row);print(json.dumps(row,ensure_ascii=False))
    with httpx.Client(base_url=args.url,timeout=180,trust_env=False) as c:
        login=c.post('/api/login',json={'password':password});login.raise_for_status()
        c.headers['X-CSRF-Token']=login.json()['csrf']
        accounts=c.get('/api/accounts').json()
        account=next(a for a in accounts if a['id']==args.account)
        record('authorized_account',account['provider']=='workbuddy_cn' and account['state']=='ready',state=account['state'])
        if args.refresh:
            response=c.post(f'/api/accounts/{args.account}/refresh',json={})
            record('credential_refresh',response.status_code==200,status=response.status_code)
        sync=c.post(f'/api/accounts/{args.account}/sync',json={})
        record('models_and_balance_sync',sync.status_code==200 and sync.json().get('ok',False),status=sync.status_code)
        before=next(a for a in c.get('/api/accounts').json() if a['id']==args.account)
        record('balance_observed',before['balance_micro'] is not None,balance_micro=before['balance_micro'],source=before['balance_source'])
        pool=c.post('/api/pools',json={'name':'验收-'+str(int(time.time())),'account_ids':[args.account]});pool.raise_for_status()
        key=c.post('/api/keys',json={'name':'临时验收密钥','pool_id':pool.json()['id'],'request_limit':8,'models':[args.model]});key.raise_for_status()
        headers={'Authorization':'Bearer '+key.json()['key']}
        try:
            catalog=c.get('/v1/models',headers=headers)
            record('model_access',catalog.status_code==200 and any(m['id']==args.model for m in catalog.json().get('data',[])))
            body={'model':args.model,'messages':[{'role':'user','content':'只回复 OK 两个字母，不要解释。'}],'max_tokens':64}
            normal=c.post('/v1/chat/completions',headers=headers,json=body)
            normal_json=normal.json()
            ok=normal.status_code==200 and bool(normal_json.get('choices'))
            record('chat_nonstream',ok,status=normal.status_code,request_id=normal.headers.get('x-request-id'),usage=normal_json.get('usage'))
            if not ok:
                record('remaining_calls_skipped',False,reason='first_call_failed')
            else:
                with c.stream('POST','/v1/chat/completions',headers=headers,json={**body,'stream':True}) as response:
                    done=False;has_text=False;error=False
                    for line in response.iter_lines():
                        if line=='data: [DONE]':done=True
                        elif line.startswith('data: '):
                            event=json.loads(line[6:]);error=error or bool(event.get('error'))
                            has_text=has_text or any(x.get('delta',{}).get('content') for x in event.get('choices',[]))
                    record('chat_stream',response.status_code==200 and done and has_text and not error,status=response.status_code,request_id=response.headers.get('x-request-id'))
                for protocol,extra in [('responses',{'input':'只回复 OK。','max_output_tokens':64}),('messages',{'messages':[{'role':'user','content':'只回复 OK。'}],'max_tokens':64})]:
                    response=c.post('/v1/'+protocol,headers=headers,json={'model':args.model,**extra})
                    result=response.json()
                    record(protocol,response.status_code==200 and bool(result.get('output') or result.get('content')),status=response.status_code,request_id=response.headers.get('x-request-id'))
            sync=c.post(f'/api/accounts/{args.account}/sync',json={})
            after=next(a for a in c.get('/api/accounts').json() if a['id']==args.account)
            record('balance_resync',sync.status_code==200 and sync.json().get('ok',False),balance_micro=after['balance_micro'],source=after['balance_source'])
            logs=c.get('/api/logs',params={'key_id':key.json()['id']}).json()
            report['requests']=[{k:row[k] for k in ('id','state','status','input_tokens','output_tokens','credits_micro','usage_source','error')} for row in logs]
        finally:
            c.patch('/api/keys/'+str(key.json()['id']),json={'enabled':False})
            c.post('/api/logout',json={})
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if not all(row['passed'] for row in report['checks']):
        raise SystemExit(1)


if __name__=='__main__':
    run()
