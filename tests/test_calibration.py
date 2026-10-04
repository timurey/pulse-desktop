"""Калибровка устройства для реконструкции (calibration.py): из bag'а, по дате, вручную."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import calibration as C                           # noqa: E402

STRING_DEF = 'string data'


def write_bag(path, calib=None):
    """Минимальный MCAP (ros2) с /pulse/calibration — как будет писать Pi."""
    from mcap_ros2.writer import Writer
    with open(path, 'wb') as f:
        w = Writer(f)
        schema = w.register_msgdef('std_msgs/msg/String', STRING_DEF)
        if calib is not None:
            w.write_message(C.CALIB_TOPIC, schema, {'data': json.dumps(calib)}, log_time=1, publish_time=1)
        w.write_message('/other', schema, {'data': 'x'}, log_time=2, publish_time=2)
        w.finish()


class TestCalibration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_table_by_date(self):
        old = self.tmp / 'static_20260922_231157_0.mcap'
        new = self.tmp / 'static_20261004_201358_0.mcap'
        write_bag(old)
        write_bag(new)
        self.assertEqual(C.resolve(old)['mount_rpy_deg'][2], -1.6)
        self.assertEqual(C.resolve(new)['mount_rpy_deg'][2], -0.6)
        self.assertEqual(C.resolve(new)['source'], 'table')
        self.assertNotEqual(C.resolve(old)['fingerprint'], C.resolve(new)['fingerprint'])

    def test_from_bag_and_override(self):
        b = self.tmp / 'static_20261004_220000_0.mcap'
        # calib.json веб-HMI: только yaw + поправка наклона
        write_bag(b, {'mount_yaw_deg': -0.58, 'tilt_deg': 1.2, 'R': [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                      'yaw_bag': 'static_20261004_201358'})
        c = C.resolve(b)
        self.assertEqual(c['source'], 'bag')
        self.assertEqual(c['mount_rpy_deg'], [0.0, 0.0, -0.58])
        self.assertEqual(c['rotation_center'], [0.0, 0.0, -0.0108])      # недостающее — из таблицы
        self.assertIsNotNone(c['tilt_R'])
        o = C.resolve(b, {'mount_yaw_deg': -0.7})
        self.assertEqual(o['mount_rpy_deg'], [0.0, 0.0, -0.7])
        self.assertIn('ручная', o['source'])
        self.assertNotEqual(o['fingerprint'], c['fingerprint'])
        p = C.deskew_params(c)
        self.assertEqual(p['mount_rpy_deg'][2], -0.58)


if __name__ == '__main__':
    unittest.main()
