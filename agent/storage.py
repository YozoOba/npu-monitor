import csv
from datetime import datetime, timedelta, timezone
import io
import json
import os
import re

from cluster_common.atomic import write_bytes_atomic, write_json_atomic
from cluster_common.protocol import parse_timestamp


CSV_FIELDS = ['timestamp', 'card_id', 'utilization', 'hbm_used_mb', 'hbm_total_mb']


class QueueFullError(RuntimeError):
    pass


def _append_fsync(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError('short write: {}/{} bytes'.format(written, len(payload)))
        os.fsync(fd)
    finally:
        os.close(fd)


def save_local_sample(sample, daily_dir, status_dir):
    timestamp = sample['collected_at']
    day = timestamp[:10]
    if sample['cards']:
        path = os.path.join(daily_dir, 'stats_{}.csv'.format(day))
        include_header = not os.path.exists(path) or os.path.getsize(path) == 0
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator='\n')
        if include_header:
            writer.writeheader()
        for card in sample['cards']:
            writer.writerow({
                'timestamp': timestamp,
                'card_id': card['card_id'],
                'utilization': card['utilization'],
                'hbm_used_mb': '' if card['hbm_used_mb'] is None else card['hbm_used_mb'],
                'hbm_total_mb': '' if card['hbm_total_mb'] is None else card['hbm_total_mb'],
            })
        _append_fsync(path, buffer.getvalue().encode('utf-8'))
    status_path = os.path.join(status_dir, 'samples_{}.jsonl'.format(day))
    status = {key: sample[key] for key in (
        'sample_id', 'node_id', 'collected_at', 'status', 'expected_cards',
        'collected_cards', 'received_card_ids', 'missing_card_ids',
        'coverage_percent',
    )}
    _append_fsync(
        status_path,
        (json.dumps(status, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'),
    )


def queue_usage(spool_dir):
    files = []
    total = 0
    try:
        names = os.listdir(spool_dir)
    except OSError:
        return files, total
    for name in names:
        if not name.endswith('.json'):
            continue
        path = os.path.join(spool_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        files.append(path)
        total += size
    files.sort()
    return files, total


def enqueue(sample, spool_dir, max_files, max_bytes):
    files, total = queue_usage(spool_dir)
    payload = (json.dumps(sample, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    if len(files) >= max_files or total + len(payload) > max_bytes:
        raise QueueFullError(
            'upload queue is full ({} files, {} bytes)'.format(len(files), total)
        )
    collected = parse_timestamp(sample['collected_at']).astimezone(timezone.utc)
    epoch = int(collected.timestamp())
    filename = '{:012d}_{}.json'.format(epoch, sample['sample_id'])
    path = os.path.join(spool_dir, filename)
    if not os.path.exists(path):
        write_bytes_atomic(path, payload)
    return path


def load_queued(path):
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def reject_queued(path, rejected_dir, reason):
    os.makedirs(rejected_dir, exist_ok=True)
    base = os.path.basename(path)
    target = os.path.join(rejected_dir, base)
    try:
        os.replace(path, target)
    except OSError:
        return
    write_json_atomic(target + '.reason.json', {'reason': reason})


def expire_queued(spool_dir, rejected_dir, retention_days, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = int((now - timedelta(days=retention_days)).timestamp())
    expired = 0
    files, _size = queue_usage(spool_dir)
    for path in files:
        match = re.match(r'^(\d+)_', os.path.basename(path))
        if not match or int(match.group(1)) >= cutoff:
            continue
        reject_queued(
            path, rejected_dir,
            'upload queue item exceeded {} day retention'.format(retention_days),
        )
        expired += 1
    return expired


def clean_rejected(rejected_dir, retention_days, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - retention_days * 86400
    deleted = 0
    try:
        names = os.listdir(rejected_dir)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(rejected_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                deleted += 1
        except OSError:
            pass
    return deleted


def clean_old_local_data(directories, retention_days, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now.date() - timedelta(days=retention_days)
    deleted = 0
    pattern = re.compile(r'^(?:stats|samples)_(\d{4}-\d{2}-\d{2})\.(?:csv|jsonl)$')
    for directory in directories:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            match = pattern.fullmatch(name)
            if not match:
                continue
            try:
                file_day = datetime.strptime(match.group(1), '%Y-%m-%d').date()
            except ValueError:
                continue
            if file_day < cutoff:
                try:
                    os.remove(os.path.join(directory, name))
                    deleted += 1
                except OSError:
                    pass
    return deleted
