#!/usr/bin/env python3
from datetime import datetime, timedelta, timezone
import csv
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile

from cluster_common import PROTOCOL_VERSION
from cluster_common.protocol import (
    ProtocolError, canonical_payload_hash, normalize_sample,
)
from collector.storage import CollectorStorage, ConflictingSampleError
from console.export import create_export


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def sample(node, cluster, when=NOW, values=(10, 90), expected=2):
    cards = [
        {
            'card_id': index, 'utilization': value,
            'hbm_used_mb': 100 * (index + 1), 'hbm_total_mb': 1000,
        }
        for index, value in enumerate(values)
    ]
    return normalize_sample({
        'protocol_version': PROTOCOL_VERSION,
        'cluster_id': cluster,
        'node_id': node,
        'node_name': node + '-name',
        'collected_at': when.isoformat(timespec='seconds'),
        'collect_interval': 60,
        'expected_cards': expected,
        'cards': cards,
    }, received_at=when + timedelta(seconds=1))


class ClusterProtocolTests(unittest.TestCase):
    def test_cluster_defaults_and_custom_cluster_are_normalized(self):
        payload = {
            'protocol_version': PROTOCOL_VERSION,
            'node_id': 'node-1', 'node_name': 'node-1',
            'collected_at': NOW.isoformat(timespec='seconds'),
            'collect_interval': 60, 'expected_cards': 1,
            'cards': [{'card_id': 0, 'utilization': 1,
                       'hbm_used_mb': None, 'hbm_total_mb': None}],
        }
        default = normalize_sample(payload, received_at=NOW)
        custom = normalize_sample(dict(payload, cluster_id='training-a'), received_at=NOW)
        self.assertEqual(default['cluster_id'], 'default')
        self.assertEqual(custom['cluster_id'], 'training-a')
        self.assertNotEqual(default['sample_id'], custom['sample_id'])
        with self.assertRaises(ProtocolError):
            normalize_sample(dict(payload, cluster_id='bad cluster'), received_at=NOW)


class QueryStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temporary.name, 'cluster.sqlite3')
        self.storage = CollectorStorage(self.path)

    def tearDown(self):
        self.storage.close()
        self.temporary.cleanup()

    def test_multi_cluster_snapshot_and_card_history_filters(self):
        self.storage.ingest(sample('node-a', 'cluster-a'), NOW)
        self.storage.ingest(sample('node-b', 'cluster-b', values=(40, 60)), NOW)
        snapshot = self.storage.build_snapshot(120, 300, now=NOW + timedelta(seconds=2))
        self.assertEqual([item['cluster_id'] for item in snapshot['clusters']],
                         ['cluster-a', 'cluster-b'])
        filtered = self.storage.build_snapshot(
            120, 300, now=NOW + timedelta(seconds=2), cluster_id='cluster-a'
        )
        self.assertEqual(filtered['total_nodes'], 1)
        self.assertEqual(filtered['nodes'][0]['node_id'], 'node-a')
        history = self.storage.history_series(
            int((NOW - timedelta(minutes=1)).timestamp()),
            int((NOW + timedelta(minutes=1)).timestamp()), 60,
            cluster_id='cluster-a', card_id=1,
        )
        self.assertEqual(history[0]['utilization_avg'], 90.0)
        self.assertEqual(history[0]['card_samples'], 1)

    def test_node_id_must_be_globally_unique_across_clusters(self):
        self.storage.ingest(sample('shared-node', 'cluster-a'), NOW)
        with self.assertRaises(ConflictingSampleError):
            self.storage.ingest(sample('shared-node', 'cluster-b'), NOW)

    def test_raw_samples_are_filterable_and_paginated(self):
        self.storage.ingest(sample('alpha-1', 'cluster-a'), NOW)
        self.storage.ingest(sample(
            'beta-1', 'cluster-b', NOW + timedelta(minutes=1)
        ), NOW + timedelta(minutes=1))
        result = self.storage.query_samples(
            int((NOW - timedelta(minutes=1)).timestamp()),
            int((NOW + timedelta(minutes=2)).timestamp()),
            page=1, page_size=1, search='alpha',
        )
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['pages'], 1)
        self.assertEqual(result['items'][0]['cluster_id'], 'cluster-a')

    def test_alert_lifecycle_is_persisted(self):
        partial = sample('node-a', 'cluster-a', values=(50,), expected=2)
        self.storage.ingest(partial, NOW)
        first = self.storage.build_snapshot(120, 300, now=NOW + timedelta(seconds=2))
        self.assertEqual(self.storage.sync_alerts(first, NOW + timedelta(seconds=2)), 2)
        active = self.storage.query_alerts(
            int((NOW - timedelta(minutes=1)).timestamp()),
            int((NOW + timedelta(minutes=1)).timestamp()), status='active'
        )
        self.assertEqual({item['alert_type'] for item in active['items']},
                         {'node_degraded', 'card_coverage'})

        recovered = sample(
            'node-a', 'cluster-a', NOW + timedelta(minutes=1), values=(20, 30)
        )
        self.storage.ingest(recovered, NOW + timedelta(minutes=1))
        second = self.storage.build_snapshot(
            120, 300, now=NOW + timedelta(minutes=1, seconds=2)
        )
        self.storage.sync_alerts(second, NOW + timedelta(minutes=1, seconds=2))
        resolved = self.storage.query_alerts(
            int((NOW - timedelta(minutes=1)).timestamp()),
            int((NOW + timedelta(minutes=2)).timestamp()), status='resolved'
        )
        self.assertEqual(resolved['total'], 2)
        self.assertTrue(all(item['resolved_at'] for item in resolved['items']))

    def test_schema_one_database_is_migrated_without_data_loss(self):
        self.storage.close()
        os.remove(self.path)
        connection = sqlite3.connect(self.path)
        connection.executescript('''
            CREATE TABLE samples (
                sample_id TEXT PRIMARY KEY, node_id TEXT NOT NULL,
                node_name TEXT NOT NULL, collected_epoch INTEGER NOT NULL,
                collected_at TEXT NOT NULL, received_at TEXT NOT NULL,
                collect_interval INTEGER NOT NULL, expected_cards INTEGER NOT NULL,
                collected_cards INTEGER NOT NULL, sample_status TEXT NOT NULL,
                coverage_percent REAL NOT NULL, payload_hash TEXT NOT NULL,
                normalized_json TEXT NOT NULL, UNIQUE(node_id, collected_at));
            CREATE TABLE cards (
                sample_id TEXT NOT NULL REFERENCES samples(sample_id) ON DELETE CASCADE,
                node_id TEXT NOT NULL, collected_epoch INTEGER NOT NULL,
                card_id INTEGER NOT NULL, utilization REAL NOT NULL,
                hbm_used_mb INTEGER, hbm_total_mb INTEGER,
                PRIMARY KEY(sample_id, card_id));
            CREATE TABLE nodes (
                node_id TEXT PRIMARY KEY, node_name TEXT NOT NULL,
                latest_sample_id TEXT REFERENCES samples(sample_id) ON DELETE SET NULL,
                latest_collected_epoch INTEGER NOT NULL,
                latest_received_at TEXT NOT NULL, collect_interval INTEGER NOT NULL,
                expected_cards INTEGER NOT NULL);
            PRAGMA user_version = 1;
        ''')
        current = sample('legacy-node', 'default')
        legacy = dict(current)
        legacy.pop('cluster_id')
        legacy_json = json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(',', ':')
        )
        connection.execute('''
            INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            legacy['sample_id'], legacy['node_id'], legacy['node_name'],
            int(NOW.timestamp()), legacy['collected_at'],
            (NOW + timedelta(seconds=1)).isoformat(timespec='seconds'),
            legacy['collect_interval'], legacy['expected_cards'],
            legacy['collected_cards'], legacy['status'], legacy['coverage_percent'],
            canonical_payload_hash(legacy), legacy_json,
        ))
        connection.executemany('''
            INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', [(
            legacy['sample_id'], legacy['node_id'], int(NOW.timestamp()),
            card['card_id'], card['utilization'], card['hbm_used_mb'],
            card['hbm_total_mb'],
        ) for card in legacy['cards']])
        connection.execute('''
            INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            legacy['node_id'], legacy['node_name'], legacy['sample_id'],
            int(NOW.timestamp()),
            (NOW + timedelta(seconds=1)).isoformat(timespec='seconds'),
            legacy['collect_interval'], legacy['expected_cards'],
        ))
        connection.commit()
        connection.close()
        self.storage = CollectorStorage(self.path)
        self.assertEqual(self.storage.health()['schema_version'], 3)
        self.assertEqual(self.storage.health()['sample_count'], 1)
        self.assertFalse(self.storage.ingest(current, NOW + timedelta(seconds=2)))
        columns = self.storage.connection.execute(
            'PRAGMA table_info(samples)'
        ).fetchall()
        self.assertIn('cluster_id', {row['name'] for row in columns})


class FakeClient:
    def __init__(self, items):
        self.items = items

    def samples(self, _start, _end, page, page_size, node_id=None,
                cluster_id=None, status=None, search=None):
        values = [item for item in self.items if (
            (not node_id or item['node_id'] == node_id) and
            (not cluster_id or item['cluster_id'] == cluster_id) and
            (not status or item['status'] == status)
        )]
        offset = (page - 1) * page_size
        return {
            'page': page, 'page_size': page_size, 'total': len(values),
            'pages': (len(values) + page_size - 1) // page_size,
            'items': values[offset:offset + page_size],
        }


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        value = sample('node-a', 'cluster-a')
        value['received_at'] = (NOW + timedelta(seconds=1)).isoformat(timespec='seconds')
        self.client = FakeClient([value])

    def tearDown(self):
        self.temporary.cleanup()

    def test_csv_and_xlsx_exports_contain_typed_card_rows(self):
        csv_path = os.path.join(self.temporary.name, 'report.csv')
        xlsx_path = os.path.join(self.temporary.name, 'report.xlsx')
        self.assertEqual(create_export(
            self.client, csv_path, 'csv', 'start', 'end'
        ), 2)
        with open(csv_path, encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[1]['card_id'], '1')
        self.assertEqual(rows[1]['utilization'], '90.0')

        self.assertEqual(create_export(
            self.client, xlsx_path, 'xlsx', 'start', 'end', card_id=1
        ), 1)
        with zipfile.ZipFile(xlsx_path) as archive:
            self.assertEqual(archive.testzip(), None)
            sheet = archive.read('xl/worksheets/sheet1.xml').decode('utf-8')
        self.assertIn('NPU Samples', zipfile.ZipFile(xlsx_path).read(
            'xl/workbook.xml').decode('utf-8'))
        self.assertIn('<v>90.0</v>', sheet)


if __name__ == '__main__':
    unittest.main(verbosity=2)
