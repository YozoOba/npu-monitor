from datetime import datetime, timezone
import json
import logging
import os
import threading
import time
try:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover
    from urllib2 import HTTPError, URLError, Request, urlopen

from cluster_common.atomic import write_json_atomic
from .storage import load_queued, queue_usage, reject_queued


LOGGER = logging.getLogger('npu_agent.sender')


class UploadWorker(threading.Thread):
    def __init__(self, endpoint, spool_dir, rejected_dir, health_file,
                 timeout=5, batch_size=100):
        super().__init__(name='npu-upload-worker')
        self.daemon = True
        self.endpoint = endpoint.rstrip('/') + '/api/v1/samples'
        self.spool_dir = spool_dir
        self.rejected_dir = rejected_dir
        self.health_file = health_file
        self.timeout = timeout
        self.batch_size = batch_size
        self.stop_event = threading.Event()
        self.wakeup = threading.Event()
        persisted = self._load_health()
        self.last_success = persisted.get('last_success')
        self.last_failure_at = persisted.get('last_failure_at')
        self.upload_unavailable_since = persisted.get('upload_unavailable_since')
        self.last_error = persisted.get('last_error')
        self.consecutive_failures = self._nonnegative_int(
            persisted.get('consecutive_failures')
        )
        self.total_successes = self._nonnegative_int(
            persisted.get('total_successes')
        )
        self.total_failures = self._nonnegative_int(
            persisted.get('total_failures')
        )
        try:
            self.rejected_total = sum(
                name.endswith('.json') and not name.endswith('.reason.json')
                for name in os.listdir(rejected_dir)
            )
        except OSError:
            self.rejected_total = 0

        files, _size = queue_usage(self.spool_dir)
        if files:
            oldest_at, _age = self._oldest_pending(files)
            if (not self.upload_unavailable_since and
                    (self.last_error or self.consecutive_failures)):
                self.upload_unavailable_since = oldest_at
        else:
            self.last_error = None
            self.consecutive_failures = 0
            self.upload_unavailable_since = None

    @staticmethod
    def _nonnegative_int(value):
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _load_health(self):
        try:
            with open(self.health_file, 'r', encoding='utf-8') as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _oldest_pending(files, now=None):
        if not files:
            return None, None
        now = now or datetime.now(timezone.utc)
        oldest_path = files[0]
        name = os.path.basename(oldest_path)
        try:
            epoch = int(name.split('_', 1)[0])
        except (TypeError, ValueError):
            try:
                epoch = int(os.path.getmtime(oldest_path))
            except OSError:
                return None, None
        oldest = datetime.fromtimestamp(epoch, timezone.utc)
        age = max(0, int((now - oldest).total_seconds()))
        return oldest.isoformat(timespec='seconds'), age

    def notify(self):
        self.wakeup.set()

    def stop(self):
        self.stop_event.set()
        self.wakeup.set()

    def _write_health(self):
        files, size = queue_usage(self.spool_dir)
        oldest_pending_at, oldest_pending_age = self._oldest_pending(files)
        write_json_atomic(self.health_file, {
            'status': (
                'healthy' if not files and not self.last_error and not self.rejected_total
                else 'degraded'
            ),
            'last_success': self.last_success,
            'last_failure_at': self.last_failure_at,
            'upload_unavailable_since': self.upload_unavailable_since,
            'last_error': self.last_error,
            'consecutive_failures': self.consecutive_failures,
            'total_successes': self.total_successes,
            'total_failures': self.total_failures,
            'pending_samples': len(files),
            'pending_bytes': size,
            'oldest_pending_at': oldest_pending_at,
            'oldest_pending_age_seconds': oldest_pending_age,
            'rejected_total': self.rejected_total,
            'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        })

    def _post(self, sample):
        body = json.dumps(sample, ensure_ascii=False, sort_keys=True).encode('utf-8')
        request = Request(
            self.endpoint, data=body,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST',
        )
        try:
            response = urlopen(request, timeout=self.timeout)
            status = response.getcode()
            response.read()
            return status in (200, 201, 202), status, None
        except HTTPError as exc:
            try:
                detail = exc.read().decode('utf-8', errors='replace')[:1000]
            except Exception:
                detail = str(exc)
            return False, exc.code, detail
        except (URLError, OSError) as exc:
            return False, None, str(exc)

    def _drain_once(self):
        files, _size = queue_usage(self.spool_dir)
        if not files:
            self.last_error = None
            self.consecutive_failures = 0
            self.upload_unavailable_since = None
            self._write_health()
            return True
        for path in files[:self.batch_size]:
            try:
                sample = load_queued(path)
            except (OSError, ValueError) as exc:
                reject_queued(path, self.rejected_dir, 'unreadable queue item: {}'.format(exc))
                self.rejected_total += 1
                self.total_failures += 1
                self.last_failure_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
                continue
            success, status, detail = self._post(sample)
            if success:
                try:
                    os.remove(path)
                except OSError as exc:
                    self.last_error = 'uploaded but cannot remove queue item: {}'.format(exc)
                    self.consecutive_failures += 1
                    self.total_failures += 1
                    failure_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
                    self.last_failure_at = failure_at
                    if not self.upload_unavailable_since:
                        self.upload_unavailable_since = failure_at
                    self._write_health()
                    return False
                self.last_success = datetime.now(timezone.utc).isoformat(timespec='seconds')
                self.last_error = None
                self.consecutive_failures = 0
                self.total_successes += 1
                self.upload_unavailable_since = None
                continue
            if status is not None and 400 <= status < 500:
                reason = 'collector rejected sample with HTTP {}: {}'.format(status, detail)
                reject_queued(path, self.rejected_dir, reason)
                self.rejected_total += 1
                self.last_error = reason
                self.last_failure_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
                self.total_failures += 1
                LOGGER.error('%s', reason)
                continue
            self.consecutive_failures += 1
            self.total_failures += 1
            failure_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self.last_failure_at = failure_at
            if not self.upload_unavailable_since:
                self.upload_unavailable_since = failure_at
            self.last_error = 'upload failed: {}'.format(detail or status or 'unknown error')
            LOGGER.warning('%s', self.last_error)
            self._write_health()
            return False
        self._write_health()
        remaining, _size = queue_usage(self.spool_dir)
        return not remaining

    def run(self):
        backoff = 1
        self._write_health()
        while not self.stop_event.is_set():
            drained = self._drain_once()
            if drained:
                backoff = 1
                self.wakeup.wait(30)
            else:
                self.wakeup.wait(backoff)
                backoff = min(backoff * 2, 60)
            self.wakeup.clear()
        self._write_health()
