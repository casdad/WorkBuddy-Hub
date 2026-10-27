"""Opt-in real account check: current billing and one function-call generation."""
import argparse
import getpass
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8792')
    parser.add_argument('--account', type=int, required=True)
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('--model', default='deepseek-v4.1-flash')
    args = parser.parse_args()
    password = (args.password_file.read_text(encoding='utf-8').split('首次密码：')[1].splitlines()[0]
                if args.password_file else getpass.getpass('管理员密码：'))
    report = {'timestamp': time.time(), 'upstream': 'real_workbuddy_cn', 'checks': []}
    def check(name, passed, **details):
        row = {'name': name, 'passed': bool(passed), **details}
        report['checks'].append(row)
        print(json.dumps(row, ensure_ascii=False))
    with httpx.Client(base_url=args.url, timeout=180, trust_env=False) as client:
        login = client.post('/api/login', json={'password': password})
        login.raise_for_status()
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        key = None
        try:
            check('final_process', client.get('/healthz').json().get('check') == 'process_liveness')
            sync = client.post(f'/api/accounts/{args.account}/sync', json={})
            check('current_sync', sync.status_code == 200 and sync.json().get('ok'))
            account = next(a for a in client.get('/api/accounts').json() if a['id'] == args.account)
            check('new_billing', account['balance_source'] == 'upstream_summary' and account['balance_micro'] is not None,
                  source=account['balance_source'], balance_micro=account['balance_micro'])
            models = [m for m in client.get('/api/models').json()['models'] if m['account_id'] == args.account]
            selected = next(m for m in models if m['model'] == args.model)
            check('catalog_tool_capability', json.loads(selected['capabilities']).get('tools'), model=args.model, chat_model_count=len(models))
            pool = client.post('/api/pools', json={'name': '发布验收-'+str(int(time.time())), 'account_ids': [args.account]})
            pool.raise_for_status()
            response = client.post('/api/keys', json={'name': '发布验收临时密钥', 'pool_id': pool.json()['id'], 'request_limit': 1, 'models': [args.model]})
            response.raise_for_status()
            key = response.json()
            body = {'model': args.model, 'max_tokens': 128,
                    'messages': [{'role': 'user', 'content': '请调用 weather 函数查询上海天气，不要自行回答。'}],
                    'tools': [{'type': 'function', 'function': {'name': 'weather', 'description': '查询城市天气',
                               'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city']}}}],
                    'tool_choice': {'type': 'function', 'function': {'name': 'weather'}}}
            result = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer '+key['key']}, json=body)
            calls = result.json().get('choices', [{}])[0].get('message', {}).get('tool_calls', [])
            correct = any(call.get('function', {}).get('name') == 'weather' and
                          json.loads(call['function']['arguments']).get('city') in ('上海', 'Shanghai') for call in calls)
            check('real_function_call', result.status_code == 200 and correct, status=result.status_code,
                  request_id=result.headers.get('x-request-id'))
            # Validate the returned call only; never execute a real tool.
            detail = client.get('/api/logs/'+result.headers['x-request-id']).json()
            row = detail['request']
            check('real_ledger', row['settled'] == 1 and row['state'] == 'success' and detail['ledger']['units'] == 1,
                  input_tokens=row['input_tokens'], output_tokens=row['output_tokens'], credits_micro=row['credits_micro'],
                  attempt_count=len(detail['attempts']))
        finally:
            if key:
                disabled = client.patch('/api/keys/'+str(key['id']), json={'enabled': False})
                check('temporary_key_disabled', disabled.status_code == 200)
            client.post('/api/logout', json={})
            output = Path('test-results/live-release.json')
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if not all(row['passed'] for row in report['checks']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
