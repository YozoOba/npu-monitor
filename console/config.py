import os

from cluster_common.timezones import (
    resolve_timezone, timezone_label, timezone_offset_seconds,
)


def env_int(name, default, minimum=1):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError('{} must be an integer'.format(name))
    if value < minimum:
        raise ValueError('{} must be >= {}'.format(name, minimum))
    return value


COLLECTOR_URL = os.environ.get(
    'NPU_CONSOLE_COLLECTOR_URL', 'http://127.0.0.1:18080'
).rstrip('/')
HOST = os.environ.get('NPU_CONSOLE_HOST', '0.0.0.0')
PORT = env_int('NPU_CONSOLE_PORT', 18081)
HTTP_TIMEOUT = env_int('NPU_CONSOLE_HTTP_TIMEOUT', 10)
MAX_EXPORT_DAYS = env_int('NPU_CONSOLE_MAX_EXPORT_DAYS', 31)
REPORT_TIMEOUT = env_int('NPU_CONSOLE_REPORT_TIMEOUT', 120)
MAX_REPORT_DAYS = env_int('NPU_CONSOLE_MAX_REPORT_DAYS', 180)
REPORT_TIMEZONE = resolve_timezone(os.environ.get('NPU_MONITOR_TIMEZONE', 'auto'))
REPORT_TIMEZONE_OFFSET_SECONDS = timezone_offset_seconds(REPORT_TIMEZONE)
REPORT_TIMEZONE_NAME = timezone_label(REPORT_TIMEZONE_OFFSET_SECONDS)
