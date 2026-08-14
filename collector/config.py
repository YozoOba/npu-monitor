import os


def env_int(name, default, minimum=0):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError('{} must be an integer, got {!r}'.format(name, raw))
    if value < minimum:
        raise ValueError('{} must be >= {}'.format(name, minimum))
    return value


DATA_DIR = os.path.abspath(os.environ.get('NPU_COLLECTOR_DATA_DIR', '/app/data'))
DATABASE_PATH = os.path.join(DATA_DIR, 'cluster.sqlite3')
SNAPSHOT_PATH = os.path.join(DATA_DIR, 'latest_snapshot.json')
HOST = os.environ.get('NPU_COLLECTOR_HOST', '0.0.0.0')
PORT = env_int('NPU_COLLECTOR_PORT', 18080, 1)
RETENTION_DAYS = env_int('NPU_COLLECTOR_RETENTION_DAYS', 180, 1)
STALE_FLOOR_SECONDS = env_int('NPU_COLLECTOR_STALE_SECONDS', 120, 1)
OFFLINE_FLOOR_SECONDS = env_int('NPU_COLLECTOR_OFFLINE_SECONDS', 300, 1)
MAX_FUTURE_SECONDS = env_int('NPU_COLLECTOR_MAX_FUTURE_SECONDS', 300, 0)
MAX_BODY_BYTES = env_int('NPU_COLLECTOR_MAX_BODY_BYTES', 1024 * 1024, 1024)
SNAPSHOT_INTERVAL = env_int('NPU_COLLECTOR_SNAPSHOT_INTERVAL', 10, 1)
CLOCK_SKEW_WARN_SECONDS = env_int('NPU_COLLECTOR_CLOCK_SKEW_WARN_SECONDS', 30, 0)
BUSY_UTILIZATION = env_int('NPU_COLLECTOR_BUSY_UTILIZATION', 80, 0)
IDLE_UTILIZATION = env_int('NPU_COLLECTOR_IDLE_UTILIZATION', 10, 0)
MIN_FREE_BYTES = env_int('NPU_COLLECTOR_MIN_FREE_BYTES', 200 * 1024 * 1024, 0)
MIN_FREE_INODES = env_int('NPU_COLLECTOR_MIN_FREE_INODES', 1000, 0)


def validate():
    if PORT > 65535:
        raise ValueError('NPU_COLLECTOR_PORT must be <= 65535')
    if OFFLINE_FLOOR_SECONDS <= STALE_FLOOR_SECONDS:
        raise ValueError('offline threshold must be greater than stale threshold')
    if BUSY_UTILIZATION > 100 or IDLE_UTILIZATION > 100:
        raise ValueError('utilization thresholds must be <= 100')
    os.makedirs(DATA_DIR, exist_ok=True)
