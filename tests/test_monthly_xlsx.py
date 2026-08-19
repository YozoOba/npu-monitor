#!/usr/bin/env python3

import csv
from datetime import date
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from agent.monthly_xlsx import (
    CSV_FIELDS, MonthlyWorkbookError, build_monthly_workbook,
    clean_old_monthly_workbooks,
    update_monthly_workbooks,
)


MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


class MonthlyWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.daily_dir = os.path.join(self.temporary.name, 'daily')
        self.monthly_dir = os.path.join(self.temporary.name, 'monthly')
        os.makedirs(self.daily_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def write_day(self, day, rows):
        path = os.path.join(self.daily_dir, 'stats_{}.csv'.format(day))
        with open(path, 'w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    @staticmethod
    def row(timestamp, card_id=0, utilization=50):
        return {
            'timestamp': timestamp,
            'card_id': card_id,
            'utilization': utilization,
            'hbm_used_mb': 1024,
            'hbm_total_mb': 65536,
        }

    def sheet_names(self, workbook_path):
        with zipfile.ZipFile(workbook_path) as archive:
            root = ET.fromstring(archive.read('xl/workbook.xml'))
        return [
            value.attrib['name']
            for value in root.findall('.//{{{}}}sheet'.format(MAIN_NS))
        ]

    def test_builds_one_sheet_per_completed_utc_day(self):
        self.write_day('2026-08-01', [self.row('2026-08-01T00:00:00+00:00')])
        self.write_day('2026-08-02', [self.row('2026-08-02T00:00:00+00:00')])
        self.write_day('2026-08-03', [self.row('2026-08-03T00:00:00+00:00')])

        updated = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 2)
        )

        self.assertEqual(len(updated), 1)
        self.assertEqual(os.path.basename(updated[0]), 'stats_2026-08.xlsx')
        self.assertEqual(self.sheet_names(updated[0]), ['2026-08-01', '2026-08-02'])
        with zipfile.ZipFile(updated[0]) as archive:
            self.assertIsNone(archive.testzip())
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
        values = [''.join(cell.itertext()) for cell in sheet.findall(
            './/{{{}}}c'.format(MAIN_NS)
        )]
        self.assertIn('2026-08-02T00:00:00+00:00', values)
        self.assertIn('50.0', values)

    def test_deduplicates_timestamp_and_card(self):
        duplicate = self.row('2026-08-01T00:00:00+00:00')
        self.write_day('2026-08-01', [duplicate, duplicate])
        workbook_path = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 1)
        )[0]
        with zipfile.ZipFile(workbook_path) as archive:
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        rows = sheet.findall('.//{{{}}}row'.format(MAIN_NS))
        self.assertEqual(len(rows), 2)

    def test_creates_empty_sheet_for_missing_day_after_monitoring_started(self):
        self.write_day('2026-08-01', [self.row('2026-08-01T00:00:00+00:00')])
        self.write_day('2026-08-03', [self.row('2026-08-03T00:00:00+00:00')])
        workbook_path = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 3)
        )[0]
        self.assertEqual(
            self.sheet_names(workbook_path),
            ['2026-08-01', '2026-08-02', '2026-08-03'],
        )
        with zipfile.ZipFile(workbook_path) as archive:
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
        rows = sheet.findall('.//{{{}}}row'.format(MAIN_NS))
        self.assertEqual(len(rows), 1)

    def test_closed_month_is_not_rewritten(self):
        december_path = self.write_day(
            '2025-12-31', [self.row('2025-12-31T00:00:00+00:00')]
        )
        self.write_day('2026-01-01', [self.row('2026-01-01T00:00:00+00:00')])
        update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 1, 1)
        )
        output_path = os.path.join(self.monthly_dir, 'stats_2025-12.xlsx')
        with open(output_path, 'rb') as handle:
            original = handle.read()
        with open(december_path, 'a', encoding='utf-8') as handle:
            handle.write('2025-12-31T00:01:00+00:00,1,99,2048,65536\n')

        update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 1, 2)
        )
        with open(output_path, 'rb') as handle:
            self.assertEqual(handle.read(), original)

    def test_invalid_csv_does_not_replace_existing_workbook(self):
        csv_path = self.write_day(
            '2026-08-01', [self.row('2026-08-01T00:00:00+00:00')]
        )
        output_path = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 1)
        )[0]
        with open(output_path, 'rb') as handle:
            original = handle.read()
        with open(csv_path, 'a', encoding='utf-8') as handle:
            handle.write('broken,row\n')

        with self.assertRaises(MonthlyWorkbookError):
            build_monthly_workbook(
                [(date(2026, 8, 1), csv_path)], output_path
            )
        with open(output_path, 'rb') as handle:
            self.assertEqual(handle.read(), original)

    def test_monthly_workbooks_follow_agent_retention(self):
        os.makedirs(self.monthly_dir)
        old_path = os.path.join(self.monthly_dir, 'stats_2025-12.xlsx')
        current_path = os.path.join(self.monthly_dir, 'stats_2026-08.xlsx')
        for path in (old_path, current_path):
            with open(path, 'wb') as handle:
                handle.write(b'test')
        deleted = clean_old_monthly_workbooks(
            self.monthly_dir, retention_days=180, now_date=date(2026, 8, 19)
        )
        self.assertEqual(deleted, 1)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(current_path))


if __name__ == '__main__':
    unittest.main(verbosity=2)
