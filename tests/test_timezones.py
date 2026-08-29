#!/usr/bin/env python3

import unittest

from cluster_common.timezones import (
    resolve_timezone, timezone_label, timezone_offset_seconds,
)


class TimezoneTests(unittest.TestCase):
    def test_accepts_auto_utc_and_fixed_offsets(self):
        automatic = resolve_timezone('auto')
        self.assertLessEqual(abs(timezone_offset_seconds(automatic)), 14 * 3600)
        self.assertEqual(timezone_offset_seconds(resolve_timezone('UTC')), 0)
        self.assertEqual(
            timezone_offset_seconds(resolve_timezone('UTC+08:00')), 8 * 3600
        )
        self.assertEqual(
            timezone_offset_seconds(resolve_timezone('-05:30')), -19800
        )
        self.assertEqual(timezone_label(-19800), 'UTC-05:30')

    def test_rejects_invalid_timezone_settings(self):
        for value in ('Asia/Shanghai', 'UTC+15:00', 'UTC+08:99'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_timezone(value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
