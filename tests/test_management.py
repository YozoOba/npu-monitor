#!/usr/bin/env python3
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from urllib.request import urlopen

from cluster_common import PROTOCOL_VERSION
from cluster_common.protocol import normalize_sample
from collector.app import CollectorApplication, ThreadingHTTPServer, make_handler
from collector.management import (
    ConfirmationMismatchError, ManagementService,
)
from collector.storage import CollectorStorage
from console.client import CollectorClient
from console.web import (
    ThreadingHTTPServer as ConsoleHTTPServer,
    make_handler as make_console_handler,
)


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def sample(node_id, when, cluster_id='training-a', node_name=None):
    return normalize_sample({
        'protocol_version': PROTOCOL_VERSION,
        'cluster_id': cluster_id,
        'node_id': node_id,
        'node_name': node_name or node_id + '-name',
        'collected_at': when.isoformat(timespec='seconds'),
        'collect_interval': 60,
        'expected_cards': 2,
        'cards': [
            {'card_id': 0, 'utilization': 10,
             'hbm_used_mb': 100, 'hbm_total_mb': 1000},
            {'card_id': 1, 'utilization': 20,
             'hbm_used_mb': 200, 'hbm_total_mb': 1000},
        ],
    }, received_at=when + timedelta(seconds=1))


class ManagementStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.hot_path = os.path.join(self.temporary.name, 'cluster.sqlite3')
        self.archive_path = os.path.join(self.temporary.name, 'archive.sqlite3')
        self.backup_dir = os.path.join(self.temporary.name, 'backups')
        self.storage = CollectorStorage(self.hot_path)
        self.service = ManagementService(
            self.storage, self.archive_path, self.backup_dir
        )

    def tearDown(self):
        self.storage.close()
        self.temporary.cleanup()

    def execute_preview(self, request):
        preview = self.service.preview(request)
        return self.service.execute(dict(
            request, confirmation_token=preview['confirmation_token']
        ))

    def test_delete_node_covers_hot_and_archive_after_verified_backups(self):
        old = sample('wrong-node', NOW - timedelta(days=200))
        current = sample('wrong-node', NOW)
        self.storage.ingest(old, NOW - timedelta(days=200))
        self.storage.ingest(current, NOW)
        self.storage.archive_before(
            int((NOW - timedelta(days=180)).timestamp()), self.archive_path
        )

        request = {
            'operation': 'delete_node', 'node_id': 'wrong-node',
            'include_archive': True,
        }
        preview = self.service.preview(request)
        self.assertEqual(preview['impact']['hot']['samples'], 1)
        self.assertEqual(preview['impact']['archive']['samples'], 1)
        result = self.service.execute(dict(
            request, confirmation_token=preview['confirmation_token']
        ))

        self.assertEqual(self.storage.health()['sample_count'], 0)
        self.assertEqual(self.storage.connection.execute(
            'SELECT COUNT(*) FROM nodes'
        ).fetchone()[0], 0)
        archive = sqlite3.connect(self.archive_path)
        try:
            self.assertEqual(archive.execute(
                'SELECT COUNT(*) FROM samples'
            ).fetchone()[0], 0)
        finally:
            archive.close()
        self.assertEqual(set(result['backups']), {'hot', 'archive'})
        for path in result['backups'].values():
            self.assertTrue(os.path.exists(path))
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute('PRAGMA quick_check(1)').fetchone()[0], 'ok'
                )
            finally:
                connection.close()
        self.assertEqual(self.service.operations()['total'], 1)

    def test_delete_latest_sample_repairs_node_pointer(self):
        first = sample('node-01', NOW - timedelta(minutes=1))
        latest = sample('node-01', NOW)
        self.storage.ingest(first, NOW - timedelta(minutes=1))
        self.storage.ingest(latest, NOW)

        self.execute_preview({
            'operation': 'delete_samples',
            'sample_id': latest['sample_id'],
            'include_archive': True,
            'delete_alerts': False,
        })
        node = self.storage.connection.execute(
            'SELECT latest_sample_id FROM nodes WHERE node_id = ?', ('node-01',)
        ).fetchone()
        self.assertEqual(node['latest_sample_id'], first['sample_id'])
        self.assertEqual(self.storage.health()['sample_count'], 1)

    def test_stale_confirmation_is_rejected_without_mutation(self):
        self.storage.ingest(sample('node-01', NOW), NOW)
        request = {
            'operation': 'delete_node', 'node_id': 'node-01',
            'include_archive': True,
        }
        preview = self.service.preview(request)
        self.storage.ingest(
            sample('node-01', NOW + timedelta(minutes=1)),
            NOW + timedelta(minutes=1),
        )
        with self.assertRaises(ConfirmationMismatchError):
            self.service.execute(dict(
                request, confirmation_token=preview['confirmation_token']
            ))
        self.assertEqual(self.storage.health()['sample_count'], 2)
        self.assertFalse(os.path.exists(self.backup_dir))

    def test_update_node_metadata_is_backed_up_and_audited(self):
        self.storage.ingest(sample('node-01', NOW), NOW)
        result = self.execute_preview({
            'operation': 'update_node', 'node_id': 'node-01',
            'node_name': 'correct-name', 'cluster_id': 'correct-cluster',
        })
        node = self.storage.connection.execute(
            'SELECT cluster_id, node_name FROM nodes WHERE node_id = ?', ('node-01',)
        ).fetchone()
        self.assertEqual(tuple(node), ('correct-cluster', 'correct-name'))
        self.assertTrue(os.path.exists(result['backups']['hot']))
        self.assertEqual(self.service.operations()['items'][0]['operation'],
                         'update_node')

    def test_global_time_range_delete_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'node_id or cluster_id'):
            self.service.preview({
                'operation': 'delete_samples',
                'start': (NOW - timedelta(hours=1)).isoformat(),
                'end': NOW.isoformat(),
            })


class ManagementHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = CollectorStorage(os.path.join(
            self.temporary.name, 'cluster.sqlite3'
        ))
        self.application = CollectorApplication(self.storage)
        self.application.management = ManagementService(
            self.storage,
            os.path.join(self.temporary.name, 'archive.sqlite3'),
            os.path.join(self.temporary.name, 'backups'),
        )
        self.collector = ThreadingHTTPServer(
            ('127.0.0.1', 0), make_handler(self.application)
        )
        self.collector_thread = threading.Thread(
            target=self.collector.serve_forever, daemon=True
        )
        self.collector_thread.start()
        self.collector_url = 'http://127.0.0.1:{}'.format(
            self.collector.server_address[1]
        )
        self.client = CollectorClient(self.collector_url, timeout=2)

    def tearDown(self):
        self.collector.shutdown()
        self.collector.server_close()
        self.collector_thread.join(timeout=2)
        self.storage.close()
        self.temporary.cleanup()

    def test_collector_and_console_expose_preview_execute_and_filters(self):
        self.storage.ingest(sample('wrong-node', NOW), NOW)
        request = {
            'operation': 'delete_node', 'node_id': 'wrong-node',
            'include_archive': True,
        }
        preview = self.client.management_preview(request)
        self.client.management_execute(dict(
            request, confirmation_token=preview['confirmation_token']
        ))
        self.assertEqual(self.client.management_operations()['total'], 1)

        console = ConsoleHTTPServer(
            ('127.0.0.1', 0), make_console_handler(self.client)
        )
        thread = threading.Thread(target=console.serve_forever, daemon=True)
        thread.start()
        base_url = 'http://127.0.0.1:{}'.format(console.server_address[1])
        try:
            with urlopen(base_url + '/', timeout=2) as response:
                html = response.read().decode('utf-8')
            for text in (
                    '错误节点与错误数据管理', 'sampleStatus',
                    'alertSeverity', 'alertType', 'samplePager', 'alertPager'):
                self.assertIn(text, html)
            with urlopen(base_url + '/api/admin/operations', timeout=2) as response:
                self.assertEqual(json.loads(response.read())['total'], 1)
        finally:
            console.shutdown()
            console.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
