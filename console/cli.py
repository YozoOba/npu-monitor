#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys

from . import config
from .client import CollectorClient
from .export import create_export, resolve_range


def format_number(value):
    return '-' if value is None else '{:.2f}'.format(value)


def print_summary(snapshot):
    counts = snapshot['node_counts']
    registered = snapshot.get('registered_expected_cards', snapshot.get('expected_cards', 0))
    fresh = snapshot.get('fresh_collected_cards', snapshot.get('active_collected_cards', 0))
    last_known = snapshot.get('last_known_collected_cards', fresh)
    print('NPU Cluster Summary')
    print('=' * 100)
    print('Cluster filter: {}'.format(snapshot.get('cluster_id') or 'all'))
    print(
        'Nodes: {total}  online={online} degraded={degraded} stale={stale} offline={offline}'.format(
            total=snapshot['total_nodes'], **counts
        )
    )
    print('Registered capacity: {} cards  Last known: {} cards'.format(registered, last_known))
    print('Fresh samples: {}/{}  fleet freshness={}%  reporting completeness={}%'.format(
        fresh, registered,
        format_number(snapshot.get('fleet_freshness_coverage_percent')),
        format_number(snapshot.get('reporting_sample_coverage_percent')),
    ))
    print('Current utilization={}%  HBM={}%  busy={} idle={}  active alerts={}'.format(
        format_number(snapshot['utilization_avg']), format_number(snapshot['hbm_percent']),
        snapshot.get('busy_cards', 0), snapshot.get('idle_cards', 0),
        snapshot.get('active_alert_count', '-'),
    ))
    print('Generated: {}'.format(snapshot['generated_at']))


def print_history(result):
    print('{:<26} {:>10} {:>10} {:>10} {:>10} {:>10} {:>10}'.format(
        'TIMESTAMP', 'AVG %', 'MIN %', 'MAX %', 'HBM %', 'CARDS', 'COVER %'
    ))
    for point in result['points']:
        print('{:<26} {:>10} {:>10} {:>10} {:>10} {:>10} {:>10}'.format(
            point['timestamp'], format_number(point['utilization_avg']),
            format_number(point['utilization_min']), format_number(point['utilization_max']),
            format_number(point['hbm_percent']), point['card_samples'],
            format_number(point.get('coverage_percent')),
        ))


def time_range(args, max_days=180):
    if args.start or args.end:
        start, end = resolve_range(args.start, args.end, args.hours, max_days)
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=args.hours)
    return start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds')


def add_range_arguments(parser):
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--start', help='ISO timestamp or YYYY-MM-DD')
    parser.add_argument('--end', help='ISO timestamp or inclusive YYYY-MM-DD')


def main(argv=None):
    parser = argparse.ArgumentParser(description='NPU cluster query console')
    parser.add_argument('--collector-url', default=config.COLLECTOR_URL)
    subparsers = parser.add_subparsers(dest='command')

    summary = subparsers.add_parser('summary')
    summary.add_argument('--cluster')
    subparsers.add_parser('clusters')

    nodes = subparsers.add_parser('nodes')
    nodes.add_argument('--state', choices=['online', 'degraded', 'stale', 'offline'])
    nodes.add_argument('--cluster')
    nodes.add_argument('--search')
    nodes.add_argument('--page', type=int, default=1)
    nodes.add_argument('--page-size', type=int, default=50)

    history = subparsers.add_parser('history')
    add_range_arguments(history)
    history.add_argument('--bucket', type=int, default=300)
    history.add_argument('--node')
    history.add_argument('--cluster')
    history.add_argument('--card', type=int)

    samples = subparsers.add_parser('samples')
    add_range_arguments(samples)
    samples.add_argument('--node')
    samples.add_argument('--cluster')
    samples.add_argument('--status', choices=['complete', 'partial', 'failed'])
    samples.add_argument('--search')
    samples.add_argument('--page', type=int, default=1)
    samples.add_argument('--page-size', type=int, default=100)

    alerts = subparsers.add_parser('alerts')
    add_range_arguments(alerts)
    alerts.add_argument('--node')
    alerts.add_argument('--cluster')
    alerts.add_argument('--severity', choices=['warning', 'critical'])
    alerts.add_argument('--status', choices=['active', 'resolved'])
    alerts.add_argument('--type')
    alerts.add_argument('--page', type=int, default=1)
    alerts.add_argument('--page-size', type=int, default=100)

    export = subparsers.add_parser('export')
    add_range_arguments(export)
    export.add_argument('--format', choices=['csv', 'xlsx'], default='xlsx')
    export.add_argument('--output', required=True)
    export.add_argument('--node')
    export.add_argument('--cluster')
    export.add_argument('--card', type=int)
    export.add_argument('--status', choices=['complete', 'partial', 'failed'])

    args = parser.parse_args(argv)
    command = args.command or 'summary'
    client = CollectorClient(args.collector_url, config.HTTP_TIMEOUT)
    try:
        if command == 'summary':
            print_summary(client.snapshot(getattr(args, 'cluster', None)))
        elif command == 'clusters':
            print(json.dumps(client.clusters()['items'], ensure_ascii=False, indent=2))
        elif command == 'nodes':
            print(json.dumps(client.nodes(
                args.page, args.page_size, args.state, args.cluster, args.search
            ), ensure_ascii=False, indent=2, sort_keys=True))
        elif command == 'history':
            start, end = time_range(args)
            print_history(client.series(
                start, end, args.bucket, args.node, args.cluster, args.card
            ))
        elif command == 'samples':
            start, end = time_range(args)
            print(json.dumps(client.samples(
                start, end, args.page, args.page_size, args.node, args.cluster,
                args.status, args.search,
            ), ensure_ascii=False, indent=2, sort_keys=True))
        elif command == 'alerts':
            start, end = time_range(args)
            print(json.dumps(client.alerts(
                start, end, args.page, args.page_size, args.node, args.cluster,
                args.severity, args.status, args.type,
            ), ensure_ascii=False, indent=2, sort_keys=True))
        elif command == 'export':
            start, end = time_range(args, config.MAX_EXPORT_DAYS)
            output = os.path.abspath(args.output)
            output_dir = os.path.dirname(output)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            count = create_export(
                client, output, args.format, start, end, args.node,
                args.cluster, args.card, args.status,
            )
            print('exported {} rows to {}'.format(count, output))
        return 0
    except Exception as exc:
        print('query failed: {}'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
