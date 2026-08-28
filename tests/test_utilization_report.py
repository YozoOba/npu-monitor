#!/usr/bin/env python3

from datetime import datetime, timezone
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from cluster_common import PROTOCOL_VERSION
from cluster_common.protocol import normalize_sample
from collector.storage import CollectorStorage
from console.utilization_report import (
    build_utilization_report, report_dates, resolve_report_range,
)


MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def sample(
        when, utilizations, node_id='node-a', node_name='910C-node-a',
        cluster_id='training-a'):
    return normalize_sample({
        'protocol_version': PROTOCOL_VERSION,
        'cluster_id': cluster_id,
        'node_id': node_id,
        'node_name': node_name,
        'collected_at': when,
        'collect_interval': 60,
        'expected_cards': len(utilizations),
        'cards': [
            {
                'card_id': index,
                'utilization': value,
                'hbm_used_mb': 100,
                'hbm_total_mb': 1000,
            }
            for index, value in enumerate(utilizations)
        ],
    })


class UtilizationReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temporary.name, 'cluster.sqlite3')
        self.storage = CollectorStorage(self.database)

    def tearDown(self):
        self.storage.close()
        self.temporary.cleanup()

    def test_report_ranges_use_china_standard_time(self):
        now = datetime(2026, 8, 19, 12, 34, 56, tzinfo=timezone.utc)
        month = resolve_report_range('month', now=now)
        self.assertEqual(month['utc_start'], '2026-07-31T16:00:00+00:00')
        self.assertEqual(month['utc_end'], '2026-08-19T12:34:56+00:00')
        day = resolve_report_range('day', now=now)
        self.assertEqual(day['utc_start'], '2026-08-18T16:00:00+00:00')
        custom = resolve_report_range(
            'custom', '2026-08-01', '2026-08-02', now=now
        )
        self.assertEqual(custom['utc_start'], '2026-07-31T16:00:00+00:00')
        self.assertEqual(custom['utc_end'], '2026-08-02T16:00:00+00:00')
        self.assertEqual(
            [value.isoformat() for value in report_dates(custom)],
            ['2026-08-01', '2026-08-02'],
        )

    def test_collector_aggregates_all_card_points_by_china_day_and_hour(self):
        self.storage.ingest(sample(
            '2026-08-01T15:30:00+00:00', [10, 30]
        ))
        self.storage.ingest(sample(
            '2026-08-01T16:30:00+00:00', [50, 70]
        ))
        nodes = self.storage.utilization_report(
            int(datetime(2026, 8, 1, 15, tzinfo=timezone.utc).timestamp()),
            int(datetime(2026, 8, 1, 17, tzinfo=timezone.utc).timestamp()),
        )
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node['utilization_avg'], 40.0)
        self.assertEqual(node['card_samples'], 4)
        self.assertEqual(
            node['days']['2026-08-01']['utilization_avg'], 20.0
        )
        self.assertEqual(
            node['days']['2026-08-01']['hours']['23']['utilization_avg'], 20.0
        )
        self.assertEqual(
            node['days']['2026-08-02']['hours']['00']['utilization_avg'], 60.0
        )

    def test_node_cluster_change_does_not_split_one_node_average(self):
        self.storage.ingest(sample(
            '2026-08-01T15:30:00+00:00', [10], cluster_id='old-cluster'
        ))
        self.storage.ingest(sample(
            '2026-08-01T16:30:00+00:00', [90], cluster_id='new-cluster'
        ))
        nodes = self.storage.utilization_report(
            int(datetime(2026, 8, 1, 15, tzinfo=timezone.utc).timestamp()),
            int(datetime(2026, 8, 1, 17, tzinfo=timezone.utc).timestamp()),
            cluster_id='new-cluster',
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['cluster_id'], 'new-cluster')
        self.assertEqual(nodes[0]['utilization_avg'], 50.0)
        self.assertEqual(nodes[0]['card_samples'], 2)

    def test_builds_summary_and_one_heatmap_sheet_per_day(self):
        self.storage.ingest(sample(
            '2026-08-01T15:30:00+00:00', [10, 30]
        ))
        self.storage.ingest(sample(
            '2026-08-01T16:30:00+00:00', [50, 70]
        ))
        start = datetime(2026, 7, 31, 16, tzinfo=timezone.utc)
        end = datetime(2026, 8, 2, 16, tzinfo=timezone.utc)
        aggregate = {
            'nodes': self.storage.utilization_report(
                int(start.timestamp()), int(end.timestamp())
            )
        }
        report_range = resolve_report_range(
            'custom', '2026-08-01', '2026-08-02'
        )
        path = os.path.join(self.temporary.name, 'report.xlsx')
        result = build_utilization_report(
            path, report_range, aggregate,
            now=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(result['nodes'], 1)
        self.assertEqual(result['days'], 2)
        self.assertEqual(result['card_samples'], 4)
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            names = [
                value.attrib['name'] for value in workbook.findall(
                    './/{{{}}}sheet'.format(MAIN_NS)
                )
            ]
            summary = archive.read('xl/worksheets/sheet1.xml').decode('utf-8')
            first_day = archive.read('xl/worksheets/sheet2.xml').decode('utf-8')
            second_day = archive.read('xl/worksheets/sheet3.xml').decode('utf-8')
            styles = archive.read('xl/styles.xml').decode('utf-8')
        self.assertEqual(names, ['汇总', '2026-08-01', '2026-08-02'])
        self.assertIn('910C-node-a', summary)
        self.assertIn('<v>40.0</v>', summary)
        self.assertIn('23:00', first_day)
        self.assertIn('<v>20.0</v>', first_day)
        self.assertIn('00:00', second_day)
        self.assertIn('<v>60.0</v>', second_day)
        self.assertIn('FFEAF4FB', styles)
        self.assertIn('FF033E57', styles)


if __name__ == '__main__':
    unittest.main(verbosity=2)
