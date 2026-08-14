#!/usr/bin/env python3
from datetime import datetime, timezone
import json
import sys

from cluster_common.protocol import parse_timestamp
from . import config


def main():
    try:
        with open(config.HEALTH_FILE, 'r', encoding='utf-8') as handle:
            health = json.load(handle)
        last_attempt = parse_timestamp(health['last_attempt'])
        age = (datetime.now(timezone.utc) - last_attempt.astimezone(timezone.utc)).total_seconds()
        if age > config.COLLECT_INTERVAL * 3:
            raise ValueError('last collection is stale ({:.0f}s)'.format(age))
        if health.get('status') == 'unhealthy':
            raise ValueError('agent collection status is unhealthy')
        upload_note = 'upload state unavailable'
        try:
            with open(config.UPLOAD_HEALTH_FILE, 'r', encoding='utf-8') as handle:
                upload = json.load(handle)
            upload_note = 'pending={}, oldest={}s, last upload={}, unavailable since={}'.format(
                upload.get('pending_samples', '?'),
                upload.get('oldest_pending_age_seconds'),
                upload.get('last_success') or 'never',
                upload.get('upload_unavailable_since') or '-',
            )
        except (OSError, ValueError):
            pass
        print('{}; last collection {:.0f}s ago; {}'.format(
            health.get('status'), max(age, 0), upload_note
        ))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print('unhealthy: {}'.format(exc))
        return 1


if __name__ == '__main__':
    sys.exit(main())
