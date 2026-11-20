#!/usr/bin/python3
"""Compile the buffer with system headers or explicitly downloaded headers."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
DEFAULT = Path.home() / '.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper'


def download_headers(directory, version):
    url = f'https://codeload.github.com/PipeWire/pipewire/tar.gz/refs/tags/{version}'
    data = urllib.request.urlopen(url, timeout=60).read()
    print('Headers:', url, 'SHA256:', hashlib.sha256(data).hexdigest())
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        prefix = archive.getnames()[0] + '/'
        for entry in archive.getmembers():
            rel = entry.name[len(prefix):]
            if '..' in Path(rel).parts or Path(rel).is_absolute():
                raise ValueError('Caminho de header inválido')
            if entry.isfile() and ((rel.startswith('src/pipewire/') and
               (rel.endswith('.h') or rel.endswith('version.h.in'))) or
               (rel.startswith('spa/include/') and rel.endswith('.h'))):
                path = directory / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(entry).read())
    text = (directory / 'src/pipewire/version.h.in').read_text()
    major, minor, micro = version.split('.')
    for key, value in {'PIPEWIRE_VERSION_MAJOR': major, 'PIPEWIRE_VERSION_MINOR': minor,
                       'PIPEWIRE_VERSION_MICRO': micro, 'PIPEWIRE_VERSION': version,
                       'PIPEWIRE_API_VERSION': '0.3'}.items():
        text = text.replace('@' + key + '@', value)
    (directory / 'src/pipewire/version.h').write_text(text)
    (directory / 'mixer-headers.json').write_text(json.dumps({'pipewire': version,
        'url': url, 'sha256': hashlib.sha256(data).hexdigest()}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT)
    parser.add_argument('--headers-dir', type=Path)
    parser.add_argument('--download-headers', action='store_true')
    args = parser.parse_args()
    target = args.output_dir.expanduser().resolve()
    headers = args.headers_dir.expanduser().resolve() if args.headers_dir else target / 'build-deps'
    version = re.findall(r'(\d+\.\d+\.\d+)', subprocess.check_output(['pw-cli', '--version']).decode())[-1]
    if shutil.which('pkg-config') and subprocess.run(['pkg-config', '--exists', 'libpipewire-0.3'],
                                                    check=False).returncode == 0:
        flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'libpipewire-0.3']).decode())
    else:
        header = headers / 'src/pipewire/version.h'
        if not header.exists() or f'"{version}"' not in header.read_text():
            if not args.download_headers:
                parser.error('Instale libpipewire-0.3-dev e pkg-config, ou use --download-headers.')
            download_headers(headers, version)
        flags = ['-I' + str(headers / 'src'), '-I' + str(headers / 'spa/include'), '-l:libpipewire-0.3.so.0']
    target.mkdir(parents=True, exist_ok=True)
    temporary = target / 'mixer-buffer-engine.new'
    try:
        subprocess.run(['gcc', '-std=gnu11', '-O2', '-g', '-Wall', '-Wextra', '-Wno-unused-parameter',
                        str(ROOT / 'buffer_engine.c'), '-o', str(temporary), *flags, '-lm'], check=True)
        temporary.replace(target / 'mixer-buffer-engine')
    finally:
        temporary.unlink(missing_ok=True)
    (target / 'buffer-build.json').write_text(json.dumps({'pipewire': version,
        'source_sha256': hashlib.sha256((ROOT / 'buffer_engine.c').read_bytes()).hexdigest(),
        'binary_sha256': hashlib.sha256((target / 'mixer-buffer-engine').read_bytes()).hexdigest(),
        'compiler': subprocess.check_output(['gcc', '-dumpfullversion']).decode().strip()}, indent=2) + '\n')
    print('EXECUTADO: buffer compilado para PipeWire', version, 'em', target)


if __name__ == '__main__':
    main()
