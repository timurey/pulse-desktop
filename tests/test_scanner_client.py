"""Импорт со сканера: Blueprint веб-HMI (orangepi/hmi/bag_files.py) + клиент, докачка."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HMI = ROOT.parent / 'orangepi' / 'hmi'

try:
    import flask                                          # noqa: F401
    HAVE = HMI.exists()
except ImportError:
    HAVE = False

import scanner_client as scl                              # noqa: E402


@unittest.skipUnless(HAVE, 'нужен flask и orangepi/hmi (монорепо)')
class TestScanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        sys.path.insert(0, str(HMI))
        import bag_files
        cls.bags = Path(tempfile.mkdtemp())
        b = cls.bags / 'static_1'
        b.mkdir()
        cls.data = os.urandom(3_500_000)
        (b / 'static_1_0.mcap').write_bytes(cls.data)
        (b / 'metadata.yaml').write_text('rosbag2_bagfile_information: {}\n')
        app = flask.Flask('t')
        bag_files.init(str(cls.bags))
        app.register_blueprint(bag_files.bp)

        @app.route('/api/bags')
        def bags():
            return flask.jsonify({'bags': [{'name': 'static_1', 'size': '3.5 MB',
                                            'size_b': len(cls.data) + 32, 'mtime': 'x'}]})

        # как в app.py: удаление записи по <path:name> не должно перехватывать GET файлов
        @app.route('/api/bags/<path:name>', methods=['DELETE'])
        def delete(name):
            return flask.jsonify({'ok': True})

        @app.route('/api/status')
        def status():
            return flask.jsonify({'recording': False})
        cls.srv = make_server('127.0.0.1', 0, app, threaded=True)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.c = scl.ScannerClient(f'127.0.0.1:{cls.srv.server_port}')

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_list_and_download(self):
        self.assertEqual([b['name'] for b in self.c.bags()], ['static_1'])
        files = {f['path']: f['size'] for f in self.c.files('static_1')}
        self.assertEqual(files['static_1_0.mcap'], len(self.data))
        dest = Path(tempfile.mkdtemp())
        p = self.c.download('static_1', dest)
        self.assertEqual((p / 'static_1_0.mcap').read_bytes(), self.data)
        self.assertEqual(scl.local_state('static_1', dest), 'скачан')

    def test_resume(self):
        dest = Path(tempfile.mkdtemp())
        part = dest / 'static_1' / 'static_1_0.mcap.part'
        part.parent.mkdir(parents=True)
        part.write_bytes(self.data[:1_000_000])            # прерванная загрузка
        self.assertEqual(scl.local_state('static_1', dest), 'частично')
        seen = []
        self.c.download('static_1', dest, progress=lambda f, m: seen.append(f))
        self.assertEqual((dest / 'static_1' / 'static_1_0.mcap').read_bytes(), self.data)
        self.assertFalse(part.exists())
        mcap = [x for x in seen if x > 0.001]                # без маленького metadata.yaml
        self.assertGreater(min(mcap), 0.25)                 # продолжилось с 1 МБ, а не с нуля
        # повтор — всё уже скачано, ничего не качается
        seen.clear()
        self.c.download('static_1', dest, progress=lambda f, m: seen.append(f))
        self.assertEqual(seen, [1.0])

    def test_cancel_and_errors(self):
        dest = Path(tempfile.mkdtemp())
        with self.assertRaises(scl.Cancelled):
            self.c.download('static_1', dest, cancel=lambda: True)
        with self.assertRaises(scl.ScannerError):
            self.c.files('../etc')                           # выход за папку записей
        with self.assertRaises(scl.ScannerError):
            self.c.files('nope')
        with self.assertRaises(scl.ScannerError):
            scl.ScannerClient('127.0.0.1:1', timeout=1).bags()


if __name__ == '__main__':
    unittest.main()
