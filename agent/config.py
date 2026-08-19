import os
import socket


def env_int(name, default, minimum=0):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError('{} must be an integer, got {!r}'.format(name, raw))
    if value < minimum:
        raise ValueError('{} must be >= {}'.format(name, minimum))
    return value


def env_bool(name, default=True):
    raw = os.environ.get(name, '1' if default else '0').strip().lower()
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    if raw in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError('{} must be a boolean, got {!r}'.format(name, raw))


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get('NPU_AGENT_DATA_DIR', '/app/data'))
DAILY_DIR = os.path.join(DATA_DIR, 'daily')
STATUS_DIR = os.path.join(DATA_DIR, 'sample_status')
MONTHLY_DIR = os.path.join(DATA_DIR, 'monthly')
SPOOL_DIR = os.path.join(DATA_DIR, 'spool')
REJECTED_DIR = os.path.join(DATA_DIR, 'rejected')
HEALTH_FILE = os.path.join(DATA_DIR, 'health.json')
UPLOAD_HEALTH_FILE = os.path.join(DATA_DIR, 'upload_health.json')

NODE_ID = os.environ.get('NPU_AGENT_NODE_ID', '').strip()
NODE_NAME = os.environ.get('NPU_AGENT_NODE_NAME', socket.gethostname()).strip()
COLLECTOR_URL = os.environ.get('NPU_AGENT_COLLECTOR_URL', '').rstrip('/')
COLLECT_INTERVAL = env_int('NPU_AGENT_COLLECT_INTERVAL', 60, 1)
EXPECTED_CARDS = env_int('NPU_AGENT_EXPECTED_CARDS', 8, 1)
COMMAND_TIMEOUT = env_int('NPU_AGENT_COMMAND_TIMEOUT', 10, 1)
HTTP_TIMEOUT = env_int('NPU_AGENT_HTTP_TIMEOUT', 5, 1)
RETENTION_DAYS = env_int('NPU_AGENT_RETENTION_DAYS', 180, 1)
SPOOL_RETENTION_DAYS = env_int('NPU_AGENT_SPOOL_RETENTION_DAYS', 7, 1)
SPOOL_MAX_FILES = env_int('NPU_AGENT_SPOOL_MAX_FILES', 20000, 1)
SPOOL_MAX_BYTES = env_int('NPU_AGENT_SPOOL_MAX_BYTES', 512 * 1024 * 1024, 1024)
UPLOAD_BATCH_SIZE = env_int('NPU_AGENT_UPLOAD_BATCH_SIZE', 100, 1)
MIN_FREE_BYTES = env_int('NPU_AGENT_MIN_FREE_BYTES', 100 * 1024 * 1024, 0)
MIN_FREE_INODES = env_int('NPU_AGENT_MIN_FREE_INODES', 1000, 0)
NPU_SMI_BIN = os.environ.get('NPU_AGENT_NPU_SMI_BIN', 'npu-smi')
MONTHLY_XLSX_ENABLED = env_bool('NPU_AGENT_MONTHLY_XLSX_ENABLED', True)


def validate():
    if not NODE_ID:
        raise ValueError('NPU_AGENT_NODE_ID is required and must be stable')
    if not COLLECTOR_URL.startswith(('http://', 'https://')):
        raise ValueError('NPU_AGENT_COLLECTOR_URL must be an http(s) URL')
    for directory in (
            DATA_DIR, DAILY_DIR, STATUS_DIR, MONTHLY_DIR, SPOOL_DIR, REJECTED_DIR):
        os.makedirs(directory, exist_ok=True)
