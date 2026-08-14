from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading

from cluster_common.protocol import canonical_payload_hash, parse_timestamp


class ConflictingSampleError(RuntimeError):
    pass


class CollectorStorage:
    SCHEMA_VERSION = 1

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

    def _initialize(self):
        with self.lock:
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS samples (
                    sample_id TEXT PRIMARY KEY,
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
                CREATE INDEX IF NOT EXISTS samples_node_time
                    ON samples(node_id, collected_epoch);
                CREATE INDEX IF NOT EXISTS samples_time
                    ON samples(collected_epoch);

                CREATE TABLE IF NOT EXISTS cards (
                    sample_id TEXT NOT NULL REFERENCES samples(sample_id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL,
                    collected_epoch INTEGER NOT NULL,
                    card_id INTEGER NOT NULL,
                    utilization REAL NOT NULL,
                    hbm_used_mb INTEGER,
                    hbm_total_mb INTEGER,
                    PRIMARY KEY(sample_id, card_id)
                );
                CREATE INDEX IF NOT EXISTS cards_node_time
                    ON cards(node_id, collected_epoch);
                CREATE INDEX IF NOT EXISTS cards_time
                    ON cards(collected_epoch);

                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    node_name TEXT NOT NULL,
                    latest_sample_id TEXT REFERENCES samples(sample_id) ON DELETE SET NULL,
                    latest_collected_epoch INTEGER NOT NULL,
                    latest_received_at TEXT NOT NULL,
                    collect_interval INTEGER NOT NULL,
                    expected_cards INTEGER NOT NULL
                );
            ''')
            database_version = self.connection.execute(
                'PRAGMA user_version'
            ).fetchone()[0]
            if database_version == 0:
                self.connection.execute(
                    'PRAGMA user_version = {}'.format(self.SCHEMA_VERSION)
                )
            elif database_version != self.SCHEMA_VERSION:
                raise RuntimeError(
                    'unsupported database schema version {} (application expects {})'.format(
                        database_version, self.SCHEMA_VERSION
                    )
                )

    def close(self):
        with self.lock:
            self.connection.close()

    def ingest(self, sample, received_at=None):
        received_at = received_at or datetime.now(timezone.utc)
        received_text = received_at.isoformat(timespec='seconds')
        collected = parse_timestamp(sample['collected_at']).astimezone(timezone.utc)
        collected_epoch = int(collected.timestamp())
        payload_hash = canonical_payload_hash(sample)
        normalized_json = json.dumps(
            sample, ensure_ascii=False, sort_keys=True, separators=(',', ':')
        )
        with self.lock:
            existing = self.connection.execute(
                'SELECT payload_hash FROM samples WHERE sample_id = ?',
                (sample['sample_id'],),
            ).fetchone()
            if existing:
                if existing['payload_hash'] != payload_hash:
                    raise ConflictingSampleError(
                        'same node and timestamp already exist with different measurements'
                    )
                return False
            try:
                self.connection.execute('BEGIN IMMEDIATE')
                self.connection.execute('''
                    INSERT INTO samples (
                        sample_id, node_id, node_name, collected_epoch, collected_at,
                        received_at, collect_interval, expected_cards, collected_cards,
                        sample_status, coverage_percent, payload_hash, normalized_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    sample['sample_id'], sample['node_id'], sample['node_name'],
                    collected_epoch, sample['collected_at'], received_text,
                    sample['collect_interval'], sample['expected_cards'],
                    sample['collected_cards'], sample['status'],
                    sample['coverage_percent'], payload_hash, normalized_json,
                ))
                self.connection.executemany('''
                    INSERT INTO cards (
                        sample_id, node_id, collected_epoch, card_id, utilization,
                        hbm_used_mb, hbm_total_mb
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', [(
                    sample['sample_id'], sample['node_id'], collected_epoch,
                    card['card_id'], card['utilization'], card['hbm_used_mb'],
                    card['hbm_total_mb'],
                ) for card in sample['cards']])
                node = self.connection.execute(
                    'SELECT latest_collected_epoch FROM nodes WHERE node_id = ?',
                    (sample['node_id'],),
                ).fetchone()
                if node is None:
                    self.connection.execute('''
                        INSERT INTO nodes (
                            node_id, node_name, latest_sample_id,
                            latest_collected_epoch, latest_received_at,
                            collect_interval, expected_cards
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        sample['node_id'], sample['node_name'], sample['sample_id'],
                        collected_epoch, received_text, sample['collect_interval'],
                        sample['expected_cards'],
                    ))
                elif collected_epoch >= node['latest_collected_epoch']:
                    self.connection.execute('''
                        UPDATE nodes SET node_name = ?, latest_sample_id = ?,
                            latest_collected_epoch = ?, latest_received_at = ?,
                            collect_interval = ?, expected_cards = ?
                        WHERE node_id = ?
                    ''', (
                        sample['node_name'], sample['sample_id'], collected_epoch,
                        received_text, sample['collect_interval'],
                        sample['expected_cards'], sample['node_id'],
                    ))
                self.connection.execute('COMMIT')
            except Exception:
                self.connection.execute('ROLLBACK')
                raise
        return True

    def latest_samples(self):
        with self.lock:
            rows = self.connection.execute('''
                SELECT n.node_id, n.node_name, n.latest_collected_epoch,
                       n.latest_received_at, n.collect_interval, n.expected_cards,
                       s.normalized_json
                FROM nodes n
                LEFT JOIN samples s ON s.sample_id = n.latest_sample_id
                ORDER BY n.node_id
            ''').fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value['sample'] = (
                json.loads(value.pop('normalized_json'))
                if value.get('normalized_json') else None
            )
            values.append(value)
        return values

    def build_snapshot(self, stale_floor, offline_floor, clock_skew_warn=30,
                       busy_utilization=80, idle_utilization=10, now=None):
        now = now or datetime.now(timezone.utc)
        nodes = []
        all_cards = []
        for record in self.latest_samples():
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
            if state in ('online', 'degraded'):
                all_cards.extend(cards)
            utilization = (
                sum(card['utilization'] for card in cards) / len(cards) if cards else None
            )
            hbm_cards = [card for card in cards if card['hbm_total_mb']]
            hbm_used = sum(card['hbm_used_mb'] for card in hbm_cards)
            hbm_total = sum(card['hbm_total_mb'] for card in hbm_cards)
            received = parse_timestamp(record['latest_received_at']).astimezone(timezone.utc)
            collected = (
                parse_timestamp(sample['collected_at']).astimezone(timezone.utc)
                if sample else None
            )
            clock_skew = int((received - collected).total_seconds()) if collected else None
            nodes.append({
                'node_id': record['node_id'],
                'node_name': record['node_name'],
                'state': state,
                'age_seconds': age,
                'last_collected_at': sample['collected_at'] if sample else None,
                'last_received_at': record['latest_received_at'],
                'clock_skew_seconds': clock_skew,
                'clock_skew_warning': (
                    abs(clock_skew) > clock_skew_warn if clock_skew is not None else False
                ),
                'sample_status': sample['status'] if sample else 'missing',
                'expected_cards': record['expected_cards'],
                'collected_cards': len(cards),
                'coverage_percent': sample['coverage_percent'] if sample else 0.0,
                'utilization_avg': round(utilization, 2) if utilization is not None else None,
                'hbm_used_mb': hbm_used if hbm_total else None,
                'hbm_total_mb': hbm_total if hbm_total else None,
                'hbm_percent': round(hbm_used * 100.0 / hbm_total, 2) if hbm_total else None,
                'cards': cards,
            })
        total_expected = sum(node['expected_cards'] for node in nodes)
        active_nodes = [node for node in nodes if node['state'] in ('online', 'degraded')]
        total_collected = sum(node['collected_cards'] for node in active_nodes)
        hbm_cards = [card for card in all_cards if card['hbm_total_mb']]
        hbm_used = sum(card['hbm_used_mb'] for card in hbm_cards)
        hbm_total = sum(card['hbm_total_mb'] for card in hbm_cards)
        return {
            'snapshot_version': 1,
            'generated_at': now.isoformat(timespec='seconds'),
            'node_counts': {
                state: sum(node['state'] == state for node in nodes)
                for state in ('online', 'degraded', 'stale', 'offline')
            },
            'total_nodes': len(nodes),
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
            'nodes': nodes,
        }

    def history_series(self, start_epoch, end_epoch, bucket_seconds=300,
                       node_id=None):
        parameters = [bucket_seconds, bucket_seconds, start_epoch, end_epoch]
        node_filter = ''
        if node_id:
            node_filter = ' AND c.node_id = ?'
            parameters.append(node_id)
        query = '''
            SELECT (c.collected_epoch / ?) * ? AS bucket_epoch,
                   AVG(c.utilization) AS utilization_avg,
                   MIN(c.utilization) AS utilization_min,
                   MAX(c.utilization) AS utilization_max,
                   COUNT(*) AS card_samples,
                   COUNT(DISTINCT c.node_id) AS node_count,
                   SUM(CASE WHEN c.hbm_total_mb IS NOT NULL THEN c.hbm_used_mb ELSE 0 END)
                       AS hbm_used_sum,
                   SUM(CASE WHEN c.hbm_total_mb IS NOT NULL THEN c.hbm_total_mb ELSE 0 END)
                       AS hbm_total_sum
            FROM cards c
            WHERE c.collected_epoch >= ? AND c.collected_epoch < ? {}
            GROUP BY bucket_epoch ORDER BY bucket_epoch
        '''.format(node_filter)
        with self.lock:
            rows = self.connection.execute(query, parameters).fetchall()
        values_by_epoch = {}
        for row in rows:
            value = dict(row)
            epoch = value.pop('bucket_epoch')
            value['timestamp'] = datetime.fromtimestamp(
                epoch, timezone.utc
            ).isoformat(timespec='seconds')
            value['utilization_avg'] = round(value['utilization_avg'], 2)
            value['utilization_min'] = round(value['utilization_min'], 2)
            value['utilization_max'] = round(value['utilization_max'], 2)
            total = value.pop('hbm_total_sum')
            used = value.pop('hbm_used_sum')
            value['hbm_percent'] = round(used * 100.0 / total, 2) if total else None
            values_by_epoch[epoch] = value

        sample_parameters = [bucket_seconds, bucket_seconds, start_epoch, end_epoch]
        sample_node_filter = ''
        if node_id:
            sample_node_filter = ' AND node_id = ?'
            sample_parameters.append(node_id)
        sample_query = '''
            SELECT (collected_epoch / ?) * ? AS bucket_epoch,
                   COUNT(*) AS attempts,
                   SUM(CASE WHEN sample_status = 'complete' THEN 1 ELSE 0 END) AS complete,
                   SUM(CASE WHEN sample_status = 'partial' THEN 1 ELSE 0 END) AS partial,
                   SUM(CASE WHEN sample_status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(expected_cards) AS expected_card_samples,
                   SUM(collected_cards) AS collected_card_samples
            FROM samples
            WHERE collected_epoch >= ? AND collected_epoch < ? {}
            GROUP BY bucket_epoch ORDER BY bucket_epoch
        '''.format(sample_node_filter)
        with self.lock:
            sample_rows = self.connection.execute(
                sample_query, sample_parameters
            ).fetchall()
        for row in sample_rows:
            item = dict(row)
            epoch = item.pop('bucket_epoch')
            value = values_by_epoch.setdefault(epoch, {
                'timestamp': datetime.fromtimestamp(
                    epoch, timezone.utc
                ).isoformat(timespec='seconds'),
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

    def delete_before(self, cutoff_epoch):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                cursor = self.connection.execute(
                    'DELETE FROM samples WHERE collected_epoch < ?', (cutoff_epoch,)
                )
                deleted = cursor.rowcount
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
