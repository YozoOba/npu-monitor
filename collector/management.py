from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import uuid

from cluster_common.protocol import (
    CLUSTER_ID_PATTERN, NODE_ID_PATTERN, parse_timestamp,
)


class ConfirmationMismatchError(RuntimeError):
    pass


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    )


def _bool(value, name, default):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError('{} must be a boolean'.format(name))
    return value


class ManagementService:
    """Preview, back up and execute narrowly-scoped data corrections."""

    OPERATIONS = ('update_node', 'delete_node', 'delete_samples')

    def __init__(self, storage, archive_path, backup_dir):
        self.storage = storage
        self.archive_path = os.path.abspath(archive_path)
        self.backup_dir = os.path.abspath(backup_dir)

    @staticmethod
    def _node_id(value, required=True):
        value = value.strip() if isinstance(value, str) else value
        if value in (None, '') and not required:
            return None
        if not isinstance(value, str) or not NODE_ID_PATTERN.fullmatch(value):
            raise ValueError('node_id has an invalid format')
        return value

    @staticmethod
    def _cluster_id(value, required=False):
        value = value.strip() if isinstance(value, str) else value
        if value in (None, '') and not required:
            return None
        if not isinstance(value, str) or not CLUSTER_ID_PATTERN.fullmatch(value):
            raise ValueError('cluster_id has an invalid format')
        return value

    @staticmethod
    def _node_name(value):
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 255:
            raise ValueError('node_name must be 1 to 255 characters')
        return value.strip()

    def normalize(self, request):
        if not isinstance(request, dict):
            raise ValueError('management request must be a JSON object')
        operation = request.get('operation')
        if operation not in self.OPERATIONS:
            raise ValueError('unsupported management operation')
        criteria = {'operation': operation}
        if operation == 'update_node':
            criteria['node_id'] = self._node_id(request.get('node_id'))
            criteria['node_name'] = self._node_name(request.get('node_name'))
            criteria['cluster_id'] = self._cluster_id(request.get('cluster_id'))
            if criteria['node_name'] is None and criteria['cluster_id'] is None:
                raise ValueError('node_name or cluster_id is required')
            return criteria
        if operation == 'delete_node':
            criteria['node_id'] = self._node_id(request.get('node_id'))
            criteria['include_archive'] = _bool(
                request.get('include_archive'), 'include_archive', True
            )
            return criteria

        sample_id = request.get('sample_id')
        if sample_id is not None:
            if not isinstance(sample_id, str) or len(sample_id) != 64 or any(
                    character not in '0123456789abcdef' for character in sample_id):
                raise ValueError('sample_id must be a lowercase SHA-256 value')
            criteria['sample_id'] = sample_id
        else:
            start = parse_timestamp(request.get('start')).astimezone(timezone.utc)
            end = parse_timestamp(request.get('end')).astimezone(timezone.utc)
            if end <= start:
                raise ValueError('end must be later than start')
            criteria['start'] = start.isoformat(timespec='seconds')
            criteria['end'] = end.isoformat(timespec='seconds')
            criteria['start_epoch'] = int(start.timestamp())
            criteria['end_epoch'] = int(end.timestamp())
            criteria['node_id'] = self._node_id(
                request.get('node_id'), required=False
            )
            criteria['cluster_id'] = self._cluster_id(request.get('cluster_id'))
            if not criteria['node_id'] and not criteria['cluster_id']:
                raise ValueError(
                    'delete_samples requires node_id or cluster_id to limit scope'
                )
            status = request.get('status')
            if status not in (None, '', 'complete', 'partial', 'failed'):
                raise ValueError('invalid sample status')
            criteria['status'] = status or None
        criteria['include_archive'] = _bool(
            request.get('include_archive'), 'include_archive', True
        )
        criteria['delete_alerts'] = _bool(
            request.get('delete_alerts'), 'delete_alerts', True
        )
        return criteria

    @staticmethod
    def _sample_where(criteria):
        if criteria.get('sample_id'):
            return 'sample_id = ?', [criteria['sample_id']]
        filters = ['collected_epoch >= ?', 'collected_epoch < ?']
        parameters = [criteria['start_epoch'], criteria['end_epoch']]
        for column in ('node_id', 'cluster_id'):
            if criteria.get(column):
                filters.append('{} = ?'.format(column))
                parameters.append(criteria[column])
        if criteria.get('status'):
            filters.append('sample_status = ?')
            parameters.append(criteria['status'])
        return ' AND '.join(filters), parameters

    @staticmethod
    def _alert_where(criteria):
        if criteria.get('sample_id') or not criteria.get('delete_alerts'):
            return '1 = 0', []
        filters = ['last_seen_epoch >= ?', 'started_epoch < ?']
        parameters = [criteria['start_epoch'], criteria['end_epoch']]
        for column in ('node_id', 'cluster_id'):
            if criteria.get(column):
                filters.append('{} = ?'.format(column))
                parameters.append(criteria[column])
        return ' AND '.join(filters), parameters

    @staticmethod
    def _empty_impact():
        return {'nodes': 0, 'samples': 0, 'cards': 0, 'alerts': 0}

    @staticmethod
    def _counts(connection, schema, criteria, operation, has_nodes=True):
        result = ManagementService._empty_impact()
        prefix = schema + '.'
        if operation in ('update_node', 'delete_node'):
            node_id = criteria['node_id']
            if has_nodes:
                result['nodes'] = connection.execute(
                    'SELECT COUNT(*) FROM nodes WHERE node_id = ?', (node_id,)
                ).fetchone()[0]
            if operation == 'update_node':
                result['alerts'] = connection.execute(
                    'SELECT COUNT(*) FROM {}alerts WHERE node_id = ? '
                    "AND status = 'active'".format(prefix), (node_id,)
                ).fetchone()[0]
                return result
            for table in ('samples', 'cards', 'alerts'):
                result[table] = connection.execute(
                    'SELECT COUNT(*) FROM {}{} WHERE node_id = ?'.format(
                        prefix, table
                    ), (node_id,)
                ).fetchone()[0]
            return result

        sample_where, sample_parameters = ManagementService._sample_where(criteria)
        result['samples'] = connection.execute(
            'SELECT COUNT(*) FROM {}samples WHERE {}'.format(prefix, sample_where),
            sample_parameters,
        ).fetchone()[0]
        result['cards'] = connection.execute(
            'SELECT COUNT(*) FROM {0}cards WHERE sample_id IN '
            '(SELECT sample_id FROM {0}samples WHERE {1})'.format(
                prefix, sample_where
            ), sample_parameters,
        ).fetchone()[0]
        alert_where, alert_parameters = ManagementService._alert_where(criteria)
        result['alerts'] = connection.execute(
            'SELECT COUNT(*) FROM {}alerts WHERE {}'.format(prefix, alert_where),
            alert_parameters,
        ).fetchone()[0]
        if has_nodes:
            result['nodes'] = connection.execute(
                'SELECT COUNT(DISTINCT node_id) FROM samples WHERE {}'.format(
                    sample_where
                ), sample_parameters,
            ).fetchone()[0]
        return result

    def _open_archive(self):
        if not os.path.exists(self.archive_path):
            return None
        connection = sqlite3.connect(self.archive_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _preview_locked(self, criteria):
        operation = criteria['operation']
        hot = self._counts(self.storage.connection, 'main', criteria, operation)
        archived = self._empty_impact()
        if criteria.get('include_archive', False):
            archive = self._open_archive()
            if archive is not None:
                try:
                    archived = self._counts(
                        archive, 'main', criteria, operation, has_nodes=False
                    )
                finally:
                    archive.close()
        if operation == 'update_node' and hot['nodes'] != 1:
            raise ValueError('node_id is not registered in the hot database')
        if operation != 'update_node' and not any(hot.values()) and not any(
                archived.values()):
            raise ValueError('management filter matches no data')
        impact = {'hot': hot, 'archive': archived}
        token_source = {'criteria': criteria, 'impact': impact}
        token = hashlib.sha256(_json(token_source).encode('utf-8')).hexdigest()
        return {
            'criteria': criteria,
            'impact': impact,
            'confirmation_token': token,
            'warning': self._warning(criteria, impact),
        }

    @staticmethod
    def _warning(criteria, impact):
        if criteria['operation'] == 'update_node':
            return (
                '请先修改对应 Agent 配置；Agent 后续上报仍可能把节点名称或集群覆盖回去。'
            )
        total_samples = impact['hot']['samples'] + impact['archive']['samples']
        return (
            '将删除 {} 条采样；系统会先创建并校验数据库备份，'
            'Console 页面本身不提供撤销操作。'
        ).format(total_samples)

    def preview(self, request):
        criteria = self.normalize(request)
        with self.storage.lock:
            return self._preview_locked(criteria)

    def _backup_connection(self, source, label, operation_id):
        os.makedirs(self.backup_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        filename = '{}-before-{}-{}-{}.sqlite3'.format(
            label, operation_id['operation'], timestamp, operation_id['id'][:8]
        )
        path = os.path.join(self.backup_dir, filename)
        target = sqlite3.connect(path)
        try:
            source.backup(target)
            integrity = target.execute('PRAGMA quick_check(1)').fetchone()[0]
            if integrity != 'ok':
                raise RuntimeError('backup integrity check failed: {}'.format(integrity))
        finally:
            target.close()
        return path

    @staticmethod
    def _repair_nodes(connection, node_ids):
        for node_id in node_ids:
            latest = connection.execute('''
                SELECT sample_id, cluster_id, node_name, collected_epoch,
                       received_at, collect_interval, expected_cards
                FROM samples WHERE node_id = ?
                ORDER BY collected_epoch DESC, received_at DESC LIMIT 1
            ''', (node_id,)).fetchone()
            if latest is None:
                connection.execute('DELETE FROM nodes WHERE node_id = ?', (node_id,))
                continue
            connection.execute('''
                UPDATE nodes SET cluster_id = ?, node_name = ?, latest_sample_id = ?,
                    latest_collected_epoch = ?, latest_received_at = ?,
                    collect_interval = ?, expected_cards = ?
                WHERE node_id = ?
            ''', (
                latest['cluster_id'], latest['node_name'], latest['sample_id'],
                latest['collected_epoch'], latest['received_at'],
                latest['collect_interval'], latest['expected_cards'], node_id,
            ))

    @staticmethod
    def _delete_samples(connection, schema, criteria):
        prefix = schema + '.'
        sample_where, sample_parameters = ManagementService._sample_where(criteria)
        affected_nodes = []
        if schema == 'main':
            affected_nodes = [row[0] for row in connection.execute(
                'SELECT DISTINCT node_id FROM samples WHERE {}'.format(sample_where),
                sample_parameters,
            ).fetchall()]
        alert_where, alert_parameters = ManagementService._alert_where(criteria)
        alert_cursor = connection.execute(
            'DELETE FROM {}alerts WHERE {}'.format(prefix, alert_where),
            alert_parameters,
        )
        sample_cursor = connection.execute(
            'DELETE FROM {}samples WHERE {}'.format(prefix, sample_where),
            sample_parameters,
        )
        if schema == 'main':
            ManagementService._repair_nodes(connection, affected_nodes)
        return {
            'samples': sample_cursor.rowcount,
            'alerts': alert_cursor.rowcount,
            'affected_nodes': len(affected_nodes),
        }

    def _apply(self, connection, criteria, archive_attached):
        operation = criteria['operation']
        if operation == 'update_node':
            current = connection.execute(
                'SELECT cluster_id, node_name FROM nodes WHERE node_id = ?',
                (criteria['node_id'],),
            ).fetchone()
            cluster_id = criteria.get('cluster_id') or current['cluster_id']
            node_name = criteria.get('node_name') or current['node_name']
            connection.execute(
                'UPDATE nodes SET cluster_id = ?, node_name = ? WHERE node_id = ?',
                (cluster_id, node_name, criteria['node_id']),
            )
            if cluster_id != current['cluster_id']:
                old_prefix = current['cluster_id'] + ':' + criteria['node_id'] + ':'
                new_prefix = cluster_id + ':' + criteria['node_id'] + ':'
                connection.execute('''
                    UPDATE alerts SET cluster_id = ?,
                        alert_key = ? || substr(alert_key, ?)
                    WHERE node_id = ? AND status = 'active'
                ''', (
                    cluster_id, new_prefix, len(old_prefix) + 1,
                    criteria['node_id'],
                ))
            return {'nodes_updated': 1, 'active_alerts_updated': True}

        schemas = ['main']
        if archive_attached and criteria.get('include_archive', False):
            schemas.append('archive_db')
        if operation == 'delete_node':
            result = {}
            for schema in schemas:
                prefix = schema + '.'
                if schema == 'main':
                    connection.execute(
                        'DELETE FROM nodes WHERE node_id = ?', (criteria['node_id'],)
                    )
                alerts = connection.execute(
                    'DELETE FROM {}alerts WHERE node_id = ?'.format(prefix),
                    (criteria['node_id'],),
                ).rowcount
                samples = connection.execute(
                    'DELETE FROM {}samples WHERE node_id = ?'.format(prefix),
                    (criteria['node_id'],),
                ).rowcount
                result[schema] = {'samples_deleted': samples, 'alerts_deleted': alerts}
            return result

        return {
            schema: self._delete_samples(connection, schema, criteria)
            for schema in schemas
        }

    def execute(self, request):
        supplied_token = request.get('confirmation_token') if isinstance(request, dict) else None
        criteria = self.normalize(request)
        with self.storage.lock:
            preview = self._preview_locked(criteria)
            if supplied_token != preview['confirmation_token']:
                raise ConfirmationMismatchError(
                    'data changed or confirmation token is invalid; preview again'
                )
            operation_id = {'id': uuid.uuid4().hex, 'operation': criteria['operation']}
            backups = {
                'hot': self._backup_connection(
                    self.storage.connection, 'cluster', operation_id
                )
            }
            archive = None
            archive_attached = False
            if criteria.get('include_archive', False) and os.path.exists(
                    self.archive_path):
                archive = self._open_archive()
                try:
                    backups['archive'] = self._backup_connection(
                        archive, 'cluster-archive', operation_id
                    )
                finally:
                    archive.close()
                self.storage.connection.execute(
                    'ATTACH DATABASE ? AS archive_db', (self.archive_path,)
                )
                archive_attached = True
            now = datetime.now(timezone.utc).isoformat(timespec='seconds')
            try:
                self.storage.connection.execute('BEGIN IMMEDIATE')
                result = self._apply(
                    self.storage.connection, criteria, archive_attached
                )
                self.storage.connection.execute('''
                    INSERT INTO admin_operations (
                        operation_id, operation, created_at, criteria_json,
                        impact_json, result_json, backups_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    operation_id['id'], criteria['operation'], now,
                    _json(criteria), _json(preview['impact']), _json(result),
                    _json(backups),
                ))
                self.storage.connection.execute('COMMIT')
            except Exception:
                self.storage.connection.execute('ROLLBACK')
                raise
            finally:
                if archive_attached:
                    self.storage.connection.execute('DETACH DATABASE archive_db')
            return {
                'operation_id': operation_id['id'],
                'operation': criteria['operation'],
                'executed_at': now,
                'impact': preview['impact'],
                'result': result,
                'backups': backups,
            }

    def operations(self, page=1, page_size=50):
        with self.storage.lock:
            total = self.storage.connection.execute(
                'SELECT COUNT(*) FROM admin_operations'
            ).fetchone()[0]
            rows = self.storage.connection.execute('''
                SELECT operation_id, operation, created_at, criteria_json,
                       impact_json, result_json, backups_json
                FROM admin_operations ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            ''', (page_size, (page - 1) * page_size)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for key in ('criteria', 'impact', 'result', 'backups'):
                item[key] = json.loads(item.pop(key + '_json'))
            items.append(item)
        return {
            'page': page, 'page_size': page_size, 'total': total,
            'pages': (total + page_size - 1) // page_size, 'items': items,
        }
