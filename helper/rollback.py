#!/usr/bin/python3
"""Restore a concrete pre-update backup, retaining newly enrolled voices."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('backup', type=Path)
parser.add_argument('--restore-settings', action='store_true',
                    help='Restaurar também os valores de preferências do backup')
args = parser.parse_args()
backup = args.backup.expanduser().resolve()
installed = Path.home() / '.local/share/gnome-shell/extensions/mixer-rapido@alee.local'
saved = backup / 'extension'
for directory in (saved, installed):
    metadata = json.loads((directory / 'metadata.json').read_text())
    if metadata.get('uuid') != 'mixer-rapido@alee.local':
        raise SystemExit('UUID inesperado: ' + str(directory))
fragment = Path.home() / '.config/wireplumber/wireplumber.conf.d/95-mixer-buffer.conf'
if fragment.exists():
    # Refuse rollback while any held audio could be discarded.
    subprocess.run(['/usr/bin/python3', str(installed / 'helper/install_buffer.py'), 'disable'], check=True)
subprocess.run(['systemctl', '--user', 'disable', '--now', 'mixer-rapido.service'], check=True)
for source in saved.rglob('*'):
    if source.is_file():
        target = installed / source.relative_to(saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
if not (saved / 'prefs.js').exists():
    (installed / 'prefs.js').unlink(missing_ok=True)
if args.restore_settings:
    subprocess.run(['dconf', 'load', '/org/gnome/shell/extensions/mixer-rapido/'],
                   input=(backup / 'settings.dconf').read_bytes(), check=True)
print('EXECUTADO: arquivos originais restaurados e serviço auxiliar desativado.')
print('Vozes cadastradas e runtime auxiliar foram preservados. Saia e entre na sessão para carregar o código restaurado.')
