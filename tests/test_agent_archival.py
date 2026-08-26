#!/usr/bin/env python3
from datetime import datetime, timezone
import os
import tempfile
import unittest

from agent.storage import archive_old_local_data, archive_rejected


class AgentArchivalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.archive = os.path.join(self.temporary.name, 'archive')

    def tearDown(self):
        self.temporary.cleanup()

    def test_daily_and_status_files_are_moved_instead_of_deleted(self):
        daily = os.path.join(self.temporary.name, 'daily')
        status = os.path.join(self.temporary.name, 'sample_status')
        os.makedirs(daily)
        os.makedirs(status)
        old_csv = os.path.join(daily, 'stats_2025-01-01.csv')
        current_csv = os.path.join(daily, 'stats_2026-08-20.csv')
        old_status = os.path.join(status, 'samples_2025-01-01.jsonl')
        for path in (old_csv, current_csv, old_status):
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('test\n')

        count = archive_old_local_data(
            (daily, status), self.archive, 180,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        self.assertEqual(count, 2)
        self.assertTrue(os.path.exists(current_csv))
        self.assertTrue(os.path.exists(os.path.join(
            self.archive, 'daily', os.path.basename(old_csv)
        )))
        self.assertTrue(os.path.exists(os.path.join(
            self.archive, 'sample_status', os.path.basename(old_status)
        )))

    def test_rejected_files_are_moved_instead_of_deleted(self):
        rejected = os.path.join(self.temporary.name, 'rejected')
        os.makedirs(rejected)
        path = os.path.join(rejected, 'old.json')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('{}\n')
        old_epoch = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(path, (old_epoch, old_epoch))

        count = archive_rejected(
            rejected, self.archive, 180,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        self.assertEqual(count, 1)
        self.assertTrue(os.path.exists(os.path.join(
            self.archive, 'rejected', 'old.json'
        )))


if __name__ == '__main__':
    unittest.main()
