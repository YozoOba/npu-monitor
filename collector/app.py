#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import signal
import socketserver
import sys
import threading
from urllib.parse import parse_qs, urlparse

from cluster_common.atomic import write_json_atomic
from cluster_common.protocol import ProtocolError, normalize_sample, parse_timestamp
from cluster_common.storage_health import check_capacity
from cluster_common.timezones import timezone_label
from . import __version__
from . import config
from .management import ConfirmationMismatchError, ManagementService
from .storage import CollectorStorage, ConflictingSampleError


LOGGER = logging.getLogger('npu_collector')


def query_int(query, name, default, minimum, maximum):
    value = int(query.get(name, [str(default)])[0])
    if value < minimum or value > maximum:
        raise ValueError('{} must be between {} and {}'.format(
            name, minimum, maximum
        ))
    return value


def query_time_range(query, default_hours=24):
    now = datetime.now(timezone.utc)
    start = parse_timestamp(query.get('start', [
        (now - timedelta(hours=default_hours)).isoformat(timespec='seconds')
    ])[0]).astimezone(timezone.utc)
    end = parse_timestamp(query.get('end', [
        now.isoformat(timespec='seconds')
    ])[0]).astimezone(timezone.utc)
    if end <= start:
        raise ValueError('end must be later than start')
    if (end - start).total_seconds() > config.RETENTION_DAYS * 86400:
        raise ValueError('time range exceeds collector retention window')
    return start, end


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class CollectorApplication:
    def __init__(self, storage):
        self.storage = storage
        self.management = ManagementService(
            storage, config.ARCHIVE_DATABASE_PATH, config.BACKUP_DIR
        )
        self.snapshot_lock = threading.Lock()
        self.snapshot = None
        self.stop_event = threading.Event()
        self.maintenance = None

    def refresh_snapshot(self):
        with self.snapshot_lock:
            # Serialize refreshes triggered by concurrent Agent uploads so an
            # older build can never overwrite a newer in-memory snapshot.
            snapshot = self.storage.build_snapshot(
                config.STALE_FLOOR_SECONDS, config.OFFLINE_FLOOR_SECONDS,
                config.CLOCK_SKEW_WARN_SECONDS,
                config.BUSY_UTILIZATION, config.IDLE_UTILIZATION,
            )
            snapshot['active_alert_count'] = self.storage.sync_alerts(snapshot)
            write_json_atomic(config.SNAPSHOT_PATH, snapshot)
            self.snapshot = snapshot
        return snapshot

    def get_snapshot(self):
        with self.snapshot_lock:
            value = self.snapshot
        return value or self.refresh_snapshot()

    def run_maintenance(self):
        last_cleanup_day = None
        while not self.stop_event.is_set():
            try:
                self.refresh_snapshot()
                today = datetime.now(timezone.utc).date()
                if today != last_cleanup_day:
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        days=config.RETENTION_DAYS
                    )
                    archived_samples, archived_alerts = self.storage.archive_before(
                        int(cutoff.timestamp()), config.ARCHIVE_DATABASE_PATH
                    )
                    if archived_samples or archived_alerts:
                        LOGGER.info(
                            'archived %s samples and %s resolved alerts',
                            archived_samples, archived_alerts,
                        )
                    last_cleanup_day = today
            except Exception:
                LOGGER.exception('collector maintenance failed')
            self.stop_event.wait(config.SNAPSHOT_INTERVAL)

    def start(self):
        self.refresh_snapshot()
        self.maintenance = threading.Thread(
            target=self.run_maintenance, name='collector-maintenance', daemon=True
        )
        self.maintenance.start()

    def stop(self):
        self.stop_event.set()
        if self.maintenance:
            self.maintenance.join(timeout=config.SNAPSHOT_INTERVAL + 1)


def make_handler(application):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'NPUCollector/1.0'

        def log_message(self, format_text, *args):
            LOGGER.info('%s - %s', self.client_address[0], format_text % args)

        def send_json(self, status, value):
            payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            path = urlparse(self.path).path
            if path in (
                    '/internal/v1/admin/preview',
                    '/internal/v1/admin/execute'):
                raw_length = self.headers.get('Content-Length')
                try:
                    length = int(raw_length)
                    if length <= 0 or length > config.MAX_BODY_BYTES:
                        raise ValueError('request body size is invalid')
                    request = json.loads(self.rfile.read(length).decode('utf-8'))
                    if path.endswith('/preview'):
                        self.send_json(200, application.management.preview(request))
                    else:
                        result = application.management.execute(request)
                        application.refresh_snapshot()
                        self.send_json(200, result)
                except ConfirmationMismatchError as exc:
                    self.send_json(409, {'error': str(exc)})
                except (TypeError, ValueError, UnicodeDecodeError,
                        json.JSONDecodeError, ProtocolError) as exc:
                    self.send_json(400, {'error': str(exc)})
                except Exception as exc:
                    LOGGER.exception('management operation failed')
                    self.send_json(500, {'error': str(exc)})
                return
            if path != '/api/v1/samples':
                self.send_json(404, {'error': 'not found'})
                return
            raw_length = self.headers.get('Content-Length')
            try:
                length = int(raw_length)
            except (TypeError, ValueError):
                self.send_json(411, {'error': 'Content-Length is required'})
                return
            if length <= 0 or length > config.MAX_BODY_BYTES:
                self.send_json(413, {'error': 'request body size is invalid'})
                return
            storage_ok, storage_status = check_capacity(
                application.storage.path, config.MIN_FREE_BYTES,
                config.MIN_FREE_INODES,
            )
            if not storage_ok:
                self.send_json(507, {
                    'accepted': False, 'error': 'collector storage unavailable',
                    'storage': storage_status,
                })
                return
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
                received_at = datetime.now(timezone.utc)
                normalized = normalize_sample(
                    payload, received_at=received_at,
                    max_future_seconds=config.MAX_FUTURE_SECONDS,
                )
                created = application.storage.ingest(normalized, received_at)
                if created:
                    application.refresh_snapshot()
                self.send_json(201 if created else 200, {
                    'accepted': True,
                    'duplicate': not created,
                    'sample_id': normalized['sample_id'],
                })
            except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
                self.send_json(400, {'accepted': False, 'error': str(exc)})
            except ConflictingSampleError as exc:
                self.send_json(409, {'accepted': False, 'error': str(exc)})
            except Exception as exc:
                LOGGER.exception('sample ingestion failed')
                self.send_json(500, {'accepted': False, 'error': str(exc)})

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == '/health':
                try:
                    health = application.storage.health()
                    storage_ok, storage_status = check_capacity(
                        application.storage.path, config.MIN_FREE_BYTES,
                        config.MIN_FREE_INODES,
                    )
                    health['storage'] = storage_status
                    healthy = health['database_integrity'] == 'ok' and storage_ok
                    self.send_json(200 if healthy else 503, {
                        'status': 'healthy' if healthy else 'unhealthy',
                        'collector_version': __version__,
                        'report_timezone': config.REPORT_TIMEZONE_NAME,
                        **health,
                    })
                except Exception as exc:
                    self.send_json(503, {'status': 'unhealthy', 'error': str(exc)})
                return
            if parsed.path == '/internal/v1/snapshot':
                query = parse_qs(parsed.query)
                cluster_id = query.get('cluster_id', [None])[0]
                snapshot = (
                    application.get_snapshot() if not cluster_id
                    else application.storage.build_snapshot(
                        config.STALE_FLOOR_SECONDS, config.OFFLINE_FLOOR_SECONDS,
                        config.CLOCK_SKEW_WARN_SECONDS,
                        config.BUSY_UTILIZATION, config.IDLE_UTILIZATION,
                        cluster_id=cluster_id,
                    )
                )
                if cluster_id:
                    snapshot['active_alert_count'] = (
                        application.storage.active_alert_count(cluster_id)
                    )
                self.send_json(200, snapshot)
                return
            if parsed.path == '/internal/v1/clusters':
                snapshot = application.get_snapshot()
                self.send_json(200, {'items': snapshot.get('clusters', [])})
                return
            if parsed.path == '/internal/v1/nodes':
                try:
                    query = parse_qs(parsed.query)
                    page = query_int(query, 'page', 1, 1, 1000000)
                    page_size = query_int(query, 'page_size', 50, 1, 500)
                    cluster_id = query.get('cluster_id', [None])[0]
                    state = query.get('state', [None])[0]
                    if state and state not in ('online', 'degraded', 'stale', 'offline'):
                        raise ValueError('invalid state')
                    search = query.get('q', [''])[0].strip().lower()
                    snapshot = (
                        application.get_snapshot() if not cluster_id
                        else application.storage.build_snapshot(
                            config.STALE_FLOOR_SECONDS,
                            config.OFFLINE_FLOOR_SECONDS,
                            config.CLOCK_SKEW_WARN_SECONDS,
                            config.BUSY_UTILIZATION,
                            config.IDLE_UTILIZATION,
                            cluster_id=cluster_id,
                        )
                    )
                    nodes = snapshot['nodes']
                    if state:
                        nodes = [node for node in nodes if node['state'] == state]
                    if search:
                        nodes = [node for node in nodes if search in (
                            node['node_id'] + '\n' + node['node_name']
                        ).lower()]
                    total = len(nodes)
                    offset = (page - 1) * page_size
                    self.send_json(200, {
                        'page': page,
                        'page_size': page_size,
                        'total': total,
                        'pages': (total + page_size - 1) // page_size,
                        'items': nodes[offset:offset + page_size],
                    })
                except ValueError as exc:
                    self.send_json(400, {'error': str(exc)})
                return
            if parsed.path == '/internal/v1/series':
                try:
                    query = parse_qs(parsed.query)
                    start, end = query_time_range(query)
                    bucket = query_int(query, 'bucket', 300, 60, 86400)
                    if (end - start).total_seconds() / bucket > 10000:
                        raise ValueError('time range produces more than 10000 buckets')
                    node_id = query.get('node_id', [None])[0]
                    cluster_id = query.get('cluster_id', [None])[0]
                    raw_card_id = query.get('card_id', [None])[0]
                    card_id = None if raw_card_id is None else int(raw_card_id)
                    if card_id is not None and not 0 <= card_id <= 63:
                        raise ValueError('card_id must be between 0 and 63')
                    series = application.storage.history_series(
                        int(start.timestamp()), int(end.timestamp()), bucket,
                        node_id, cluster_id, card_id,
                    )
                    self.send_json(200, {
                        'start': start.isoformat(timespec='seconds'),
                        'end': end.isoformat(timespec='seconds'),
                        'bucket_seconds': bucket,
                        'node_id': node_id,
                        'cluster_id': cluster_id,
                        'card_id': card_id,
                        'points': series,
                    })
                except (ValueError, ProtocolError) as exc:
                    self.send_json(400, {'error': str(exc)})
                return
            if parsed.path == '/internal/v1/samples':
                try:
                    query = parse_qs(parsed.query)
                    start, end = query_time_range(query)
                    page = query_int(query, 'page', 1, 1, 1000000)
                    page_size = query_int(query, 'page_size', 100, 1, 1000)
                    sample_status = query.get('status', [None])[0]
                    if sample_status and sample_status not in ('complete', 'partial', 'failed'):
                        raise ValueError('invalid sample status')
                    result = application.storage.query_samples(
                        int(start.timestamp()), int(end.timestamp()), page,
                        page_size, query.get('node_id', [None])[0],
                        query.get('cluster_id', [None])[0], sample_status,
                        query.get('q', [None])[0],
                    )
                    result.update({
                        'start': start.isoformat(timespec='seconds'),
                        'end': end.isoformat(timespec='seconds'),
                    })
                    self.send_json(200, result)
                except (ValueError, ProtocolError) as exc:
                    self.send_json(400, {'error': str(exc)})
                return
            if parsed.path == '/internal/v1/utilization-report':
                try:
                    query = parse_qs(parsed.query)
                    start, end = query_time_range(query)
                    utc_offset_seconds = query_int(
                        query, 'utc_offset_seconds',
                        config.REPORT_TIMEZONE_OFFSET_SECONDS,
                        -14 * 60 * 60, 14 * 60 * 60,
                    )
                    if utc_offset_seconds % 60:
                        raise ValueError(
                            'utc_offset_seconds must use whole minutes'
                        )
                    self.send_json(200, {
                        'start': start.isoformat(timespec='seconds'),
                        'end': end.isoformat(timespec='seconds'),
                        'timezone': timezone_label(utc_offset_seconds),
                        'nodes': application.storage.utilization_report(
                            int(start.timestamp()), int(end.timestamp()),
                            utc_offset_seconds,
                            query.get('node_id', [None])[0],
                            query.get('cluster_id', [None])[0],
                        ),
                    })
                except (ValueError, ProtocolError) as exc:
                    self.send_json(400, {'error': str(exc)})
                return
            if parsed.path == '/internal/v1/alerts':
                try:
                    query = parse_qs(parsed.query)
                    start, end = query_time_range(query)
                    page = query_int(query, 'page', 1, 1, 1000000)
                    page_size = query_int(query, 'page_size', 100, 1, 1000)
                    severity = query.get('severity', [None])[0]
                    status = query.get('status', [None])[0]
                    if severity and severity not in ('warning', 'critical'):
                        raise ValueError('invalid alert severity')
                    if status and status not in ('active', 'resolved'):
                        raise ValueError('invalid alert status')
                    result = application.storage.query_alerts(
                        int(start.timestamp()), int(end.timestamp()), page,
                        page_size, query.get('cluster_id', [None])[0],
                        query.get('node_id', [None])[0], severity, status,
                        query.get('type', [None])[0],
                    )
                    result.update({
                        'start': start.isoformat(timespec='seconds'),
                        'end': end.isoformat(timespec='seconds'),
                    })
                    self.send_json(200, result)
                except (ValueError, ProtocolError) as exc:
                    self.send_json(400, {'error': str(exc)})
                return
            if parsed.path == '/internal/v1/admin/operations':
                try:
                    query = parse_qs(parsed.query)
                    self.send_json(200, application.management.operations(
                        query_int(query, 'page', 1, 1, 1000000),
                        query_int(query, 'page_size', 50, 1, 500),
                    ))
                except ValueError as exc:
                    self.send_json(400, {'error': str(exc)})
                return
            self.send_json(404, {'error': 'not found'})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description='NPU cluster collector')
    parser.add_argument('--check-db', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    config.validate()
    storage = CollectorStorage(config.DATABASE_PATH)
    if args.check_db:
        health = storage.health(check_integrity=True)
        storage.close()
        print(json.dumps(health, ensure_ascii=False, sort_keys=True))
        return 0 if health['database_integrity'] == 'ok' else 1
    application = CollectorApplication(storage)
    server = ThreadingHTTPServer(
        (config.HOST, config.PORT), make_handler(application)
    )

    def stop_server(signum, _frame):
        LOGGER.info('received signal %s, stopping', signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    application.start()
    LOGGER.info('default report timezone is %s', config.REPORT_TIMEZONE_NAME)
    LOGGER.info('collector listening on %s:%s', config.HOST, config.PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        application.stop()
        storage.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
