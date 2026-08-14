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
        self.last_success = None
        self.last_error = None
        self.consecutive_failures = 0
        try:
            self.rejected_total = sum(
                name.endswith('.json') and not name.endswith('.reason.json')
                for name in os.listdir(rejected_dir)
            )
        except OSError:
            self.rejected_total = 0

    def notify(self):
        self.wakeup.set()

    def stop(self):
        self.stop_event.set()
        self.wakeup.set()

    def _write_health(self):
        files, size = queue_usage(self.spool_dir)
        write_json_atomic(self.health_file, {
            'status': (
                'healthy' if not self.last_error and not self.rejected_total
                else 'degraded'
            ),
            'last_success': self.last_success,
            'last_error': self.last_error,
            'consecutive_failures': self.consecutive_failures,
            'pending_samples': len(files),
            'pending_bytes': size,
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
            self._write_health()
            return True
        for path in files[:self.batch_size]:
            try:
                sample = load_queued(path)
            except (OSError, ValueError) as exc:
                reject_queued(path, self.rejected_dir, 'unreadable queue item: {}'.format(exc))
                self.rejected_total += 1
                continue
            success, status, detail = self._post(sample)
            if success:
                try:
                    os.remove(path)
                except OSError as exc:
                    self.last_error = 'uploaded but cannot remove queue item: {}'.format(exc)
                    self.consecutive_failures += 1
                    self._write_health()
                    return False
                self.last_success = datetime.now(timezone.utc).isoformat(timespec='seconds')
                self.last_error = None
                self.consecutive_failures = 0
                continue
            if status is not None and 400 <= status < 500:
                reason = 'collector rejected sample with HTTP {}: {}'.format(status, detail)
                reject_queued(path, self.rejected_dir, reason)
                self.rejected_total += 1
                self.last_error = reason
                LOGGER.error('%s', reason)
                continue
            self.consecutive_failures += 1
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
