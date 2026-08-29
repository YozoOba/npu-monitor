from datetime import datetime, timedelta, timezone
import re


_OFFSET_PATTERN = re.compile(
    r'^(?:UTC)?(?P<sign>[+-])(?P<hours>\d{1,2})(?::?(?P<minutes>\d{2}))?$',
    re.IGNORECASE,
)
MAX_UTC_OFFSET_SECONDS = 14 * 60 * 60


def timezone_label(offset_seconds):
    offset_seconds = int(offset_seconds)
    sign = '+' if offset_seconds >= 0 else '-'
    total_minutes = abs(offset_seconds) // 60
    hours, minutes = divmod(total_minutes, 60)
    return 'UTC{}{:02d}:{:02d}'.format(sign, hours, minutes)


def fixed_timezone(offset_seconds):
    offset_seconds = int(offset_seconds)
    if abs(offset_seconds) > MAX_UTC_OFFSET_SECONDS:
        raise ValueError('timezone offset must be between UTC-14:00 and UTC+14:00')
    if offset_seconds % 60:
        raise ValueError('timezone offset must use whole minutes')
    return timezone(
        timedelta(seconds=offset_seconds), name=timezone_label(offset_seconds)
    )


def timezone_offset_seconds(value):
    offset = value.utcoffset(None)
    if offset is None:
        offset = datetime.now(value).utcoffset()
    seconds = int((offset or timedelta()).total_seconds())
    if seconds % 60:
        raise ValueError('local timezone offset must use whole minutes')
    return seconds


def resolve_timezone(setting='auto'):
    raw = str(setting or 'auto').strip()
    normalized = raw.lower()
    if normalized in ('auto', 'local', 'system'):
        local = datetime.now().astimezone()
        offset = local.utcoffset() or timedelta()
        return fixed_timezone(int(offset.total_seconds()))
    if normalized in ('utc', 'z', 'gmt'):
        return fixed_timezone(0)
    match = _OFFSET_PATTERN.fullmatch(raw)
    if not match:
        raise ValueError(
            'timezone must be auto, UTC, or a fixed offset such as UTC+08:00'
        )
    hours = int(match.group('hours'))
    minutes = int(match.group('minutes') or '0')
    if minutes > 59:
        raise ValueError('timezone minutes must be between 00 and 59')
    seconds = (hours * 60 + minutes) * 60
    if match.group('sign') == '-':
        seconds = -seconds
    return fixed_timezone(seconds)
