"""Installer file transactions and opt-in downloads; never install on the host."""
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load('mixer_installer', ROOT / 'install.py')
setup = load('mixer_setup', ROOT / 'helper/setup.py')


class InstallTests(unittest.TestCase):
    def test_prepared_payload_compiles_schema_and_excludes_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            installer.copy_payload(ROOT, stage)
            actual = {str(p.relative_to(stage)) for p in stage.rglob('*') if p.is_file()}
            self.assertEqual(actual, set(installer.PAYLOAD) | {'schemas/gschemas.compiled'})
            self.assertNotIn('models', {p.name for p in stage.rglob('*')})

    def test_publication_keeps_runtime_and_personal_files(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, target = Path(directory)/'stage', Path(directory)/'target'
            installer.copy_payload(ROOT, stage)
            for name in ['helper/.venv/keep', 'helper/models/keep', 'personal.txt']:
                path = target/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('preservar')
            installer.publish_payload(stage, target)
            self.assertEqual((target/'helper/.venv/keep').read_text(), 'preservar')
            self.assertEqual((target/'helper/models/keep').read_text(), 'preservar')
            self.assertEqual((target/'personal.txt').read_text(), 'preservar')
            self.assertEqual((target/'extension.js').read_bytes(), (stage/'extension.js').read_bytes())

    def test_failed_publication_restores_content_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            stage, target = Path(directory)/'stage', Path(directory)/'target'
            stage.mkdir();target.mkdir()
            (stage/'first').write_text('novo');(stage/'second').write_text('novo')
            (target/'first').write_text('anterior');(target/'first').chmod(0o700)
            copy = installer.shutil.copy2
            def fail_second(source, destination):
                if source.name == 'second':
                    raise OSError('Falha de cópia exercitada pelo teste')
                return copy(source, destination)
            with patch.object(installer.shutil, 'copy2', side_effect=fail_second):
                with self.assertRaises(OSError):
                    installer.publish_payload(stage, target)
            self.assertEqual((target/'first').read_text(), 'anterior')
            self.assertEqual(stat.S_IMODE((target/'first').stat().st_mode), 0o700)
            self.assertFalse((target/'second').exists())
            self.assertFalse(list(target.glob('*.new')))

    def test_missing_models_do_not_download_without_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'helper'
            with patch.object(setup.urllib.request, 'urlopen', side_effect=AssertionError('Download inesperado')):
                with self.assertRaisesRegex(RuntimeError, '--models'):
                    setup.prepare_models(target)
            self.assertFalse(target.exists())

    def test_bad_download_does_not_replace_existing_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);target = root/'target'
            (target/'models').mkdir(parents=True)
            (target/'models/model.onnx').write_bytes(b'original')
            (root/'models.lock.json').write_text(json.dumps({'model.onnx': {
                'url': 'https://example.invalid/model', 'bytes': 3, 'sha256': '0'*64}}))
            with patch.object(setup, 'ROOT', root), patch.object(setup.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                    setup.prepare_models(target, download=True)
            self.assertEqual((target/'models/model.onnx').read_bytes(), b'original')
            self.assertFalse((target/'models/model.onnx.download').exists())


if __name__ == '__main__':
    unittest.main()
