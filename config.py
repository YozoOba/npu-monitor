import os


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DAILY_DIR = os.path.join(DATA_DIR, 'daily')
LOG_DIR = os.path.join(DATA_DIR, 'logs')
SAMPLE_STATUS_DIR = os.path.join(DATA_DIR, 'sample_status')
ARCHIVE_DIR = os.path.abspath(os.environ.get(
    'NPU_MONITOR_ARCHIVE_DIR', os.path.join(DATA_DIR, 'archive')
))
HEALTH_FILE = os.path.join(DATA_DIR, 'health.json')


def _env_int(name, default, minimum=1):
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer, got {raw_value!r}') from exc
    if value < minimum:
        raise ValueError(f'{name} must be >= {minimum}, got {value}')
    return value


# Environment variables let the same image be configured without changing it.
COLLECT_INTERVAL = _env_int('NPU_MONITOR_COLLECT_INTERVAL', 60)
DATA_RETENTION_DAYS = _env_int('NPU_MONITOR_RETENTION_DAYS', 180)
EXPECTED_NPU_COUNT = _env_int('NPU_MONITOR_EXPECTED_NPU_COUNT', 8)
NPU_SMI_TIMEOUT = _env_int('NPU_MONITOR_COMMAND_TIMEOUT', 10)
LOG_MAX_BYTES = _env_int('NPU_MONITOR_LOG_MAX_BYTES', 10 * 1024 * 1024)
LOG_BACKUP_COUNT = _env_int('NPU_MONITOR_LOG_BACKUP_COUNT', 5)
MIN_FREE_BYTES = _env_int('NPU_MONITOR_MIN_FREE_BYTES', 100 * 1024 * 1024, minimum=0)
MIN_FREE_INODES = _env_int('NPU_MONITOR_MIN_FREE_INODES', 1000, minimum=0)

for directory in [DATA_DIR, DAILY_DIR, LOG_DIR, SAMPLE_STATUS_DIR, ARCHIVE_DIR]:
    os.makedirs(directory, exist_ok=True)
