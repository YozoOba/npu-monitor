#!/usr/bin/env python3
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent.sender import UploadWorker
from agent.storage import enqueue, queue_usage
from cluster_common import PROTOCOL_VERSION
from cluster_common.protocol import ProtocolError, normalize_sample
from collector.app import CollectorApplication, ThreadingHTTPServer, make_handler
from collector.storage import CollectorStorage, ConflictingSampleError
from console.client import CollectorClient
from console.web import ThreadingHTTPServer as ConsoleHTTPServer
from console.web import make_handler as make_console_handler


NOW = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)


def payload(node='node-01', collected_at=NOW, cards=None, expected=2):
    if cards is None:
        cards = [
            {'card_id': 0, 'utilization': 20, 'hbm_used_mb': 100, 'hbm_total_mb': 1000},
            {'card_id': 1, 'utilization': 80, 'hbm_used_mb': 500, 'hbm_total_mb': 1000},
        ]
    return {
        'protocol_version': PROTOCOL_VERSION,
        'node_id': node,
        'node_name': node,
        'collected_at': collected_at.isoformat(timespec='seconds'),
        'collect_interval': 60,
        'expected_cards': expected,
        'cards': cards,
    }


def normalized(**kwargs):
    value = payload(**kwargs)
    return normalize_sample(value, received_at=NOW + timedelta(seconds=1))


class ProtocolTests(unittest.TestCase):
    def test_normalizes_complete_sample_to_utc(self):
        local_time = NOW.astimezone(timezone(timedelta(hours=8)))
        value = payload(collected_at=local_time)
        result = normalize_sample(value, received_at=NOW)
        self.assertEqual(result['collected_at'], NOW.isoformat(timespec='seconds'))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['coverage_percent'], 100.0)

    def test_partial_sample_is_recomputed(self):
        result = normalized(cards=[{'card_id': 1, 'utilization': 10,
                                    'hbm_used_mb': None, 'hbm_total_mb': None}])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['missing_card_ids'], [0])

    def test_rejects_duplicate_or_out_of_range_cards(self):
        card = {'card_id': 0, 'utilization': 10, 'hbm_used_mb': None, 'hbm_total_mb': None}
        with self.assertRaises(ProtocolError):
            normalized(cards=[card, dict(card)])
        with self.assertRaises(ProtocolError):
            normalized(cards=[dict(card, card_id=2)])

    def test_rejects_future_sample(self):
        with self.assertRaises(ProtocolError):
            normalize_sample(
                payload(collected_at=NOW + timedelta(minutes=6)), received_at=NOW,
                max_future_seconds=300,
            )


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = CollectorStorage(os.path.join(self.temporary.name, 'cluster.db'))

    def tearDown(self):
        self.storage.close()
        self.temporary.cleanup()

    def test_ingest_is_idempotent_and_conflicts_are_rejected(self):
        sample = normalized()
        self.assertTrue(self.storage.ingest(sample, NOW + timedelta(seconds=1)))
        self.assertFalse(self.storage.ingest(sample, NOW + timedelta(seconds=2)))
        changed = normalized()
        changed['cards'][0]['utilization'] = 99.0
        with self.assertRaises(ConflictingSampleError):
            self.storage.ingest(changed, NOW + timedelta(seconds=3))
        self.assertEqual(self.storage.health()['sample_count'], 1)

    def test_backfill_does_not_replace_latest_sample(self):
        latest = normalized(collected_at=NOW)
        older = normalize_sample(
            payload(collected_at=NOW - timedelta(hours=1)), received_at=NOW
        )
        self.storage.ingest(latest, NOW)
        self.storage.ingest(older, NOW + timedelta(seconds=1))
        values = self.storage.latest_samples()
        self.assertEqual(values[0]['sample']['sample_id'], latest['sample_id'])

    def test_snapshot_uses_card_weighting_and_marks_offline(self):
        self.storage.ingest(normalized(node='node-a'), NOW)
        one_card = normalize_sample(payload(
            node='node-b', cards=[{'card_id': 0, 'utilization': 100,
                                   'hbm_used_mb': None, 'hbm_total_mb': None}],
            expected=2,
        ), received_at=NOW)
        self.storage.ingest(one_card, NOW)
        snapshot = self.storage.build_snapshot(120, 300, now=NOW + timedelta(seconds=10))
        self.assertEqual(snapshot['node_counts']['online'], 1)
        self.assertEqual(snapshot['node_counts']['degraded'], 1)
        self.assertEqual(snapshot['utilization_avg'], 66.67)
        later = self.storage.build_snapshot(120, 300, now=NOW + timedelta(seconds=301))
        self.assertEqual(later['node_counts']['offline'], 2)
        self.assertIsNone(later['utilization_avg'])

    def test_history_series_is_bucketed(self):
        self.storage.ingest(normalized(), NOW)
        values = self.storage.history_series(
            int((NOW - timedelta(minutes=1)).timestamp()),
            int((NOW + timedelta(minutes=1)).timestamp()), 60,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['utilization_avg'], 50.0)
        self.assertEqual(values[0]['card_samples'], 2)
        self.assertEqual(values[0]['coverage_percent'], 100.0)


class HttpAndQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.storage = CollectorStorage(os.path.join(self.temporary.name, 'cluster.db'))
        self.snapshot_patch = mock.patch(
            'collector.config.SNAPSHOT_PATH',
            os.path.join(self.temporary.name, 'snapshot.json'),
        )
        self.snapshot_patch.start()
        self.application = CollectorApplication(self.storage)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.application))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:{}'.format(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.storage.close()
        self.snapshot_patch.stop()
        self.temporary.cleanup()

    def post(self, value):
        request = Request(
            self.base_url + '/api/v1/samples',
            data=json.dumps(value).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        return urlopen(request, timeout=2)

    def test_http_ingest_snapshot_and_console_client(self):
        response = self.post(payload())
        self.assertEqual(response.getcode(), 201)
        duplicate = self.post(payload())
        self.assertEqual(duplicate.getcode(), 200)
        client = CollectorClient(self.base_url, timeout=2)
        snapshot = client.snapshot()
        self.assertEqual(snapshot['total_nodes'], 1)
        self.assertEqual(client.health()['database_integrity'], 'ok')

    def test_http_rejects_inaccurate_measurement(self):
        invalid = payload(cards=[{'card_id': 0, 'utilization': 101,
                                  'hbm_used_mb': None, 'hbm_total_mb': None}])
        with self.assertRaises(HTTPError) as caught:
            self.post(invalid)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertEqual(self.storage.health()['sample_count'], 0)

    def test_upload_worker_drains_durable_queue(self):
        spool = os.path.join(self.temporary.name, 'spool')
        rejected = os.path.join(self.temporary.name, 'rejected')
        os.makedirs(spool)
        sample = normalized()
        enqueue(sample, spool, 10, 1024 * 1024)
        worker = UploadWorker(
            self.base_url, spool, rejected,
            os.path.join(self.temporary.name, 'upload_health.json'),
            timeout=2, batch_size=10,
        )
        worker.start()
        worker.notify()
        deadline = time.time() + 3
        while time.time() < deadline and queue_usage(spool)[0]:
            time.sleep(0.05)
        worker.stop()
        worker.join(timeout=2)
        self.assertEqual(queue_usage(spool)[0], [])
        self.assertEqual(self.storage.health()['sample_count'], 1)

    def test_console_web_is_independently_served(self):
        self.post(payload()).close()
        console_server = ConsoleHTTPServer(
            ('127.0.0.1', 0),
            make_console_handler(CollectorClient(self.base_url, timeout=2)),
        )
        thread = threading.Thread(target=console_server.serve_forever, daemon=True)
        thread.start()
        console_url = 'http://127.0.0.1:{}'.format(console_server.server_address[1])
        try:
            with urlopen(console_url + '/', timeout=2) as response:
                self.assertIn('NPU 集群状态', response.read().decode('utf-8'))
            with urlopen(console_url + '/api/snapshot', timeout=2) as response:
                self.assertEqual(json.loads(response.read())['total_nodes'], 1)
        finally:
            console_server.shutdown()
            console_server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
