#!/usr/bin/env python3

import csv
from datetime import date
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from agent.monthly_xlsx import (
    CSV_FIELDS, MonthlyWorkbookError, build_monthly_workbook,
    archive_old_monthly_workbooks,
    update_monthly_workbooks,
)
from agent.storage import save_local_sample
from cluster_common import PROTOCOL_VERSION
from cluster_common.protocol import normalize_sample


MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


class MonthlyWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.daily_dir = os.path.join(self.temporary.name, 'daily')
        self.monthly_dir = os.path.join(self.temporary.name, 'monthly')
        os.makedirs(self.daily_dir)
        self.node_info = {
            'node_id': 'node-01',
            'node_name': '910C 节点/01',
            'cluster_id': 'training-a',
        }

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

    def test_builds_one_sheet_per_completed_china_day_with_node_metadata(self):
        self.write_day('2026-08-01', [self.row('2026-08-01T00:00:00+00:00')])
        self.write_day('2026-08-02', [self.row('2026-08-02T00:00:00+00:00')])
        self.write_day('2026-08-03', [self.row('2026-08-03T00:00:00+00:00')])

        updated = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 2), self.node_info
        )

        self.assertEqual(len(updated), 1)
        self.assertEqual(
            os.path.basename(updated[0]), '910C_节点_01_2026-08.xlsx'
        )
        self.assertEqual(self.sheet_names(updated[0]), ['2026-08-01', '2026-08-02'])
        with zipfile.ZipFile(updated[0]) as archive:
            self.assertIsNone(archive.testzip())
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
        values = [''.join(cell.itertext()) for cell in sheet.findall(
            './/{{{}}}c'.format(MAIN_NS)
        )]
        self.assertIn('node-01', values)
        self.assertIn('910C 节点/01', values)
        self.assertIn('training-a', values)
        self.assertIn('UTC+08:00', values)
        self.assertIn('2026-08-02T08:00:00+08:00', values)
        self.assertIn('50.0', values)

    def test_deduplicates_timestamp_and_card(self):
        duplicate = self.row('2026-08-01T00:00:00+00:00')
        self.write_day('2026-08-01', [duplicate, duplicate])
        workbook_path = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 1), self.node_info
        )[0]
        with zipfile.ZipFile(workbook_path) as archive:
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        rows = sheet.findall('.//{{{}}}row'.format(MAIN_NS))
        self.assertEqual(len(rows), 7)

    def test_creates_empty_sheet_for_missing_day_after_monitoring_started(self):
        self.write_day('2026-08-01', [self.row('2026-08-01T00:00:00+00:00')])
        self.write_day('2026-08-03', [self.row('2026-08-03T00:00:00+00:00')])
        workbook_path = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 3), self.node_info
        )[0]
        self.assertEqual(
            self.sheet_names(workbook_path),
            ['2026-08-01', '2026-08-02', '2026-08-03'],
        )
        with zipfile.ZipFile(workbook_path) as archive:
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
        rows = sheet.findall('.//{{{}}}row'.format(MAIN_NS))
        self.assertEqual(len(rows), 6)

    def test_closed_month_is_not_rewritten(self):
        december_path = self.write_day(
            '2025-12-31', [self.row('2025-12-31T00:00:00+00:00')]
        )
        self.write_day('2026-01-01', [self.row('2026-01-01T00:00:00+00:00')])
        update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 1, 1), self.node_info
        )
        output_path = os.path.join(
            self.monthly_dir, '910C_节点_01_2025-12.xlsx'
        )
        with open(output_path, 'rb') as handle:
            original = handle.read()
        with open(december_path, 'a', encoding='utf-8') as handle:
            handle.write('2025-12-31T00:01:00+00:00,1,99,2048,65536\n')

        update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 1, 2), self.node_info
        )
        with open(output_path, 'rb') as handle:
            self.assertEqual(handle.read(), original)

    def test_invalid_csv_does_not_replace_existing_workbook(self):
        csv_path = self.write_day(
            '2026-08-01', [self.row('2026-08-01T00:00:00+00:00')]
        )
        output_path = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 1), self.node_info
        )[0]
        with open(output_path, 'rb') as handle:
            original = handle.read()
        with open(csv_path, 'a', encoding='utf-8') as handle:
            handle.write('broken,row\n')

        with self.assertRaises(MonthlyWorkbookError):
            build_monthly_workbook(
                [(date(2026, 8, 1), csv_path)], output_path, self.node_info
            )
        with open(output_path, 'rb') as handle:
            self.assertEqual(handle.read(), original)

    def test_monthly_workbooks_are_archived_after_hot_retention(self):
        os.makedirs(self.monthly_dir)
        archive_dir = os.path.join(self.temporary.name, 'archive')
        old_path = os.path.join(self.monthly_dir, 'stats_2025-12.xlsx')
        current_path = os.path.join(self.monthly_dir, 'stats_2026-08.xlsx')
        for path in (old_path, current_path):
            with open(path, 'wb') as handle:
                handle.write(b'test')
        archived = archive_old_monthly_workbooks(
            self.monthly_dir, archive_dir,
            retention_days=180, now_date=date(2026, 8, 19)
        )
        self.assertEqual(archived, 1)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(os.path.join(
            archive_dir, 'monthly', 'stats_2025-12.xlsx'
        )))
        self.assertTrue(os.path.exists(current_path))

    def test_existing_utc_csv_is_regrouped_at_china_midnight(self):
        self.write_day('2026-08-01', [
            self.row('2026-08-01T15:59:00+00:00', utilization=10),
            self.row('2026-08-01T16:00:00+00:00', utilization=20),
        ])
        updated = update_monthly_workbooks(
            self.daily_dir, self.monthly_dir, date(2026, 8, 2), self.node_info
        )
        self.assertEqual(self.sheet_names(updated[0]), [
            '2026-08-01', '2026-08-02'
        ])
        with zipfile.ZipFile(updated[0]) as archive:
            first = archive.read('xl/worksheets/sheet1.xml').decode('utf-8')
            second = archive.read('xl/worksheets/sheet2.xml').decode('utf-8')
        self.assertIn('2026-08-01T23:59:00+08:00', first)
        self.assertNotIn('2026-08-02T00:00:00+08:00', first)
        self.assertIn('2026-08-02T00:00:00+08:00', second)

    def test_local_csv_and_status_are_stored_by_china_date(self):
        status_dir = os.path.join(self.temporary.name, 'status')
        sample = normalize_sample({
            'protocol_version': PROTOCOL_VERSION,
            'cluster_id': 'training-a',
            'node_id': 'node-01',
            'node_name': 'node-01',
            'collected_at': '2026-08-01T16:00:00+00:00',
            'collect_interval': 60,
            'expected_cards': 1,
            'cards': [{
                'card_id': 0, 'utilization': 50,
                'hbm_used_mb': 100, 'hbm_total_mb': 1000,
            }],
        })
        save_local_sample(sample, self.daily_dir, status_dir)
        csv_path = os.path.join(self.daily_dir, 'stats_2026-08-02.csv')
        status_path = os.path.join(status_dir, 'samples_2026-08-02.jsonl')
        with open(csv_path, encoding='utf-8', newline='') as handle:
            rows = list(csv.DictReader(handle))
        with open(status_path, encoding='utf-8') as handle:
            status = json.loads(handle.readline())
        self.assertEqual(rows[0]['timestamp'], '2026-08-02T00:00:00+08:00')
        self.assertEqual(status['collected_at'], '2026-08-02T00:00:00+08:00')


if __name__ == '__main__':
    unittest.main(verbosity=2)
