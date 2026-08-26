#!/usr/bin/env python3
import argparse
import csv
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from agent.sampler import parse_npu_smi_output
from config import (
    ARCHIVE_DIR,
    COLLECT_INTERVAL,
    DAILY_DIR,
    DATA_RETENTION_DAYS,
    EXPECTED_NPU_COUNT,
    HEALTH_FILE,
    LOG_BACKUP_COUNT,
    LOG_DIR,
    LOG_MAX_BYTES,
    MIN_FREE_BYTES,
    MIN_FREE_INODES,
    NPU_SMI_TIMEOUT,
    SAMPLE_STATUS_DIR,
)


CST = timezone(timedelta(hours=8))
CSV_FIELDS = [
    'timestamp', 'card_id', 'utilization', 'hbm_used_mb', 'hbm_total_mb'
]
LEGACY_CSV_FIELDS = ['timestamp', 'card_id', 'utilization']
LOGGER = logging.getLogger('npu_monitor')
STOP_EVENT = threading.Event()
PID_FILE = None


class CSTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        timestamp = datetime.fromtimestamp(record.created, CST)
        return timestamp.strftime(datefmt or '%Y-%m-%d %H:%M:%S')


def configure_logging(include_console=True):
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    formatter = CSTFormatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, 'npu_monitor.log'),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    if include_console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        LOGGER.addHandler(stream_handler)


def signal_handler(signum, _frame):
    LOGGER.info('Received signal %s, shutting down...', signum)
    STOP_EVENT.set()


def acquire_pid_file(pid_file):
    """Create the PID file exclusively so concurrent starts cannot both win."""
    global PID_FILE
    if not pid_file:
        return

    pid_file = os.path.abspath(pid_file)
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    try:
        fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise RuntimeError(f'PID file already exists: {pid_file}') from exc

    try:
        os.write(fd, f'{os.getpid()}\n'.encode('ascii'))
        os.fsync(fd)
    finally:
        os.close(fd)
    PID_FILE = pid_file


def release_pid_file():
    """Never remove a PID file that has subsequently changed ownership."""
    if not PID_FILE:
        return
    try:
        with open(PID_FILE, 'r', encoding='ascii') as handle:
            owner_pid = int(handle.read().strip())
        if owner_pid == os.getpid():
            os.remove(PID_FILE)
    except (OSError, ValueError):
        pass


def collect_npu_data():
    try:
        result = subprocess.run(
            ['npu-smi', 'info'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=NPU_SMI_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        LOGGER.error('npu-smi command timed out after %s seconds', NPU_SMI_TIMEOUT)
        return []
    except OSError as exc:
        LOGGER.error('Unable to execute npu-smi: %s', exc)
        return []

    if result.returncode != 0:
        LOGGER.error(
            'npu-smi failed with exit code %s: %s',
            result.returncode,
            result.stderr.strip()[:1000],
        )
        return []

    cards = parse_npu_smi_output(result.stdout)
    if len(cards) != EXPECTED_NPU_COUNT:
        LOGGER.warning(
            'Incomplete NPU sample: expected %s cards, parsed %s (IDs: %s)',
            EXPECTED_NPU_COUNT,
            len(cards),
            ','.join(str(card['card_id']) for card in cards) or 'none',
        )
    return cards


def timestamp_text(value):
    return value.isoformat(timespec='seconds')


def write_json_atomic(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = f'{path}.{os.getpid()}.tmp'
    try:
        with open(temp_path, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return True
    except OSError as exc:
        LOGGER.error('Unable to update %s: %s', path, exc)
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return False


def check_storage_capacity(path=DAILY_DIR):
    try:
        usage = os.statvfs(path)
    except AttributeError:
        # Windows is used only for development tests; production is Linux.
        return True, None
    except OSError as exc:
        return False, f'unable to inspect storage: {exc}'

    free_bytes = usage.f_bavail * usage.f_frsize
    free_inodes = usage.f_favail
    if free_bytes < MIN_FREE_BYTES:
        return False, f'low disk space: {free_bytes} free bytes'
    if MIN_FREE_INODES and free_inodes < MIN_FREE_INODES:
        return False, f'low inode count: {free_inodes} free inodes'
    return True, None


def save_sample_status(collected_at, cards, storage_ok, error=None,
                       status_dir=SAMPLE_STATUS_DIR):
    received_ids = sorted(card['card_id'] for card in cards)
    expected_ids = list(range(EXPECTED_NPU_COUNT))
    missing_ids = sorted(set(expected_ids) - set(received_ids))
    status = 'complete' if not missing_ids and storage_ok else 'partial'
    if not cards or not storage_ok:
        status = 'failed'
    record = {
        'timestamp': timestamp_text(collected_at),
        'status': status,
        'expected_cards': EXPECTED_NPU_COUNT,
        'collected_cards': len(cards),
        'received_card_ids': received_ids,
        'missing_card_ids': missing_ids,
        'coverage_percent': round(len(cards) * 100.0 / EXPECTED_NPU_COUNT, 2),
        'storage_ok': storage_ok,
        'error': error,
    }
    filename = f'samples_{collected_at.strftime("%Y-%m-%d")}.jsonl'
    path = os.path.join(status_dir, filename)
    os.makedirs(status_dir, exist_ok=True)
    try:
        payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError(f'short write: {written}/{len(payload)} bytes')
            os.fsync(fd)
        finally:
            os.close(fd)
        return record
    except OSError as exc:
        LOGGER.error('Unable to append sample status %s: %s', path, exc)
        return record


def _serialize_rows(cards, timestamp, fields, include_header):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator='\n')
    if include_header:
        writer.writeheader()
    for card in cards:
        row = {
            'timestamp': timestamp,
            'card_id': card['card_id'],
            'utilization': card['utilization'],
            'hbm_used_mb': '' if card.get('hbm_used_mb') is None else card['hbm_used_mb'],
            'hbm_total_mb': '' if card.get('hbm_total_mb') is None else card['hbm_total_mb'],
        }
        writer.writerow({field: row[field] for field in fields})
    return buffer.getvalue().encode('utf-8')


def _existing_csv_fields(csv_file):
    if not os.path.exists(csv_file) or os.path.getsize(csv_file) == 0:
        return CSV_FIELDS, True
    with open(csv_file, 'r', encoding='utf-8', newline='') as handle:
        header = next(csv.reader(handle), [])
    if header == CSV_FIELDS:
        return CSV_FIELDS, False
    if header == LEGACY_CSV_FIELDS:
        return LEGACY_CSV_FIELDS, False
    raise ValueError(f'unsupported CSV header: {header!r}')


def save_to_csv(cards, collected_at, daily_dir=DAILY_DIR):
    if not cards:
        return False

    date_string = collected_at.strftime('%Y-%m-%d')
    timestamp = timestamp_text(collected_at)
    csv_file = os.path.join(daily_dir, f'stats_{date_string}.csv')
    os.makedirs(daily_dir, exist_ok=True)

    try:
        fields, include_header = _existing_csv_fields(csv_file)
        payload = _serialize_rows(cards, timestamp, fields, include_header)
        fd = os.open(csv_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            # One append payload prevents readers from seeing half a card batch.
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError(f'short write: {written}/{len(payload)} bytes')
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except (OSError, ValueError) as exc:
        LOGGER.error('Unable to append %s: %s', csv_file, exc)
        return False


def _archive_old_files(source_dir, archive_dir, pattern, today, retention_days):
    today = today or datetime.now(CST).date()
    cutoff_date = today - timedelta(days=retention_days)
    archived_count = 0

    try:
        filenames = os.listdir(source_dir)
    except OSError as exc:
        LOGGER.error('Unable to list data directory %s: %s', source_dir, exc)
        return 0

    for filename in filenames:
        match = re.fullmatch(pattern, filename)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group(1), '%Y-%m-%d').date()
        except ValueError:
            continue
        if file_date >= cutoff_date:
            continue
        try:
            os.makedirs(archive_dir, exist_ok=True)
            source = os.path.join(source_dir, filename)
            target = os.path.join(archive_dir, filename)
            if os.path.exists(target):
                LOGGER.error('Archive target already exists; keeping source: %s', target)
                continue
            os.replace(source, target)
            archived_count += 1
        except OSError as exc:
            LOGGER.error('Unable to archive old data file %s: %s', filename, exc)

    if archived_count:
        LOGGER.info('Archived %s data files older than %s', archived_count, cutoff_date)
    return archived_count


def clean_old_data(today=None, daily_dir=DAILY_DIR, retention_days=DATA_RETENTION_DAYS,
                   archive_dir=None):
    """Archive files outside the hot window; retained name for compatibility."""
    archive_dir = archive_dir or os.path.join(ARCHIVE_DIR, 'daily')
    return _archive_old_files(
        daily_dir, archive_dir, r'stats_(\d{4}-\d{2}-\d{2})\.csv',
        today, retention_days,
    )


def clean_old_sample_status(today=None, status_dir=SAMPLE_STATUS_DIR,
                            retention_days=DATA_RETENTION_DAYS, archive_dir=None):
    """Archive status files outside the hot window; retained name for compatibility."""
    archive_dir = archive_dir or os.path.join(ARCHIVE_DIR, 'sample_status')
    return _archive_old_files(
        status_dir, archive_dir, r'samples_(\d{4}-\d{2}-\d{2})\.jsonl',
        today, retention_days,
    )


def seconds_to_next_interval(now=None):
    now = time.time() if now is None else now
    next_run = (int(now) // COLLECT_INTERVAL + 1) * COLLECT_INTERVAL
    return max(0.0, next_run - now)


def run_monitor(once=False):
    LOGGER.info('NPU Monitor started')
    LOGGER.info('Collection interval: %s seconds', COLLECT_INTERVAL)
    LOGGER.info('Expected NPU count: %s', EXPECTED_NPU_COUNT)
    LOGGER.info('Data directory: %s', DAILY_DIR)

    last_cleanup_date = None
    last_success = None
    consecutive_failures = 0
    write_json_atomic(HEALTH_FILE, {
        'status': 'starting',
        'pid': os.getpid(),
        'expected_cards': EXPECTED_NPU_COUNT,
    })
    while not STOP_EVENT.is_set():
        collected_at = datetime.now(CST)
        if collected_at.date() != last_cleanup_date:
            clean_old_data(today=collected_at.date())
            clean_old_sample_status(today=collected_at.date())
            last_cleanup_date = collected_at.date()

        cards = collect_npu_data()
        capacity_ok, storage_error = check_storage_capacity()
        if not capacity_ok:
            LOGGER.error('%s', storage_error)
        data_saved = bool(cards) and capacity_ok and save_to_csv(cards, collected_at)
        storage_ok = capacity_ok and (not cards or data_saved)
        if cards and capacity_ok and not data_saved:
            storage_error = 'CSV write failed'
        collection_succeeded = bool(cards) and data_saved
        sample = save_sample_status(
            collected_at, cards, storage_ok, error=storage_error
        )
        if collection_succeeded:
            last_success = collected_at
            if sample['status'] == 'complete':
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            LOGGER.info(
                'Collected data for %s/%s cards (%s%% coverage)',
                len(cards), EXPECTED_NPU_COUNT, sample['coverage_percent']
            )
        else:
            consecutive_failures += 1
            LOGGER.error('Collection failed (%s consecutive failures)', consecutive_failures)

        health_status = 'healthy'
        if sample['status'] == 'partial':
            health_status = 'degraded'
        elif sample['status'] == 'failed':
            health_status = 'unhealthy'
        last_error = storage_error
        if not cards and not last_error:
            last_error = 'npu-smi returned no usable card data'
        elif sample['missing_card_ids'] and not last_error:
            last_error = 'missing cards: {}'.format(','.join(
                str(card_id) for card_id in sample['missing_card_ids']
            ))
        write_json_atomic(HEALTH_FILE, {
            'status': health_status,
            'pid': os.getpid(),
            'last_attempt': timestamp_text(collected_at),
            'last_success': timestamp_text(last_success) if last_success else None,
            'expected_cards': EXPECTED_NPU_COUNT,
            'collected_cards': len(cards),
            'missing_card_ids': sample['missing_card_ids'],
            'coverage_percent': sample['coverage_percent'],
            'consecutive_failures': consecutive_failures,
            'storage_ok': storage_ok,
            'last_error': last_error,
        })

        if once:
            return 0 if collection_succeeded else 1
        STOP_EVENT.wait(seconds_to_next_interval())

    LOGGER.info('NPU Monitor stopped')
    write_json_atomic(HEALTH_FILE, {
        'status': 'stopped',
        'pid': os.getpid(),
        'stopped_at': timestamp_text(datetime.now(CST)),
    })
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Collect NPU utilization metrics')
    parser.add_argument('--pid-file', help='PID file owned by this process')
    parser.add_argument('--once', action='store_true', help='collect once and exit')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(include_console=not bool(args.pid_file))
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    try:
        acquire_pid_file(args.pid_file)
        return run_monitor(once=args.once)
    except RuntimeError as exc:
        LOGGER.error('%s', exc)
        return 1
    except Exception as exc:
        LOGGER.exception('Fatal monitor error: %s', exc)
        write_json_atomic(HEALTH_FILE, {
            'status': 'unhealthy',
            'pid': os.getpid(),
            'last_error': str(exc),
        })
        return 1
    finally:
        release_pid_file()


if __name__ == '__main__':
    sys.exit(main())
