#!/usr/bin/python3
"""Install the existing checkout into the user's GNOME extension directory."""
import argparse
from datetime import datetime, timezone
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
UUID = 'mixer-rapido@alee.local'
DESTINATION = Path.home() / '.local/share/gnome-shell/extensions' / UUID
PAYLOAD = [
    'metadata.json', 'extension.js', 'prefs.js', 'README.md',
    'schemas/org.gnome.shell.extensions.mixer-rapido.gschema.xml',
    *('helper/' + name for name in (
        'core.py', 'service.py', 'inference.py', 'fast_vad.py', 'setup.py',
        'build_buffer.py', 'install_buffer.py', 'rollback.py', 'buffer_engine.c',
        'buffer-policy.lua', 'mixer-rapido.service', 'mixer-buffer.service',
        'requirements.lock', 'models.lock.json')),
]


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=30).decode()


def check_host():
    if os.geteuid() == 0:
        raise RuntimeError('Execute como usuário da sessão gráfica, sem sudo.')
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError('Requer Python 3.14 (Ubuntu 26.04 de referência).')
    if platform.machine() != 'x86_64':
        raise RuntimeError('O runtime fixado foi validado para Ubuntu x86_64.')
    missing = [name for name in ('gnome-shell', 'gsettings', 'gnome-extensions', 'glib-compile-schemas',
        'pw-cli', 'pw-dump', 'pw-metadata', 'pactl', 'parec', 'wireplumber', 'gcc',
        'systemctl', 'dconf', 'ffmpeg') if not shutil.which(name)]
    if missing:
        raise RuntimeError('Instale as dependências do README: ' + ', '.join(missing))
    shell = re.search(r'\b(\d+)\.', run('gnome-shell', '--version'))
    if not shell or shell.group(1) != '50':
        raise RuntimeError('Esta extensão é compatível com GNOME Shell 50.')
    import gi
    gi.require_version('Adw', '1')
    from gi.repository import Adw, Gio
    if not hasattr(Adw, 'SpinRow'):
        raise RuntimeError('Libadwaita com SpinRow não está disponível.')
    if not os.environ.get('XDG_RUNTIME_DIR'):
        raise RuntimeError('Entre na sessão gráfica do Ubuntu antes de instalar.')
    Gio.bus_get_sync(Gio.BusType.SESSION, None)
    run('systemctl', '--user', 'show-environment')


def copy_payload(source, target):
    for relative in PAYLOAD:
        original = source / relative
        if not original.is_file():
            raise RuntimeError('Arquivo obrigatório ausente: ' + relative)
    for relative in PAYLOAD:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
    subprocess.run(['glib-compile-schemas', '--strict', str(target / 'schemas')], check=True)


def backup_code(target):
    if not (target / 'metadata.json').exists():
        return None
    metadata = json.loads((target / 'metadata.json').read_text())
    if metadata.get('uuid') != UUID:
        raise RuntimeError('O destino pertence a outra extensão; nada foi substituído.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = Path.home() / '.local/state/mixer-rapido/backups' / ('install-' + stamp)
    shutil.copytree(target, backup / 'extension',
                    ignore=shutil.ignore_patterns('.venv', 'models', 'build-deps', '__pycache__'))
    (backup / 'settings.dconf').write_text(run('dconf', 'dump', '/org/gnome/shell/extensions/mixer-rapido/'))
    print('Backup dos arquivos e preferências:', backup)
    return backup


def publish_payload(stage, target):
    """Only prepared code/artifacts; retain runtime, models and personal data."""
    files = [p for p in stage.rglob('*') if p.is_file() and 'build-deps' not in p.relative_to(stage).parts]
    old = {str(p.relative_to(stage)): ((target / p.relative_to(stage)).read_bytes(),
                                      (target / p.relative_to(stage)).stat().st_mode)
           for p in files if (target / p.relative_to(stage)).is_file()}
    try:
        for path in files:
            destination = target / path.relative_to(stage)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + '.new')
            shutil.copy2(path, temporary)
            temporary.replace(destination)
    except BaseException:
        for path in files:
            relative = str(path.relative_to(stage))
            destination = target / relative
            if relative in old:
                destination.write_bytes(old[relative][0])
                destination.chmod(old[relative][1])
            else:
                destination.unlink(missing_ok=True)
            destination.with_name(destination.name + '.new').unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Conferir pré-requisitos e mostrar o plano, sem instalar')
    mode.add_argument('--check', action='store_true', help='Verificar a instalação existente, sem alterá-la')
    parser.add_argument('--download', action='store_true', help='Permitir downloads de dependências/modelos/headers')
    args = parser.parse_args()
    try:
        check_host()
        for relative in PAYLOAD:
            if not (SOURCE / relative).is_file():
                raise RuntimeError('Arquivo obrigatório ausente: ' + relative)
        if args.check:
            subprocess.run([sys.executable, str(SOURCE / 'helper/setup.py')], check=True)
            subprocess.run([sys.executable, str(DESTINATION / 'helper/install_buffer.py'), 'check'], check=True)
            print('VERIFICADO: instalação existente, sem alterações.')
            return
        if args.dry_run:
            print(json.dumps({'destination': str(DESTINATION), 'payload': PAYLOAD,
                'downloads_allowed': args.download,
                'steps': ['Preparar e compilar código temporário', 'Preparar runtime/modelos com opt-in',
                          'Preservar configurações e criar backup se houver instalação',
                          'Copiar arquivos preparados', 'Instalar unidades de usuário e ativar interceptação',
                          'Habilitar extensão para o próximo login']}, indent=2, ensure_ascii=False))
            return
        for unit in ('mixer-rapido.service', 'mixer-buffer.service'):
            status = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit], check=False)
            if status.returncode == 0:
                raise RuntimeError('Instalação ativa: use --check. Para atualizar, siga a parada segura no README.')
        if list((Path(os.environ['XDG_RUNTIME_DIR']) / 'mixer-buffer').glob('status-*.json')):
            raise RuntimeError('Há filas retidas. Encerre o Chrome e aguarde a drenagem antes de instalar.')
        if (Path.home() / '.config/wireplumber/wireplumber.conf.d/95-mixer-buffer.conf').exists():
            raise RuntimeError('Desative a interceptação antes de atualizar, conforme o README.')
        # Validate ownership and preserve existing code before touching the destination.
        backup_code(DESTINATION)
        with tempfile.TemporaryDirectory(prefix='mixer-build-') as directory:
            stage = Path(directory)
            copy_payload(SOURCE, stage)
            build = [sys.executable, str(SOURCE / 'helper/build_buffer.py'), '--output-dir', str(stage / 'helper')]
            cached = DESTINATION / 'helper/build-deps'
            if cached.is_dir():
                build += ['--headers-dir', str(cached)]
            if args.download:
                build += ['--download-headers']
            subprocess.run(build, check=True)
            setup = [sys.executable, str(SOURCE / 'helper/setup.py'), '--target', str(DESTINATION / 'helper')]
            if args.download:
                setup += ['--dependencies', '--models']
            subprocess.run(setup, check=True)
            publish_payload(stage, DESTINATION)
        units = Path.home() / '.config/systemd/user'
        units.mkdir(parents=True, exist_ok=True)
        for unit in ('mixer-rapido.service', 'mixer-buffer.service'):
            shutil.copy2(DESTINATION / 'helper' / unit, units / unit)
        run('systemctl', '--user', 'daemon-reload')
        run('systemctl', '--user', 'enable', 'mixer-rapido.service')
        subprocess.run([sys.executable, str(DESTINATION / 'helper/install_buffer.py'), 'enable'], check=True)
        from gi.repository import Gio
        settings = Gio.Settings.new('org.gnome.shell')
        enabled = list(settings.get_strv('enabled-extensions'))
        if UUID not in enabled:
            if not settings.set_strv('enabled-extensions', enabled + [UUID]):
                raise RuntimeError('Não foi possível habilitar a extensão nas preferências do GNOME.')
            Gio.Settings.sync()
        print('EXECUTADO: instalação preparada e auxiliares ativos. Saia e entre na sessão para carregar o menu.')
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\nInstalação não concluída; consulte o README para diagnóstico.\n')


if __name__ == '__main__':
    main()
