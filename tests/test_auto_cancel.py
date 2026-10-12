"""
Остановка автостыковки (Session.run_auto(cancel=...)): посчитанные пары остаются,
повторный запуск продолжает с места; оценка оставшегося времени в сообщениях.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_session import Session, fmt_eta          # noqa: E402
from test_session import write_scans, POSES        # noqa: E402


class TestAutoCancel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        write_scans(cls.tmp)
        cls.paths = [str(cls.tmp / n) for n in POSES]

    def test_cancel_and_resume(self):
        s = Session.from_scans(self.paths)
        for sc in s.scans:
            sc.analyze()
        msgs = []
        computed = []

        def progress(f, m):
            msgs.append(m)
            if m.startswith('стыковка: пара'):
                computed.append(m)

        r = s.run_auto(progress, by_tree=False, cancel=lambda: len(computed) >= 1)
        self.assertTrue(r['cancelled'])
        self.assertEqual((r['pairs'], r['done']), (3, 1))
        self.assertEqual(len([e for e in s.edges if 'T_canon' in e]), 1)
        self.assertTrue(any('пара 1 из 3' in m for m in msgs))
        # продолжение: первая пара берётся готовой, считаются только две оставшиеся
        computed.clear()
        msgs.clear()
        r = s.run_auto(progress, by_tree=False)
        self.assertFalse(r['cancelled'])
        self.assertEqual((r['pairs'], r['done']), (3, 3))
        self.assertEqual(len(computed), 2)
        self.assertTrue(any('осталось' in m for m in computed[1:]))
        self.assertEqual(len(s.placed()), len(s.scans))

    def test_fmt_eta(self):
        self.assertEqual(fmt_eta(12), '~10 с')
        self.assertEqual(fmt_eta(130), '~2 мин')
        self.assertEqual(fmt_eta(4900), '~1 ч 22 мин')


if __name__ == '__main__':
    unittest.main()
