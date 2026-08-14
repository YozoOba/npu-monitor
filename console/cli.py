#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta, timezone
import json
import sys

from . import config
from .client import CollectorClient


def format_number(value):
    return '-' if value is None else '{:.2f}'.format(value)


def print_summary(snapshot):
    counts = snapshot['node_counts']
    registered = snapshot.get(
        'registered_expected_cards', snapshot.get('expected_cards', 0)
    )
    fresh = snapshot.get(
        'fresh_collected_cards', snapshot.get('active_collected_cards', 0)
    )
    fresh_expected = snapshot.get('fresh_expected_cards', registered)
    last_known = snapshot.get('last_known_collected_cards', fresh)
    fleet_freshness = snapshot.get(
        'fleet_freshness_coverage_percent', snapshot.get('coverage_percent', 0.0)
    )
    reporting_coverage = snapshot.get('reporting_sample_coverage_percent')
    print('NPU Cluster Summary')
    print('=' * 92)
    print(
        'Nodes: {total}  online={online} degraded={degraded} stale={stale} offline={offline}'.format(
            total=snapshot['total_nodes'], **counts
        )
    )
    print('Registered capacity: {} cards  Last known: {} cards'.format(
        registered, last_known
    ))
    print(
        'Fresh samples: {}/{}  fleet freshness={}%%  reporting completeness={}%%'.format(
            fresh, registered, format_number(fleet_freshness),
            format_number(reporting_coverage),
        )
    )
    print(
        'Current utilization={}%%  HBM={}%%  busy={} idle={}  reporting expected={}'.format(
            format_number(snapshot['utilization_avg']),
            format_number(snapshot['hbm_percent']),
            snapshot.get('busy_cards', 0), snapshot.get('idle_cards', 0),
            fresh_expected,
        )
    )
    print('Generated: {}'.format(snapshot['generated_at']))
    print('-' * 92)
    print('{:<22} {:<11} {:>8} {:>10} {:>10} {:>9}  {}'.format(
        'NODE', 'STATE', 'LAST CARDS', 'FRESH %', 'HBM %', 'AGE(s)', 'LAST SAMPLE'
    ))
    for node in snapshot['nodes']:
        fresh_utilization = node.get('fresh_utilization_avg')
        if 'fresh_utilization_avg' not in node and node['state'] in ('online', 'degraded'):
            fresh_utilization = node.get('utilization_avg')
        fresh_hbm = node.get('fresh_hbm_percent')
        if 'fresh_hbm_percent' not in node and node['state'] in ('online', 'degraded'):
            fresh_hbm = node.get('hbm_percent')
        print('{:<22} {:<11} {:>8} {:>10} {:>10} {:>9}  {}'.format(
            node['node_id'][:22], node['state'],
            '{}/{}'.format(node['collected_cards'], node['expected_cards']),
            format_number(fresh_utilization), format_number(fresh_hbm),
            node['age_seconds'], node['last_collected_at'] or '-',
        ))


def print_nodes(snapshot, state=None):
    nodes = snapshot['nodes']
    if state:
        nodes = [node for node in nodes if node['state'] == state]
    print(json.dumps(nodes, ensure_ascii=False, indent=2, sort_keys=True))


def print_history(client, hours, bucket, node_id):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    result = client.series(
        start.isoformat(timespec='seconds'), end.isoformat(timespec='seconds'),
        bucket, node_id,
    )
    print('{:<26} {:>10} {:>10} {:>10} {:>10} {:>10}'.format(
        'TIMESTAMP', 'AVG %', 'MIN %', 'MAX %', 'HBM %', 'SAMPLES'
    ))
    for point in result['points']:
        print('{:<26} {:>10} {:>10} {:>10} {:>10} {:>10}'.format(
            point['timestamp'], format_number(point['utilization_avg']),
            format_number(point['utilization_min']),
            format_number(point['utilization_max']),
            format_number(point['hbm_percent']), point['card_samples'],
        ))


def main(argv=None):
    parser = argparse.ArgumentParser(description='NPU cluster query console')
    parser.add_argument('--collector-url', default=config.COLLECTOR_URL)
    subparsers = parser.add_subparsers(dest='command')
    subparsers.add_parser('summary')
    nodes = subparsers.add_parser('nodes')
    nodes.add_argument('--state', choices=['online', 'degraded', 'stale', 'offline'])
    history = subparsers.add_parser('history')
    history.add_argument('--hours', type=int, default=24)
    history.add_argument('--bucket', type=int, default=300)
    history.add_argument('--node')
    args = parser.parse_args(argv)
    command = args.command or 'summary'
    client = CollectorClient(args.collector_url, config.HTTP_TIMEOUT)
    try:
        if command == 'summary':
            print_summary(client.snapshot())
        elif command == 'nodes':
            print_nodes(client.snapshot(), args.state)
        else:
            print_history(client, args.hours, args.bucket, args.node)
        return 0
    except Exception as exc:
        print('query failed: {}'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
