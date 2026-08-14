#!/usr/bin/env python3
"""Docker-compatible health check for the collector state file."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys

from config import COLLECT_INTERVAL, HEALTH_FILE, NPU_SMI_TIMEOUT
from stats_core import parse_timestamp


CST = timezone(timedelta(hours=8))


def check_health(path=HEALTH_FILE, max_age=None, now=None):
    max_age = max_age or max(COLLECT_INTERVAL * 2, NPU_SMI_TIMEOUT * 2)
    now = now or datetime.now(CST)
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            health = json.load(handle)
    except (OSError, ValueError) as exc:
        return False, f'unreadable health file: {exc}'

    status = health.get('status')
    if status not in ('healthy', 'degraded'):
        return False, f'collector status is {status or "unknown"}'
    last_attempt = health.get('last_attempt')
    if not last_attempt:
        return False, 'last_attempt is missing'
    try:
        attempted_at = parse_timestamp(last_attempt)
    except ValueError as exc:
        return False, f'invalid last_attempt: {exc}'
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=CST)
    age = (now - attempted_at.astimezone(CST)).total_seconds()
    if age < -COLLECT_INTERVAL:
        return False, f'clock moved backwards by {-age:.0f} seconds'
    if age > max_age:
        return False, f'last attempt is stale ({age:.0f} seconds old)'
    return True, f'{status}, last attempt {max(0, age):.0f} seconds ago'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check NPU monitor health')
    parser.add_argument('--file', default=HEALTH_FILE, help='health JSON file')
    parser.add_argument('--max-age', type=int, help='maximum age in seconds')
    args = parser.parse_args(argv)
    healthy, message = check_health(args.file, args.max_age)
    print(message)
    return 0 if healthy else 1


if __name__ == '__main__':
    sys.exit(main())
