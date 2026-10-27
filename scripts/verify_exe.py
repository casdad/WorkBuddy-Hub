"""Test the built executable against the running local release without secrets."""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    exe = Path('dist/WorkBuddyHub.exe').resolve()
    version = re.search(r'__version__\s*=\s*"([^"]+)"',Path('hub/__init__.py').read_text(encoding='utf-8')).group(1)
    report = {'timestamp': time.time(), 'version': version, 'exe_sha256': hashlib.sha256(exe.read_bytes()).hexdigest(), 'checks': []}
    def check(name, passed, **detail):
        row = {'name': name, 'passed': bool(passed), **detail}
        report['checks'].append(row)
        print(json.dumps(row, ensure_ascii=False))
    with tempfile.TemporaryDirectory(prefix='wbh-exe-') as folder:
        result = subprocess.run([str(exe), '--check', '--data-dir', folder], capture_output=True, timeout=60)
        check('offline_ssl_vault_schema_frontend', result.returncode == 0 and b'frontend=True' in result.stdout and version.encode() in result.stdout)
    with httpx.Client(base_url='http://127.0.0.1:8792', timeout=15, trust_env=False) as c:
        health = c.get('/healthz').json()
        check('running_release_health', health.get('check') == 'process_liveness' and health.get('version') == version)
        html = c.get('/')
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html.text)
        check('bundled_frontend', html.status_code == 200 and len(assets) >= 2 and all(c.get(a).status_code == 200 for a in assets))
        check('admin_requires_login', c.get('/api/accounts').status_code == 401)
    result = subprocess.run([str(exe), '--check', '--data-dir', str(args.data_dir.resolve())], capture_output=True, timeout=60)
    check('single_instance_rejected_cleanly', result.returncode != 0 and b'Traceback' not in result.stderr,
          exit_code=result.returncode)
    target = Path('test-results/exe.json')
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if not all(row['passed'] for row in report['checks']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
