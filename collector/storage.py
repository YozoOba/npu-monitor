from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading

from cluster_common.protocol import canonical_payload_hash, parse_timestamp


class ConflictingSampleError(RuntimeError):
    pass


def _utc_text(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds')


class CollectorStorage:
    SCHEMA_VERSION = 2

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(
            path, timeout=30, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self._initialize()
        with self.lock:
            self.integrity_status = self.connection.execute(
                'PRAGMA quick_check(1)'
            ).fetchone()[0]

    def _column_names(self, table):
        return {
            row['name'] for row in self.connection.execute(
                'PRAGMA table_info({})'.format(table)
            ).fetchall()
        }

    def _initialize(self):
        with self.lock:
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.execute('PRAGMA foreign_keys=ON')
            database_version = self.connection.execute(
                'PRAGMA user_version'
            ).fetchone()[0]
            if database_version not in (0, 1, self.SCHEMA_VERSION):
                raise RuntimeError(
                    'unsupported database schema version {} (application expects {})'.format(
                        database_version, self.SCHEMA_VERSION
                    )
                )
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS samples (
                    sample_id TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL DEFAULT 'default',
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    collected_epoch INTEGER NOT NULL,
                    collected_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    collect_interval INTEGER NOT NULL,
                    expected_cards INTEGER NOT NULL,
                    collected_cards INTEGER NOT NULL,
                    sample_status TEXT NOT NULL,
                    coverage_percent REAL NOT NULL,
                    payload_hash TEXT NOT NULL,
                    normalized_json TEXT NOT NULL,
                    UNIQUE(node_id, collected_at)
                );
                CREATE TABLE IF NOT EXISTS cards (
                    sample_id TEXT NOT NULL REFERENCES samples(sample_id) ON DELETE CASCADE,
                    cluster_id TEXT NOT NULL DEFAULT 'default',
                    node_id TEXT NOT NULL,
                    collected_epoch INTEGER NOT NULL,
                    card_id INTEGER NOT NULL,
                    utilization REAL NOT NULL,
                    hbm_used_mb INTEGER,
                    hbm_total_mb INTEGER,
                    PRIMARY KEY(sample_id, card_id)
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL DEFAULT 'default',
                    node_name TEXT NOT NULL,
                    latest_sample_id TEXT REFERENCES samples(sample_id) ON DELETE SET NULL,
                    latest_collected_epoch INTEGER NOT NULL,
                    latest_received_at TEXT NOT NULL,
                    collect_interval INTEGER NOT NULL,
                    expected_cards INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id TEXT PRIMARY KEY,
                    alert_key TEXT NOT NULL,
                    cluster_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_epoch INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    last_seen_epoch INTEGER NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_epoch INTEGER,
                    resolved_at TEXT,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
            ''')
            if database_version == 1:
                for table in ('samples', 'cards', 'nodes'):
                    if 'cluster_id' not in self._column_names(table):
                        self.connection.execute(
                            "ALTER TABLE {} ADD COLUMN cluster_id TEXT NOT NULL "
                            "DEFAULT 'default'".format(table)
                        )
            self.connection.executescript('''
                CREATE INDEX IF NOT EXISTS samples_node_time
                    ON samples(node_id, collected_epoch);
                CREATE INDEX IF NOT EXISTS samples_time
                    ON samples(collected_epoch);
                CREATE INDEX IF NOT EXISTS samples_cluster_time
                    ON samples(cluster_id, collected_epoch);
                CREATE INDEX IF NOT EXISTS cards_node_time
                    ON cards(node_id, collected_epoch);
                CREATE INDEX IF NOT EXISTS cards_time
                    ON cards(collected_epoch);
                CREATE INDEX IF NOT EXISTS cards_cluster_time
                    ON cards(cluster_id, collected_epoch);
                CREATE INDEX IF NOT EXISTS alerts_time
                    ON alerts(started_epoch);
                CREATE INDEX IF NOT EXISTS alerts_filter
                    ON alerts(cluster_id, node_id, status, severity);
                CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_active_key
                    ON alerts(alert_key) WHERE status = 'active';
            ''')
            if database_version != self.SCHEMA_VERSION:
                self.connection.execute(
                    'PRAGMA user_version = {}'.format(self.SCHEMA_VERSION)
                )

    def close(self):
        with self.lock:
            self.connection.close()

    def ingest(self, sample, received_at=None):
        received_at = received_at or datetime.now(timezone.utc)
        received_text = received_at.isoformat(timespec='seconds')
        collected = parse_timestamp(sample['collected_at']).astimezone(timezone.utc)
        collected_epoch = int(collected.timestamp())
        cluster_id = sample.get('cluster_id', 'default')
        payload_hash = canonical_payload_hash(sample)
        normalized_json = json.dumps(
            sample, ensure_ascii=False, sort_keys=True, separators=(',', ':')
        )
        with self.lock:
            existing = self.connection.execute(
                'SELECT payload_hash, normalized_json FROM samples WHERE sample_id = ?',
                (sample['sample_id'],),
            ).fetchone()
            if existing:
                if existing['payload_hash'] != payload_hash:
                    previous = json.loads(existing['normalized_json'])
                    previous.setdefault('cluster_id', 'default')
                    if canonical_payload_hash(previous) != payload_hash:
                        raise ConflictingSampleError(
                            'same node and timestamp already exist with different measurements'
                        )
                return False
            identity_collision = self.connection.execute('''
                SELECT sample_id FROM samples
                WHERE node_id = ? AND collected_at = ?
            ''', (sample['node_id'], sample['collected_at'])).fetchone()
            if identity_collision:
                raise ConflictingSampleError(
                    'node_id and collected_at already belong to another sample; '
                    'node_id must be globally unique across clusters'
                )
            try:
                self.connection.execute('BEGIN IMMEDIATE')
                self.connection.execute('''
                    INSERT INTO samples (
                        sample_id, cluster_id, node_id, node_name, collected_epoch,
                        collected_at, received_at, collect_interval, expected_cards,
                        collected_cards, sample_status, coverage_percent, payload_hash,
                        normalized_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    sample['sample_id'], cluster_id, sample['node_id'],
                    sample['node_name'], collected_epoch, sample['collected_at'],
                    received_text, sample['collect_interval'], sample['expected_cards'],
                    sample['collected_cards'], sample['status'],
                    sample['coverage_percent'], payload_hash, normalized_json,
                ))
                self.connection.executemany('''
                    INSERT INTO cards (
                        sample_id, cluster_id, node_id, collected_epoch, card_id,
                        utilization, hbm_used_mb, hbm_total_mb
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', [(
                    sample['sample_id'], cluster_id, sample['node_id'],
                    collected_epoch, card['card_id'], card['utilization'],
                    card['hbm_used_mb'], card['hbm_total_mb'],
                ) for card in sample['cards']])
                node = self.connection.execute(
                    'SELECT latest_collected_epoch FROM nodes WHERE node_id = ?',
                    (sample['node_id'],),
                ).fetchone()
                if node is None:
                    self.connection.execute('''
                        INSERT INTO nodes (
                            node_id, cluster_id, node_name, latest_sample_id,
                            latest_collected_epoch, latest_received_at,
                            collect_interval, expected_cards
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        sample['node_id'], cluster_id, sample['node_name'],
                        sample['sample_id'], collected_epoch, received_text,
                        sample['collect_interval'], sample['expected_cards'],
                    ))
                elif collected_epoch >= node['latest_collected_epoch']:
                    self.connection.execute('''
                        UPDATE nodes SET cluster_id = ?, node_name = ?,
                            latest_sample_id = ?, latest_collected_epoch = ?,
                            latest_received_at = ?, collect_interval = ?,
                            expected_cards = ? WHERE node_id = ?
                    ''', (
                        cluster_id, sample['node_name'], sample['sample_id'],
                        collected_epoch, received_text, sample['collect_interval'],
                        sample['expected_cards'], sample['node_id'],
                    ))
                self.connection.execute('COMMIT')
            except Exception:
                self.connection.execute('ROLLBACK')
                raise
        return True

    def latest_samples(self, cluster_id=None):
        parameters = []
        where = ''
        if cluster_id:
            where = 'WHERE n.cluster_id = ?'
            parameters.append(cluster_id)
        with self.lock:
            rows = self.connection.execute('''
                SELECT n.node_id, n.cluster_id, n.node_name,
                       n.latest_collected_epoch, n.latest_received_at,
                       n.collect_interval, n.expected_cards, s.normalized_json
                FROM nodes n
                LEFT JOIN samples s ON s.sample_id = n.latest_sample_id
                {} ORDER BY n.cluster_id, n.node_id
            '''.format(where), parameters).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value['sample'] = (
                json.loads(value.pop('normalized_json'))
                if value.get('normalized_json') else None
            )
            if value['sample'] is not None:
                value['sample'].setdefault('cluster_id', value['cluster_id'])
            values.append(value)
        return values

    @staticmethod
    def _aggregate_nodes(nodes, now, busy_utilization, idle_utilization,
                         include_nodes=True):
        total_expected = sum(node['expected_cards'] for node in nodes)
        active_nodes = [node for node in nodes if node['state'] in ('online', 'degraded')]
        reporting_expected = sum(node['expected_cards'] for node in active_nodes)
        total_collected = sum(node['collected_cards'] for node in active_nodes)
        last_known_collected = sum(node['last_known_collected_cards'] for node in nodes)
        all_cards = [
            card for node in active_nodes for card in node.get('cards', [])
        ]
        hbm_cards = [card for card in all_cards if card['hbm_total_mb']]
        hbm_used = sum(card['hbm_used_mb'] for card in hbm_cards)
        hbm_total = sum(card['hbm_total_mb'] for card in hbm_cards)
        value = {
            'generated_at': now.isoformat(timespec='seconds'),
            'node_counts': {
                state: sum(node['state'] == state for node in nodes)
                for state in ('online', 'degraded', 'stale', 'offline')
            },
            'total_nodes': len(nodes),
            'registered_expected_cards': total_expected,
            'fresh_expected_cards': reporting_expected,
            'fresh_collected_cards': total_collected,
            'last_known_collected_cards': last_known_collected,
            'stale_expected_cards': sum(
                node['expected_cards'] for node in nodes if node['state'] == 'stale'
            ),
            'offline_expected_cards': sum(
                node['expected_cards'] for node in nodes if node['state'] == 'offline'
            ),
            'fleet_freshness_coverage_percent': round(
                total_collected * 100.0 / total_expected, 2
            ) if total_expected else 0.0,
            'reporting_sample_coverage_percent': round(
                total_collected * 100.0 / reporting_expected, 2
            ) if reporting_expected else None,
            'expected_cards': total_expected,
            'active_collected_cards': total_collected,
            'coverage_percent': round(
                total_collected * 100.0 / total_expected, 2
            ) if total_expected else 0.0,
            'utilization_avg': round(
                sum(card['utilization'] for card in all_cards) / len(all_cards), 2
            ) if all_cards else None,
            'busy_cards': sum(
                card['utilization'] >= busy_utilization for card in all_cards
            ),
            'idle_cards': sum(
                card['utilization'] <= idle_utilization for card in all_cards
            ),
            'hbm_used_mb': hbm_used if hbm_total else None,
            'hbm_total_mb': hbm_total if hbm_total else None,
            'hbm_percent': round(hbm_used * 100.0 / hbm_total, 2) if hbm_total else None,
        }
        if include_nodes:
            value['nodes'] = nodes
        return value

    def build_snapshot(self, stale_floor, offline_floor, clock_skew_warn=30,
                       busy_utilization=80, idle_utilization=10, now=None,
                       cluster_id=None):
        now = now or datetime.now(timezone.utc)
        nodes = []
        for record in self.latest_samples(cluster_id):
            sample = record['sample']
            age = max(0, int(now.timestamp()) - record['latest_collected_epoch'])
            interval = record['collect_interval']
            stale_after = max(stale_floor, interval * 2)
            offline_after = max(offline_floor, interval * 5)
            state = 'online'
            if age > offline_after:
                state = 'offline'
            elif age > stale_after:
                state = 'stale'
            elif not sample or sample['status'] != 'complete':
                state = 'degraded'
            cards = sample['cards'] if sample else []
            is_fresh = state in ('online', 'degraded')
            utilization = (
                sum(card['utilization'] for card in cards) / len(cards)
                if cards else None
            )
            hbm_cards = [card for card in cards if card['hbm_total_mb']]
            hbm_used = sum(card['hbm_used_mb'] for card in hbm_cards)
            hbm_total = sum(card['hbm_total_mb'] for card in hbm_cards)
            received = parse_timestamp(
                record['latest_received_at']
            ).astimezone(timezone.utc)
            collected = (
                parse_timestamp(sample['collected_at']).astimezone(timezone.utc)
                if sample else None
            )
            clock_skew = int((received - collected).total_seconds()) if collected else None
            nodes.append({
                'cluster_id': record['cluster_id'],
                'node_id': record['node_id'],
                'node_name': record['node_name'],
                'state': state,
                'age_seconds': age,
                'last_collected_at': sample['collected_at'] if sample else None,
                'last_received_at': record['latest_received_at'],
                'clock_skew_seconds': clock_skew,
                'clock_skew_warning': (
                    abs(clock_skew) > clock_skew_warn
                    if clock_skew is not None else False
                ),
                'sample_status': sample['status'] if sample else 'missing',
                'expected_cards': record['expected_cards'],
                'collected_cards': len(cards),
                'fresh_collected_cards': len(cards) if is_fresh else 0,
                'last_known_collected_cards': len(cards),
                'coverage_percent': sample['coverage_percent'] if sample else 0.0,
                'utilization_avg': round(utilization, 2) if utilization is not None else None,
                'fresh_utilization_avg': (
                    round(utilization, 2)
                    if is_fresh and utilization is not None else None
                ),
                'last_known_utilization_avg': (
                    round(utilization, 2) if utilization is not None else None
                ),
                'hbm_used_mb': hbm_used if hbm_total else None,
                'hbm_total_mb': hbm_total if hbm_total else None,
                'hbm_percent': (
                    round(hbm_used * 100.0 / hbm_total, 2) if hbm_total else None
                ),
                'fresh_hbm_percent': (
                    round(hbm_used * 100.0 / hbm_total, 2)
                    if is_fresh and hbm_total else None
                ),
                'last_known_hbm_percent': (
                    round(hbm_used * 100.0 / hbm_total, 2) if hbm_total else None
                ),
                'cards': cards,
            })
        snapshot = self._aggregate_nodes(
            nodes, now, busy_utilization, idle_utilization
        )
        snapshot['snapshot_version'] = 3
        snapshot['cluster_id'] = cluster_id
        grouped = {}
        for node in nodes:
            grouped.setdefault(node['cluster_id'], []).append(node)
        snapshot['clusters'] = []
        for current_cluster in sorted(grouped):
            cluster_value = self._aggregate_nodes(
                grouped[current_cluster], now, busy_utilization,
                idle_utilization, include_nodes=False,
            )
            cluster_value['cluster_id'] = current_cluster
            snapshot['clusters'].append(cluster_value)
        return snapshot

    def history_series(self, start_epoch, end_epoch, bucket_seconds=300,
                       node_id=None, cluster_id=None, card_id=None):
        card_filters = []
        card_parameters = [bucket_seconds, bucket_seconds, start_epoch, end_epoch]
        if node_id:
            card_filters.append('c.node_id = ?')
            card_parameters.append(node_id)
        if cluster_id:
            card_filters.append('c.cluster_id = ?')
            card_parameters.append(cluster_id)
        if card_id is not None:
            card_filters.append('c.card_id = ?')
            card_parameters.append(card_id)
        card_where = ''.join(' AND ' + value for value in card_filters)
        with self.lock:
            rows = self.connection.execute('''
                SELECT (c.collected_epoch / ?) * ? AS bucket_epoch,
                       AVG(c.utilization) AS utilization_avg,
                       MIN(c.utilization) AS utilization_min,
                       MAX(c.utilization) AS utilization_max,
                       COUNT(*) AS card_samples,
                       COUNT(DISTINCT c.node_id) AS node_count,
                       SUM(CASE WHEN c.hbm_total_mb IS NOT NULL
                                THEN c.hbm_used_mb ELSE 0 END) AS hbm_used_sum,
                       SUM(CASE WHEN c.hbm_total_mb IS NOT NULL
                                THEN c.hbm_total_mb ELSE 0 END) AS hbm_total_sum
                FROM cards c
                WHERE c.collected_epoch >= ? AND c.collected_epoch < ? {}
                GROUP BY bucket_epoch ORDER BY bucket_epoch
            '''.format(card_where), card_parameters).fetchall()
        values_by_epoch = {}
        for row in rows:
            value = dict(row)
            epoch = value.pop('bucket_epoch')
            value['timestamp'] = _utc_text(epoch)
            value['utilization_avg'] = round(value['utilization_avg'], 2)
            value['utilization_min'] = round(value['utilization_min'], 2)
            value['utilization_max'] = round(value['utilization_max'], 2)
            total = value.pop('hbm_total_sum')
            used = value.pop('hbm_used_sum')
            value['hbm_percent'] = round(used * 100.0 / total, 2) if total else None
            values_by_epoch[epoch] = value

        sample_parameters = [bucket_seconds, bucket_seconds, start_epoch, end_epoch]
        sample_filters = []
        if node_id:
            sample_filters.append('node_id = ?')
            sample_parameters.append(node_id)
        if cluster_id:
            sample_filters.append('cluster_id = ?')
            sample_parameters.append(cluster_id)
        sample_where = ''.join(' AND ' + value for value in sample_filters)
        with self.lock:
            sample_rows = self.connection.execute('''
                SELECT (collected_epoch / ?) * ? AS bucket_epoch,
                       COUNT(*) AS attempts,
                       SUM(CASE WHEN sample_status = 'complete' THEN 1 ELSE 0 END)
                           AS complete,
                       SUM(CASE WHEN sample_status = 'partial' THEN 1 ELSE 0 END)
                           AS partial,
                       SUM(CASE WHEN sample_status = 'failed' THEN 1 ELSE 0 END)
                           AS failed,
                       SUM(expected_cards) AS expected_card_samples,
                       SUM(collected_cards) AS collected_card_samples
                FROM samples
                WHERE collected_epoch >= ? AND collected_epoch < ? {}
                GROUP BY bucket_epoch ORDER BY bucket_epoch
            '''.format(sample_where), sample_parameters).fetchall()
        for row in sample_rows:
            item = dict(row)
            epoch = item.pop('bucket_epoch')
            value = values_by_epoch.setdefault(epoch, {
                'timestamp': _utc_text(epoch),
                'utilization_avg': None,
                'utilization_min': None,
                'utilization_max': None,
                'card_samples': 0,
                'node_count': 0,
                'hbm_percent': None,
            })
            value.update(item)
            expected = value['expected_card_samples']
            value['coverage_percent'] = round(
                value['collected_card_samples'] * 100.0 / expected, 2
            ) if expected else 0.0
        return [values_by_epoch[key] for key in sorted(values_by_epoch)]

    def query_samples(self, start_epoch, end_epoch, page=1, page_size=100,
                      node_id=None, cluster_id=None, sample_status=None,
                      search=None):
        filters = ['collected_epoch >= ?', 'collected_epoch < ?']
        parameters = [start_epoch, end_epoch]
        if node_id:
            filters.append('node_id = ?')
            parameters.append(node_id)
        if cluster_id:
            filters.append('cluster_id = ?')
            parameters.append(cluster_id)
        if sample_status:
            filters.append('sample_status = ?')
            parameters.append(sample_status)
        if search:
            filters.append('(node_id LIKE ? ESCAPE \'\\\' OR node_name LIKE ? ESCAPE \'\\\')')
            escaped = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            parameters.extend(['%' + escaped + '%', '%' + escaped + '%'])
        where = ' AND '.join(filters)
        with self.lock:
            total = self.connection.execute(
                'SELECT COUNT(*) FROM samples WHERE ' + where, parameters
            ).fetchone()[0]
            rows = self.connection.execute('''
                SELECT normalized_json, received_at FROM samples
                WHERE {} ORDER BY collected_epoch DESC, node_id
                LIMIT ? OFFSET ?
            '''.format(where), parameters + [page_size, (page - 1) * page_size]).fetchall()
        items = []
        for row in rows:
            item = json.loads(row['normalized_json'])
            item.setdefault('cluster_id', 'default')
            item['received_at'] = row['received_at']
            items.append(item)
        return {
            'page': page,
            'page_size': page_size,
            'total': total,
            'pages': (total + page_size - 1) // page_size,
            'items': items,
        }

    @staticmethod
    def _alert_candidates(snapshot):
        candidates = {}
        for node in snapshot.get('nodes', []):
            prefix = '{}:{}'.format(node['cluster_id'], node['node_id'])
            if node['state'] != 'online':
                severity = 'critical' if node['state'] == 'offline' else 'warning'
                alert_type = 'node_' + node['state']
                candidates[prefix + ':' + alert_type] = {
                    'cluster_id': node['cluster_id'],
                    'node_id': node['node_id'],
                    'alert_type': alert_type,
                    'severity': severity,
                    'message': '{} is {}'.format(node['node_id'], node['state']),
                    'details': {
                        'age_seconds': node['age_seconds'],
                        'sample_status': node['sample_status'],
                    },
                }
            if node['coverage_percent'] < 100.0:
                candidates[prefix + ':card_coverage'] = {
                    'cluster_id': node['cluster_id'],
                    'node_id': node['node_id'],
                    'alert_type': 'card_coverage',
                    'severity': 'warning',
                    'message': '{} card coverage is {:.2f}%'.format(
                        node['node_id'], node['coverage_percent']
                    ),
                    'details': {
                        'expected_cards': node['expected_cards'],
                        'collected_cards': node['collected_cards'],
                        'coverage_percent': node['coverage_percent'],
                    },
                }
            if node['clock_skew_warning']:
                candidates[prefix + ':clock_skew'] = {
                    'cluster_id': node['cluster_id'],
                    'node_id': node['node_id'],
                    'alert_type': 'clock_skew',
                    'severity': 'warning',
                    'message': '{} clock skew is {} seconds'.format(
                        node['node_id'], node['clock_skew_seconds']
                    ),
                    'details': {'clock_skew_seconds': node['clock_skew_seconds']},
                }
        return candidates

    def sync_alerts(self, snapshot, now=None):
        now = now or datetime.now(timezone.utc)
        now_epoch = int(now.timestamp())
        now_text = now.isoformat(timespec='seconds')
        candidates = self._alert_candidates(snapshot)
        with self.lock:
            active = {
                row['alert_key']: dict(row) for row in self.connection.execute(
                    "SELECT alert_id, alert_key FROM alerts WHERE status = 'active'"
                ).fetchall()
            }
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                for alert_key, value in candidates.items():
                    details = json.dumps(
                        value['details'], ensure_ascii=False, sort_keys=True,
                        separators=(',', ':'),
                    )
                    if alert_key in active:
                        self.connection.execute('''
                            UPDATE alerts SET last_seen_epoch = ?, last_seen_at = ?,
                                severity = ?, message = ?, details_json = ?
                            WHERE alert_id = ?
                        ''', (
                            now_epoch, now_text, value['severity'], value['message'],
                            details, active[alert_key]['alert_id'],
                        ))
                    else:
                        alert_id = hashlib.sha256(
                            '{}\n{}'.format(alert_key, now_text).encode('utf-8')
                        ).hexdigest()
                        self.connection.execute('''
                            INSERT INTO alerts (
                                alert_id, alert_key, cluster_id, node_id,
                                alert_type, severity, status, started_epoch,
                                started_at, last_seen_epoch, last_seen_at,
                                resolved_epoch, resolved_at, message, details_json
                            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?,
                                      NULL, NULL, ?, ?)
                        ''', (
                            alert_id, alert_key, value['cluster_id'], value['node_id'],
                            value['alert_type'], value['severity'], now_epoch, now_text,
                            now_epoch, now_text, value['message'], details,
                        ))
                for alert_key, row in active.items():
                    if alert_key not in candidates:
                        self.connection.execute('''
                            UPDATE alerts SET status = 'resolved',
                                resolved_epoch = ?, resolved_at = ?
                            WHERE alert_id = ?
                        ''', (now_epoch, now_text, row['alert_id']))
                self.connection.execute('COMMIT')
            except Exception:
                self.connection.execute('ROLLBACK')
                raise
        return len(candidates)

    def query_alerts(self, start_epoch, end_epoch, page=1, page_size=100,
                     cluster_id=None, node_id=None, severity=None, status=None,
                     alert_type=None):
        filters = ['last_seen_epoch >= ?', 'started_epoch < ?']
        parameters = [start_epoch, end_epoch]
        for column, value in (
                ('cluster_id', cluster_id), ('node_id', node_id),
                ('severity', severity), ('status', status),
                ('alert_type', alert_type)):
            if value:
                filters.append('{} = ?'.format(column))
                parameters.append(value)
        where = ' AND '.join(filters)
        with self.lock:
            total = self.connection.execute(
                'SELECT COUNT(*) FROM alerts WHERE ' + where, parameters
            ).fetchone()[0]
            rows = self.connection.execute('''
                SELECT alert_id, cluster_id, node_id, alert_type, severity,
                       status, started_at, last_seen_at, resolved_at, message,
                       details_json FROM alerts WHERE {}
                ORDER BY started_epoch DESC, node_id LIMIT ? OFFSET ?
            '''.format(where), parameters + [page_size, (page - 1) * page_size]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['details'] = json.loads(item.pop('details_json'))
            items.append(item)
        return {
            'page': page,
            'page_size': page_size,
            'total': total,
            'pages': (total + page_size - 1) // page_size,
            'items': items,
        }

    def active_alert_count(self, cluster_id=None):
        query = "SELECT COUNT(*) FROM alerts WHERE status = 'active'"
        parameters = []
        if cluster_id:
            query += ' AND cluster_id = ?'
            parameters.append(cluster_id)
        with self.lock:
            return self.connection.execute(query, parameters).fetchone()[0]

    def delete_before(self, cutoff_epoch):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                cursor = self.connection.execute(
                    'DELETE FROM samples WHERE collected_epoch < ?', (cutoff_epoch,)
                )
                deleted = cursor.rowcount
                self.connection.execute('''
                    DELETE FROM alerts
                    WHERE status = 'resolved' AND last_seen_epoch < ?
                ''', (cutoff_epoch,))
                self.connection.execute('COMMIT')
            except Exception:
                self.connection.execute('ROLLBACK')
                raise
        return deleted

    def health(self, check_integrity=False):
        with self.lock:
            row = self.connection.execute('''
                SELECT COUNT(*) AS sample_count,
                       (SELECT COUNT(*) FROM nodes) AS node_count,
                       (SELECT COUNT(DISTINCT cluster_id) FROM nodes) AS cluster_count,
                       (SELECT COUNT(*) FROM alerts WHERE status = 'active')
                           AS active_alert_count,
                       MAX(received_at) AS last_received_at
                FROM samples
            ''').fetchone()
            if check_integrity:
                self.integrity_status = self.connection.execute(
                    'PRAGMA quick_check(1)'
                ).fetchone()[0]
        result = dict(row)
        result['database_integrity'] = self.integrity_status
        result['schema_version'] = self.SCHEMA_VERSION
        return result
