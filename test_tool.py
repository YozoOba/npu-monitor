#!/usr/bin/env python3
"""Repeatable standard-library tests that never touch production data."""
import csv
from datetime import date, datetime, timedelta, timezone
import json
import os
import tempfile
import unittest
from unittest import mock

import npu_monitor
import healthcheck
from stats_core import (
    calculate_coverage, calculate_overall, calculate_stats, read_daily_data,
    read_sample_status,
)


SAMPLE_OUTPUT = """\
+----------------------+----------------------+----------------------+
| NPU   Name           | Health               | Power(W)             |
+======================+======================+======================+
| 0     910B           | OK                   | 100.0                |
| 0                    | 0                    | 42.5% 0 / 0 1024 / 65536   |
| 1     910B           | OK                   | 101.0                |
| 0                    | 0                    | 7 2048 / 65536       |
+----------------------+----------------------+----------------------+
"""


class MonitorTests(unittest.TestCase):
    def tearDown(self):
        npu_monitor.STOP_EVENT.clear()

    def test_parse_npu_smi_output(self):
        cards = npu_monitor.parse_npu_smi_output(SAMPLE_OUTPUT)
        self.assertEqual([card['card_id'] for card in cards], [0, 1])
        self.assertEqual(cards[0]['utilization'], 42.5)
        self.assertEqual(cards[0]['hbm_used_mb'], 1024)
        self.assertEqual(cards[1]['utilization'], 7.0)

    def test_collection_keeps_incomplete_sample(self):
        completed = subprocess_result(stdout=SAMPLE_OUTPUT)
        with mock.patch.object(npu_monitor.subprocess, 'run', return_value=completed), \
                mock.patch.object(npu_monitor, 'EXPECTED_NPU_COUNT', 8):
            self.assertEqual(len(npu_monitor.collect_npu_data()), 2)

    def test_collection_accepts_expected_sample(self):
        completed = subprocess_result(stdout=SAMPLE_OUTPUT)
        with mock.patch.object(npu_monitor.subprocess, 'run', return_value=completed), \
                mock.patch.object(npu_monitor, 'EXPECTED_NPU_COUNT', 2):
            cards = npu_monitor.collect_npu_data()
        self.assertEqual(len(cards), 2)

    def test_save_and_read_csv(self):
        cards = npu_monitor.parse_npu_smi_output(SAMPLE_OUTPUT)
        collected_at = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary_directory:
            self.assertTrue(npu_monitor.save_to_csv(cards, collected_at, temporary_directory))
            csv_file = os.path.join(temporary_directory, 'stats_2026-08-12.csv')
            with open(csv_file, encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['timestamp'], '2026-08-12T12:30:00+00:00')

    def test_append_to_legacy_csv(self):
        cards = npu_monitor.parse_npu_smi_output(SAMPLE_OUTPUT)
        collected_at = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary_directory:
            csv_file = os.path.join(temporary_directory, 'stats_2026-08-12.csv')
            with open(csv_file, 'w', encoding='utf-8', newline='') as handle:
                handle.write('timestamp,card_id,utilization\n')
            self.assertTrue(npu_monitor.save_to_csv(cards, collected_at, temporary_directory))
            with open(csv_file, encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertNotIn(None, rows[0])

    def test_clean_old_data(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            old_file = os.path.join(temporary_directory, 'stats_2026-01-01.csv')
            kept_file = os.path.join(temporary_directory, 'stats_2026-08-11.csv')
            open(old_file, 'w').close()
            open(kept_file, 'w').close()
            deleted = npu_monitor.clean_old_data(
                today=date(2026, 8, 12),
                daily_dir=temporary_directory,
                retention_days=30,
            )
            self.assertEqual(deleted, 1)
            self.assertFalse(os.path.exists(old_file))
            self.assertTrue(os.path.exists(kept_file))

    def test_partial_sample_status_records_coverage(self):
        cards = npu_monitor.parse_npu_smi_output(SAMPLE_OUTPUT)
        collected_at = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary_directory, \
                mock.patch.object(npu_monitor, 'EXPECTED_NPU_COUNT', 8):
            record = npu_monitor.save_sample_status(
                collected_at, cards, True, status_dir=temporary_directory
            )
            self.assertEqual(record['status'], 'partial')
            self.assertEqual(record['coverage_percent'], 25.0)
            self.assertEqual(record['missing_card_ids'], [2, 3, 4, 5, 6, 7])
            records = read_sample_status(
                '2026-08-12', status_dir=temporary_directory
            )
            coverage = calculate_coverage(records)
            self.assertEqual(coverage['partial'], 1)
            self.assertEqual(coverage['coverage_percent'], 25.0)

    def test_health_json_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = os.path.join(temporary_directory, 'health.json')
            self.assertTrue(npu_monitor.write_json_atomic(path, {'status': 'healthy'}))
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['status'], 'healthy')
            self.assertEqual(
                [name for name in os.listdir(temporary_directory) if name.endswith('.tmp')],
                [],
            )

    def test_healthcheck_rejects_stale_attempt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = os.path.join(temporary_directory, 'health.json')
            attempted_at = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'status': 'healthy',
                    'last_attempt': attempted_at.isoformat(),
                }, handle)
            healthy, message = healthcheck.check_health(
                path=path,
                max_age=60,
                now=attempted_at + timedelta(seconds=61),
            )
            self.assertFalse(healthy)
            self.assertIn('stale', message)

    def test_healthcheck_accepts_degraded_fresh_attempt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = os.path.join(temporary_directory, 'health.json')
            attempted_at = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'status': 'degraded',
                    'last_attempt': attempted_at.isoformat(),
                }, handle)
            healthy, _message = healthcheck.check_health(
                path=path,
                max_age=60,
                now=attempted_at + timedelta(seconds=30),
            )
            self.assertTrue(healthy)

    def test_overall_inputs_support_weighting(self):
        stats = calculate_stats([
            {'card_id': 0, 'utilization': 100.0},
            {'card_id': 1, 'utilization': 0.0},
            {'card_id': 1, 'utilization': 0.0},
            {'card_id': 1, 'utilization': 0.0},
        ])
        count, overall = calculate_overall(stats)
        self.assertEqual(count, 4)
        self.assertEqual(overall, 25.0)

    def test_reader_skips_invalid_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = os.path.join(temporary_directory, 'stats_2026-08-12.csv')
            with open(path, 'w', encoding='utf-8', newline='') as handle:
                handle.write('timestamp,card_id,utilization\n')
                handle.write('2026-08-12 00:00:00,0,50\n')
                handle.write('broken,row\n')
            rows = read_daily_data('2026-08-12', daily_dir=temporary_directory)
            self.assertEqual(len(rows), 1)

    def test_reader_deduplicates_timestamp_and_card(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = os.path.join(temporary_directory, 'stats_2026-08-12.csv')
            with open(path, 'w', encoding='utf-8', newline='') as handle:
                handle.write('timestamp,card_id,utilization\n')
                handle.write('2026-08-12T00:00:00+08:00,0,10\n')
                handle.write('2026-08-12T00:00:00+08:00,0,20\n')
            rows = read_daily_data('2026-08-12', daily_dir=temporary_directory)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['utilization'], 20.0)

    def test_pid_file_is_released_by_owner(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            pid_file = os.path.join(temporary_directory, 'monitor.pid')
            old_pid_file = npu_monitor.PID_FILE
            try:
                npu_monitor.acquire_pid_file(pid_file)
                self.assertTrue(os.path.exists(pid_file))
                npu_monitor.release_pid_file()
                self.assertFalse(os.path.exists(pid_file))
            finally:
                npu_monitor.PID_FILE = old_pid_file


def subprocess_result(stdout='', stderr='', returncode=0):
    return type('Completed', (), {
        'stdout': stdout,
        'stderr': stderr,
        'returncode': returncode,
    })()


if __name__ == '__main__':
    unittest.main(verbosity=2)
