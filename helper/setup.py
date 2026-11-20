#!/usr/bin/python3
"""Prepare or verify the local ML runtime; network access is opt-in."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULT = Path.home() / '.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def requirements():
    return dict(line.strip().split('==', 1) for line in
                (ROOT / 'requirements.lock').read_text().splitlines()
                if line.strip() and not line.startswith('#'))


def runtime_mismatches(target):
    wanted = requirements()
    python = target / '.venv/bin/python'
    if not python.exists():
        return list(wanted)
    script = '''import importlib.metadata as m,json,sys
result={'_python':list(sys.version_info[:2])}
for name in json.loads(sys.argv[1]):
 try: result[name]=m.version(name)
 except m.PackageNotFoundError: result[name]=None
print(json.dumps(result))'''
    actual = json.loads(subprocess.check_output([str(python), '-c', script, json.dumps(list(wanted))]))
    missing = [name for name, version in wanted.items() if actual.get(name) != version]
    return missing + (['Python 3.14'] if actual.get('_python') != [3, 14] else [])


def prepare_dependencies(target):
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError('O lock requer Python 3.14; use o Python do Ubuntu 26.04.')
    if not runtime_mismatches(target):
        return
    subprocess.run([sys.executable, '-m', 'venv', str(target / '.venv')], check=True)
    python = str(target / '.venv/bin/python')
    subprocess.run([python, '-m', 'pip', 'install', '--no-cache-dir',
                    '--index-url', 'https://pypi.org/simple',
                    '--extra-index-url', 'https://download.pytorch.org/whl/cpu', '-r',
                    str(ROOT / 'requirements.lock')], check=True)


def verify_runtime(target):
    missing = runtime_mismatches(target)
    if missing:
        raise RuntimeError('Runtime ausente ou divergente: ' + ', '.join(missing) +
                           '. Use --dependencies para permitir a preparação.')
    subprocess.run([str(target / '.venv/bin/python'), '-m', 'pip', 'check'], check=True)
    print('VERIFICADO: dependências fixadas e compatíveis.')


def prepare_models(target, download=False):
    manifest = json.loads((ROOT / 'models.lock.json').read_text())
    missing = []
    for name, entry in manifest.items():
        path = target / 'models' / name
        if path.is_file() and path.stat().st_size == entry['bytes'] and digest(path) == entry['sha256']:
            print('VERIFICADO:', name)
            continue
        if not download:
            missing.append(name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.download')
        print('Baixando explicitamente:', entry['url'], flush=True)
        try:
            with urllib.request.urlopen(entry['url'], timeout=90) as source, tmp.open('wb') as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
            if tmp.stat().st_size != entry['bytes'] or digest(tmp) != entry['sha256']:
                raise RuntimeError('Tamanho ou SHA-256 divergente: ' + name)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        print('VERIFICADO:', name)
    if missing:
        raise RuntimeError('Modelos ausentes ou divergentes: ' + ', '.join(missing) +
                           '. Use --models para permitir downloads.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=DEFAULT, help='Diretório helper de destino')
    parser.add_argument('--dependencies', action='store_true', help='Permitir downloads do runtime CPU')
    parser.add_argument('--models', action='store_true', help='Permitir downloads do manifesto de modelos')
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    metadata = target.parent / 'metadata.json'
    if metadata.exists() and json.loads(metadata.read_text()).get('uuid') != 'mixer-rapido@alee.local':
        parser.error('O destino pertence a outra extensão.')
    try:
        if args.dependencies:
            prepare_dependencies(target)
        verify_runtime(target)
        prepare_models(target, args.models)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')
    print('VERIFICADO: runtime e modelos locais íntegros; downloads somente quando autorizados.')


if __name__ == '__main__':
    main()
