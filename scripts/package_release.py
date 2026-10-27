"""Build clean deliverables from explicit allowlists; never include account data."""
import hashlib
import importlib.metadata
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'release'
EVIDENCE = ['pytest.xml', 'review-regressions.xml', 'browser.json', 'live-acceptance.json', 'live-release.json', 'exe.json', 'overview.png', 'mobile.png']


def digest(data):
    return hashlib.sha256(data).hexdigest()


def licenses():
    result = {'Python-LICENSE.txt': (Path(sys.base_prefix) / 'LICENSE.txt').read_bytes()}
    inventory = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata['Name']
        inventory.append({'name': name, 'version': dist.version,
                          'license': dist.metadata.get('License-Expression') or dist.metadata.get('License', '')})
        for entry in dist.files or []:
            if any(word in entry.name.lower() for word in ('license', 'copying', 'notice')) and entry.suffix.lower() in ('', '.txt', '.md', '.rst'):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    result[name+'/'+entry.name] = source.read_bytes()
    for name in ('react', 'react-dom', 'scheduler', 'lucide-react'):
        folder = ROOT/'web'/'node_modules'/name
        for path in folder.glob('*'):
            if path.is_file() and path.name.lower().startswith(('license', 'notice')):
                result['web-'+name+'/'+path.name] = path.read_bytes()
    result['dependency-inventory.json'] = json.dumps(inventory, ensure_ascii=False, indent=2).encode()
    return result


def archive(name, files, extra):
    entries = {path.relative_to(ROOT).as_posix(): path.read_bytes() for path in files}
    entries.update(extra)
    manifest = {name: {'sha256': digest(data), 'bytes': len(data)} for name, data in entries.items()}
    entries['MANIFEST.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    target = OUT/name
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for relative, data in sorted(entries.items()):
            assert not set(Path(relative).parts) & {'data', '.venv', '__pycache__', 'node_modules'}
            assert not relative.endswith(('.db', '.sqlite3', '.log', '.key'))
            z.writestr('WorkBuddy-Hub/'+relative, data)
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        for relative, meta in manifest.items():
            assert digest(z.read('WorkBuddy-Hub/'+relative)) == meta['sha256']
    return {'file': name, 'sha256': digest(target.read_bytes()), 'bytes': target.stat().st_size,
            'entries': len(entries), 'integrity_verified': True, 'account_data_excluded': True}


def main():
    OUT.mkdir(exist_ok=True)
    docs = [ROOT/name for name in ('README.md', 'REFERENCES.md', 'TEST-REPORT.md', 'REVIEW-RESPONSE.md', 'LICENSE')]
    evidence = [ROOT/'test-results'/name for name in EVIDENCE]
    attribution = [ROOT/'hub'/'vendor'/name for name in ('LICENSE-wb2api', 'LICENSE-switch')]
    notices = {'licenses/'+name: data for name, data in licenses().items()}
    source = docs+evidence
    for folder in ('hub', 'scripts', 'tests', 'web/src', 'web/e2e', 'web/dist'):
        source += [p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    source += [p for p in ROOT.iterdir() if p.is_file() and p.suffix in ('.py', '.ps1', '.bat', '.ini')]
    source += list(ROOT.glob('requirements*.txt')) + [ROOT/'.gitignore']
    source += [ROOT/'web'/name for name in ('package.json', 'package-lock.json', 'index.html', 'tsconfig.json', 'vite.config.ts', 'playwright.config.ts')]
    binary = docs+evidence+attribution+[ROOT/'start.bat', ROOT/'start.ps1', ROOT/'dist'/'WorkBuddyHub.exe']
    report = [archive('WorkBuddy-Hub-Windows-x64.zip', binary, notices), archive('WorkBuddy-Hub-Source.zip', source, notices)]
    (OUT/'SHA256SUMS.txt').write_text('\n'.join(row['sha256']+'  '+row['file'] for row in report)+'\n', encoding='ascii')
    (OUT/'release-manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
