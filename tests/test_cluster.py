#!/usr/bin/env python3
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
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
        self.assertEqual(
            values[0]['latest_received_at'],
            (NOW + timedelta(seconds=1)).isoformat(timespec='seconds'),
        )

    def test_fixed_agent_clock_offset_uses_collector_receipt_for_freshness(self):
        collected = NOW - timedelta(minutes=4)
        value = normalize_sample(
            payload(node='slow-clock-node', collected_at=collected),
            received_at=NOW,
        )
        self.storage.ingest(value, NOW)

        fresh = self.storage.build_snapshot(
            120, 300, now=NOW + timedelta(seconds=10)
        )
        node = fresh['nodes'][0]
        self.assertEqual(node['state'], 'online')
        self.assertEqual(node['age_seconds'], 10)
        self.assertEqual(node['clock_offset_seconds'], 240)
        self.assertIsNone(node['clock_drift_seconds'])
        self.assertFalse(node['clock_skew_warning'])

        stale = self.storage.build_snapshot(
            120, 300, now=NOW + timedelta(seconds=121)
        )
        self.assertEqual(stale['nodes'][0]['state'], 'stale')

    def test_stable_offset_is_ignored_but_relative_clock_jump_warns(self):
        first_collected = NOW - timedelta(minutes=5)
        first = normalize_sample(
            payload(node='offset-node', collected_at=first_collected),
            received_at=NOW - timedelta(minutes=1),
        )
        second_collected = NOW - timedelta(minutes=4)
        second = normalize_sample(
            payload(node='offset-node', collected_at=second_collected),
            received_at=NOW,
        )
        self.storage.ingest(first, NOW - timedelta(minutes=1))
        self.storage.ingest(second, NOW)

        stable = self.storage.build_snapshot(120, 300, now=NOW)
        self.assertEqual(stable['nodes'][0]['clock_offset_seconds'], 240)
        self.assertEqual(stable['nodes'][0]['clock_drift_seconds'], 0)
        self.assertFalse(stable['nodes'][0]['clock_skew_warning'])
        self.assertEqual(self.storage.sync_alerts(stable, NOW), 0)

        jumped_collected = second_collected + timedelta(minutes=2)
        jumped = normalize_sample(
            payload(node='offset-node', collected_at=jumped_collected),
            received_at=NOW + timedelta(minutes=1),
        )
        self.storage.ingest(jumped, NOW + timedelta(minutes=1))
        changed = self.storage.build_snapshot(
            120, 300, now=NOW + timedelta(minutes=1)
        )
        self.assertEqual(changed['nodes'][0]['clock_drift_seconds'], -60)
        self.assertTrue(changed['nodes'][0]['clock_skew_warning'])
        self.assertEqual(
            self.storage.sync_alerts(changed, NOW + timedelta(minutes=1)), 1
        )

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

    def test_snapshot_separates_registered_fresh_and_last_known_cards(self):
        self.storage.ingest(normalized(), NOW)
        snapshot = self.storage.build_snapshot(
            120, 300, now=NOW + timedelta(seconds=121)
        )
        self.assertEqual(snapshot['snapshot_version'], 4)
        self.assertEqual(snapshot['node_counts']['stale'], 1)
        self.assertEqual(snapshot['registered_expected_cards'], 2)
        self.assertEqual(snapshot['fresh_expected_cards'], 0)
        self.assertEqual(snapshot['fresh_collected_cards'], 0)
        self.assertEqual(snapshot['last_known_collected_cards'], 2)
        self.assertEqual(snapshot['fleet_freshness_coverage_percent'], 0.0)
        self.assertIsNone(snapshot['reporting_sample_coverage_percent'])
        self.assertIsNone(snapshot['utilization_avg'])
        self.assertEqual(snapshot['nodes'][0]['last_known_utilization_avg'], 50.0)
        self.assertIsNone(snapshot['nodes'][0]['fresh_utilization_avg'])
        # Snapshot v1 aliases remain available during rolling upgrades.
        self.assertEqual(snapshot['expected_cards'], 2)
        self.assertEqual(snapshot['active_collected_cards'], 0)
        self.assertEqual(snapshot['coverage_percent'], 0.0)

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

    def test_cold_data_is_verified_in_archive_before_hot_removal(self):
        value = normalized()
        self.storage.ingest(value, NOW)
        self.storage.connection.execute('''
            INSERT INTO alerts (
                alert_id, alert_key, cluster_id, node_id, alert_type,
                severity, status, started_epoch, started_at, last_seen_epoch,
                last_seen_at, resolved_epoch, resolved_at, message, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'old-alert', 'old-key', 'default', 'node-01', 'node_stale',
            'warning', 'resolved', int(NOW.timestamp()), NOW.isoformat(),
            int(NOW.timestamp()), NOW.isoformat(), int(NOW.timestamp()),
            NOW.isoformat(), 'resolved', '{}',
        ))
        archive_path = os.path.join(self.temporary.name, 'archive', 'cold.db')

        archived = self.storage.archive_before(
            int((NOW + timedelta(seconds=1)).timestamp()), archive_path
        )
        self.assertEqual(archived, (1, 1))
        self.assertEqual(self.storage.health()['sample_count'], 0)

        archive = sqlite3.connect(archive_path)
        try:
            self.assertEqual(
                archive.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 1
            )
            self.assertEqual(
                archive.execute('SELECT COUNT(*) FROM cards').fetchone()[0], 2
            )
            self.assertEqual(
                archive.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 1
            )
        finally:
            archive.close()
        self.assertEqual(self.storage.archive_before(
            int((NOW + timedelta(seconds=1)).timestamp()), archive_path
        ), (0, 0))

    def test_archive_conflict_keeps_hot_data(self):
        self.storage.ingest(normalized(), NOW)
        archive_path = os.path.join(self.temporary.name, 'archive-conflict.sqlite3')
        archive = sqlite3.connect(archive_path)
        try:
            CollectorStorage._initialize_archive(archive)
            source = list(self.storage.connection.execute('''
                SELECT sample_id, cluster_id, node_id, node_name,
                       collected_epoch, collected_at, received_at,
                       collect_interval, expected_cards, collected_cards,
                       sample_status, coverage_percent, payload_hash,
                       normalized_json
                FROM samples
            ''').fetchone())
            source[12] = 'different-payload-hash'
            archive.execute('''
                INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', source)
            archive.commit()
        finally:
            archive.close()

        with self.assertRaisesRegex(RuntimeError, 'archive payload conflict'):
            self.storage.archive_before(
                int((NOW + timedelta(seconds=1)).timestamp()), archive_path
            )
        self.assertEqual(self.storage.health()['sample_count'], 1)


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
        self.assertEqual(client.clusters()['items'][0]['cluster_id'], 'default')
        self.assertEqual(client.nodes(search='node-01')['total'], 1)
        start = (NOW - timedelta(minutes=1)).isoformat(timespec='seconds')
        end = (NOW + timedelta(minutes=1)).isoformat(timespec='seconds')
        self.assertEqual(client.series(
            start, end, 60, card_id=1
        )['points'][0]['utilization_avg'], 80.0)
        self.assertEqual(client.samples(start, end)['total'], 1)
        self.assertEqual(client.alerts(start, end)['total'], 0)

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
        with open(os.path.join(self.temporary.name, 'upload_health.json'),
                  encoding='utf-8') as handle:
            health = json.load(handle)
        self.assertIsNotNone(health['last_success'])
        self.assertEqual(health['total_successes'], 1)
        self.assertEqual(health['consecutive_failures'], 0)
        self.assertIsNone(health['upload_unavailable_since'])
        self.assertIsNone(health['oldest_pending_age_seconds'])

    def test_upload_worker_restores_health_state_across_restart(self):
        spool = os.path.join(self.temporary.name, 'persistent-spool')
        rejected = os.path.join(self.temporary.name, 'persistent-rejected')
        health_path = os.path.join(self.temporary.name, 'persistent-upload-health.json')
        os.makedirs(spool)
        enqueue(normalized(), spool, 10, 1024 * 1024)
        persisted = {
            'last_success': '2026-08-14T01:00:00+00:00',
            'last_failure_at': '2026-08-14T02:00:00+00:00',
            'upload_unavailable_since': '2026-08-14T02:00:00+00:00',
            'last_error': 'upload failed: connection refused',
            'consecutive_failures': 7,
            'total_successes': 11,
            'total_failures': 13,
        }
        with open(health_path, 'w', encoding='utf-8') as handle:
            json.dump(persisted, handle)

        worker = UploadWorker(
            self.base_url, spool, rejected, health_path, timeout=2, batch_size=10
        )
        self.assertEqual(worker.last_success, persisted['last_success'])
        self.assertEqual(worker.last_failure_at, persisted['last_failure_at'])
        self.assertEqual(
            worker.upload_unavailable_since, persisted['upload_unavailable_since']
        )
        self.assertEqual(worker.consecutive_failures, 7)
        self.assertEqual(worker.total_successes, 11)
        self.assertEqual(worker.total_failures, 13)
        worker._write_health()

        with open(health_path, encoding='utf-8') as handle:
            restored = json.load(handle)
        self.assertEqual(restored['consecutive_failures'], 7)
        self.assertEqual(restored['last_success'], persisted['last_success'])
        self.assertEqual(restored['upload_unavailable_since'],
                         persisted['upload_unavailable_since'])
        self.assertEqual(restored['pending_samples'], 1)
        self.assertIsNotNone(restored['oldest_pending_at'])
        self.assertGreaterEqual(restored['oldest_pending_age_seconds'], 0)

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
                html = response.read().decode('utf-8')
                self.assertIn('NPU 集群监控', html)
                self.assertIn('登记卡数', html)
                self.assertIn('原始采样记录', html)
                self.assertIn('本月汇聚报表', html)
            with urlopen(console_url + '/api/snapshot', timeout=2) as response:
                self.assertEqual(json.loads(response.read())['total_nodes'], 1)
            query = urlencode({
                'start': (NOW - timedelta(minutes=1)).isoformat(timespec='seconds'),
                'end': (NOW + timedelta(minutes=1)).isoformat(timespec='seconds'),
                'format': 'xlsx',
            })
            with urlopen(console_url + '/api/export?' + query, timeout=2) as response:
                self.assertEqual(
                    response.headers.get_content_type(),
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                )
                self.assertTrue(response.read().startswith(b'PK'))
            report_query = urlencode({
                'period': 'custom',
                'start': (NOW - timedelta(minutes=1)).isoformat(timespec='seconds'),
                'end': (NOW + timedelta(minutes=1)).isoformat(timespec='seconds'),
            })
            with urlopen(
                    console_url + '/api/utilization-report?' + report_query,
                    timeout=5) as response:
                self.assertEqual(
                    response.headers.get_content_type(),
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                )
                self.assertTrue(response.read().startswith(b'PK'))
        finally:
            console_server.shutdown()
            console_server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
