#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta, timezone
import logging
import os
import signal
import sys
import threading
import time
import zipfile

from cluster_common import PROTOCOL_VERSION
from cluster_common.atomic import write_json_atomic
from cluster_common.protocol import normalize_sample
from cluster_common.storage_health import check_capacity
from . import __version__
from . import config
from .sampler import collect
from .sender import UploadWorker
from .monthly_xlsx import (
    MonthlyWorkbookError, archive_old_monthly_workbooks,
    update_monthly_workbooks,
)
from .storage import (
    archive_old_local_data, archive_rejected, enqueue, expire_queued, queue_usage,
    save_local_sample,
)


LOGGER = logging.getLogger('npu_agent')
STOP_EVENT = threading.Event()


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )


def handle_signal(signum, _frame):
    LOGGER.info('received signal %s, stopping', signum)
    STOP_EVENT.set()


def resolve_expected_cards(cards, minimum):
    """Expand the configured minimum for products with more logical dies."""
    if not cards:
        return minimum
    highest_id = max(card['card_id'] for card in cards)
    return max(minimum, len(cards), highest_id + 1)


def build_sample(cards, collected_at, expected_cards=None):
    expected_cards = resolve_expected_cards(
        cards,
        config.EXPECTED_CARDS if expected_cards is None else expected_cards,
    )
    return normalize_sample({
        'protocol_version': PROTOCOL_VERSION,
        'node_id': config.NODE_ID,
        'node_name': config.NODE_NAME,
        'cluster_id': config.CLUSTER_ID,
        'collected_at': collected_at.isoformat(timespec='seconds'),
        'collect_interval': config.COLLECT_INTERVAL,
        'expected_cards': expected_cards,
        'cards': cards,
    }, received_at=collected_at)


def write_agent_health(sample, collection_error=None, local_error=None):
    files, size = queue_usage(config.SPOOL_DIR)
    status = 'healthy'
    if sample['status'] == 'partial':
        status = 'degraded'
    if sample['status'] == 'failed' or local_error:
        status = 'unhealthy'
    write_json_atomic(config.HEALTH_FILE, {
        'status': status,
        'agent_version': __version__,
        'node_id': config.NODE_ID,
        'cluster_id': config.CLUSTER_ID,
        'local_timezone': config.LOCAL_TIMEZONE_NAME,
        'last_attempt': sample['collected_at'],
        'sample_status': sample['status'],
        'expected_cards': sample['expected_cards'],
        'collected_cards': sample['collected_cards'],
        'missing_card_ids': sample['missing_card_ids'],
        'coverage_percent': sample['coverage_percent'],
        'pending_samples': len(files),
        'pending_bytes': size,
        'collection_error': collection_error,
        'local_error': local_error,
    })


def seconds_to_next_interval(interval):
    now = time.time()
    return max(0.0, (int(now) // interval + 1) * interval - now)


def run(once=False):
    config.validate()
    worker = UploadWorker(
        config.COLLECTOR_URL, config.SPOOL_DIR, config.REJECTED_DIR,
        config.UPLOAD_HEALTH_FILE, config.HTTP_TIMEOUT, config.UPLOAD_BATCH_SIZE,
    )
    worker.start()
    last_cleanup_day = None
    expected_cards = config.EXPECTED_CARDS
    exit_code = 0
    try:
        while not STOP_EVENT.is_set():
            collected_at = datetime.now(timezone.utc)
            local_collected_at = collected_at.astimezone(config.LOCAL_TIMEZONE)
            if local_collected_at.date() != last_cleanup_day:
                if config.MONTHLY_XLSX_ENABLED:
                    try:
                        through_date = local_collected_at.date() - timedelta(days=1)
                        updated_workbooks = update_monthly_workbooks(
                            config.DAILY_DIR, config.MONTHLY_DIR, through_date,
                            {
                                'node_id': config.NODE_ID,
                                'node_name': config.NODE_NAME,
                                'cluster_id': config.CLUSTER_ID,
                            },
                            config.LOCAL_TIMEZONE,
                        )
                        for workbook_path in updated_workbooks:
                            LOGGER.info('updated monthly workbook %s', workbook_path)
                    except (OSError, MonthlyWorkbookError, zipfile.BadZipFile):
                        LOGGER.exception('cannot update monthly XLSX workbooks')
                archived_local = archive_old_local_data(
                    (config.DAILY_DIR, config.STATUS_DIR), config.ARCHIVE_DIR,
                    config.RETENTION_DAYS,
                    now=local_collected_at,
                )
                archived_workbooks = archive_old_monthly_workbooks(
                    config.MONTHLY_DIR, config.ARCHIVE_DIR,
                    config.RETENTION_DAYS,
                    local_collected_at.date(),
                )
                expired = expire_queued(
                    config.SPOOL_DIR, config.REJECTED_DIR,
                    config.SPOOL_RETENTION_DAYS, now=collected_at,
                )
                archived_rejected = archive_rejected(
                    config.REJECTED_DIR, config.ARCHIVE_DIR,
                    config.RETENTION_DAYS, now=collected_at
                )
                if archived_local or archived_workbooks or archived_rejected:
                    LOGGER.info(
                        'archived %s local files, %s monthly workbooks and '
                        '%s rejected files',
                        archived_local, archived_workbooks, archived_rejected,
                    )
                if expired:
                    LOGGER.error('%s queued samples expired before upload', expired)
                last_cleanup_day = local_collected_at.date()
            cards, collection_error = collect(config.NPU_SMI_BIN, config.COMMAND_TIMEOUT)
            detected_expected = resolve_expected_cards(cards, expected_cards)
            if detected_expected > expected_cards:
                LOGGER.info(
                    'expanded expected logical NPU count from %s to %s',
                    expected_cards, detected_expected,
                )
                expected_cards = detected_expected
            sample = build_sample(cards, collected_at, expected_cards)
            local_error = None
            try:
                capacity_ok, capacity = check_capacity(
                    config.DATA_DIR, config.MIN_FREE_BYTES, config.MIN_FREE_INODES
                )
                if not capacity_ok:
                    raise OSError('local storage unavailable: {}'.format(capacity))
                save_local_sample(
                    sample, config.DAILY_DIR, config.STATUS_DIR,
                    config.LOCAL_TIMEZONE,
                )
                enqueue(
                    sample, config.SPOOL_DIR, config.SPOOL_MAX_FILES,
                    config.SPOOL_MAX_BYTES,
                )
                worker.notify()
            except (OSError, RuntimeError, ValueError) as exc:
                local_error = str(exc)
                LOGGER.exception('cannot persist sample')
            write_agent_health(sample, collection_error, local_error)
            LOGGER.info(
                'sample %s: %s/%s cards, status=%s, queued=%s',
                sample['sample_id'][:12], sample['collected_cards'],
                sample['expected_cards'], sample['status'], local_error is None,
            )
            if sample['status'] == 'failed' or local_error:
                exit_code = 1
            if once:
                worker.notify()
                deadline = time.time() + config.HTTP_TIMEOUT + 2
                while time.time() < deadline:
                    files, _size = queue_usage(config.SPOOL_DIR)
                    if not files:
                        break
                    time.sleep(0.05)
                return exit_code
            STOP_EVENT.wait(seconds_to_next_interval(config.COLLECT_INTERVAL))
    finally:
        worker.stop()
        worker.join(timeout=config.HTTP_TIMEOUT + 2)
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description='NPU cluster node agent')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    configure_logging()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    LOGGER.info('local file timezone is %s', config.LOCAL_TIMEZONE_NAME)
    try:
        return run(args.once)
    except Exception as exc:
        LOGGER.exception('fatal agent error: %s', exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
