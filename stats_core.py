"""Shared CSV reading and statistics for all query frontends."""
import csv
from datetime import datetime
import json
import os
import re
import sys

from config import DAILY_DIR, SAMPLE_STATUS_DIR


def parse_timestamp(value):
    value = value.strip()
    try:
        return datetime.fromisoformat(value)
    except AttributeError:
        # Python 3.6 compatibility.
        pass
    except ValueError:
        pass

    timezone_match = re.fullmatch(
        r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}):(\d{2})',
        value,
    )
    if timezone_match:
        return datetime.strptime(
            ''.join(timezone_match.groups()), '%Y-%m-%dT%H:%M:%S%z'
        )

    normalized = value.replace('T', ' ')
    timezone_index = max(normalized.rfind('+'), normalized.rfind('-'))
    if timezone_index > 10:
        normalized = normalized[:timezone_index]
    normalized = normalized.split('.')[0]
    return datetime.strptime(normalized, '%Y-%m-%d %H:%M:%S')


def read_daily_data(date_str, daily_dir=DAILY_DIR, warning_stream=None):
    warning_stream = warning_stream or sys.stderr
    csv_file = os.path.join(daily_dir, f'stats_{date_str}.csv')
    if not os.path.exists(csv_file):
        return None

    # Last observation wins for duplicate (timestamp, card_id) records. This
    # protects statistics after a restart or clock correction repeats a slot.
    records = {}
    try:
        with open(csv_file, 'r', encoding='utf-8', newline='') as handle:
            reader = csv.DictReader(handle)
            for line_number, row in enumerate(reader, start=2):
                try:
                    timestamp = row['timestamp'].strip()
                    parsed_timestamp = parse_timestamp(timestamp)
                    card_id = int(row['card_id'])
                    utilization = float(row['utilization'])
                    if not 0.0 <= utilization <= 100.0:
                        raise ValueError('utilization outside 0..100')
                    normalized_timestamp = parsed_timestamp.replace(
                        tzinfo=None
                    ).isoformat(timespec='seconds')
                    records[(normalized_timestamp, card_id)] = {
                        'timestamp': timestamp,
                        'card_id': card_id,
                        'utilization': utilization,
                    }
                except (KeyError, TypeError, ValueError) as exc:
                    print(
                        f'Warning: skipped invalid row {line_number} in '
                        f'{csv_file}: {exc}', file=warning_stream
                    )
    except OSError as exc:
        print(f'Error reading {csv_file}: {exc}', file=warning_stream)
        return None
    return list(records.values())


def calculate_stats(data):
    if not data:
        return None
    samples_by_card = {}
    for row in data:
        samples_by_card.setdefault(row['card_id'], []).append(row['utilization'])
    return {
        card_id: {
            'count': len(samples),
            'avg': sum(samples) / len(samples),
            'max': max(samples),
            'min': min(samples),
        }
        for card_id, samples in samples_by_card.items()
    }


def calculate_overall(stats):
    if not stats:
        return 0, 0.0
    total_samples = sum(stat['count'] for stat in stats.values())
    weighted_sum = sum(
        stat['avg'] * stat['count'] for stat in stats.values()
    )
    return total_samples, weighted_sum / total_samples if total_samples else 0.0


def read_sample_status(date_str, status_dir=SAMPLE_STATUS_DIR,
                       warning_stream=None):
    warning_stream = warning_stream or sys.stderr
    path = os.path.join(status_dir, f'samples_{date_str}.jsonl')
    if not os.path.exists(path):
        return []
    records = {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    record = json.loads(line)
                    timestamp = record['timestamp']
                    parse_timestamp(timestamp)
                    records[timestamp] = record
                except (KeyError, TypeError, ValueError) as exc:
                    print(
                        f'Warning: skipped invalid sample status line '
                        f'{line_number} in {path}: {exc}', file=warning_stream
                    )
    except OSError as exc:
        print(f'Error reading {path}: {exc}', file=warning_stream)
        return []
    return list(records.values())


def calculate_coverage(records):
    if not records:
        return None
    attempts = len(records)
    expected = sum(int(record.get('expected_cards', 0)) for record in records)
    collected = sum(int(record.get('collected_cards', 0)) for record in records)
    return {
        'attempts': attempts,
        'complete': sum(record.get('status') == 'complete' for record in records),
        'partial': sum(record.get('status') == 'partial' for record in records),
        'failed': sum(record.get('status') == 'failed' for record in records),
        'expected_card_samples': expected,
        'collected_card_samples': collected,
        'coverage_percent': collected * 100.0 / expected if expected else 0.0,
    }


def format_coverage(coverage):
    if not coverage:
        return 'Coverage: unavailable (legacy data has no sample-status records)'
    return (
        'Coverage: {coverage_percent:.2f}% '
        '({collected_card_samples}/{expected_card_samples} card samples; '
        '{complete} complete, {partial} partial, {failed} failed attempts)'
    ).format(**coverage)
