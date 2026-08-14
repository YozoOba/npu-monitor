"""Versioned and deterministic cluster sample protocol.

Authentication is intentionally outside this module. Validation here protects
measurement accuracy: malformed, ambiguous, duplicated-card and future-dated
samples must never enter aggregate calculations.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from . import PROTOCOL_VERSION


NODE_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


class ProtocolError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def isoformat_seconds(value):
    return value.isoformat(timespec='seconds')


def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError('timestamp must be a non-empty string')
    value = value.strip()
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(value)
    except AttributeError:
        parsed = None
    except ValueError:
        parsed = None
    if parsed is None:
        match = re.fullmatch(
            r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}):(\d{2})',
            value,
        )
        if not match:
            raise ProtocolError('timestamp must be ISO 8601 with timezone')
        try:
            parsed = datetime.strptime(
                ''.join(match.groups()), '%Y-%m-%dT%H:%M:%S%z'
            )
        except ValueError as exc:
            raise ProtocolError('invalid timestamp: {}'.format(exc))
    if parsed.tzinfo is None:
        raise ProtocolError('timestamp must include a timezone offset')
    return parsed


def _integer(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError('{} must be an integer'.format(name))
    if minimum is not None and value < minimum:
        raise ProtocolError('{} must be >= {}'.format(name, minimum))
    if maximum is not None and value > maximum:
        raise ProtocolError('{} must be <= {}'.format(name, maximum))
    return value


def _number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError('{} must be a number'.format(name))
    value = float(value)
    if value != value or value in (float('inf'), float('-inf')):
        raise ProtocolError('{} must be finite'.format(name))
    if minimum is not None and value < minimum:
        raise ProtocolError('{} must be >= {}'.format(name, minimum))
    if maximum is not None and value > maximum:
        raise ProtocolError('{} must be <= {}'.format(name, maximum))
    return value


def sample_identity(node_id, collected_at):
    canonical = '{}\n{}'.format(node_id, collected_at).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def normalize_sample(payload, received_at=None, max_future_seconds=300):
    if not isinstance(payload, dict):
        raise ProtocolError('request body must be a JSON object')
    version = _integer(payload.get('protocol_version'), 'protocol_version')
    if version != PROTOCOL_VERSION:
        raise ProtocolError('unsupported protocol_version {}'.format(version))

    node_id = payload.get('node_id')
    if not isinstance(node_id, str) or not NODE_ID_PATTERN.fullmatch(node_id):
        raise ProtocolError('node_id has an invalid format')
    node_name = payload.get('node_name') or node_id
    if not isinstance(node_name, str) or not node_name.strip() or len(node_name) > 255:
        raise ProtocolError('node_name must be a non-empty string up to 255 characters')

    collected = parse_timestamp(payload.get('collected_at'))
    received_at = received_at or utc_now()
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    future_seconds = (collected.astimezone(timezone.utc) - received_at).total_seconds()
    if future_seconds > max_future_seconds:
        raise ProtocolError(
            'collected_at is {:.0f} seconds in the future'.format(future_seconds)
        )

    expected = _integer(payload.get('expected_cards'), 'expected_cards', 1, 64)
    interval = _integer(payload.get('collect_interval'), 'collect_interval', 1, 86400)
    cards_value = payload.get('cards')
    if not isinstance(cards_value, list):
        raise ProtocolError('cards must be a list')
    if len(cards_value) > expected:
        raise ProtocolError('cards contains more entries than expected_cards')

    cards = []
    seen = set()
    for index, card in enumerate(cards_value):
        if not isinstance(card, dict):
            raise ProtocolError('cards[{}] must be an object'.format(index))
        card_id = _integer(
            card.get('card_id'), 'cards[{}].card_id'.format(index), 0,
            expected - 1,
        )
        if card_id in seen:
            raise ProtocolError('duplicate card_id {}'.format(card_id))
        seen.add(card_id)
        utilization = _number(
            card.get('utilization'), 'cards[{}].utilization'.format(index), 0, 100
        )
        used = card.get('hbm_used_mb')
        total = card.get('hbm_total_mb')
        if (used is None) != (total is None):
            raise ProtocolError('HBM used and total must both be null or both be numbers')
        if used is not None:
            used = _integer(used, 'cards[{}].hbm_used_mb'.format(index), 0)
            total = _integer(total, 'cards[{}].hbm_total_mb'.format(index), 1)
            if used > total:
                raise ProtocolError('HBM used cannot exceed HBM total')
        cards.append({
            'card_id': card_id,
            'utilization': utilization,
            'hbm_used_mb': used,
            'hbm_total_mb': total,
        })
    cards.sort(key=lambda item: item['card_id'])

    collected_text = isoformat_seconds(collected.astimezone(timezone.utc))
    missing = sorted(set(range(expected)) - seen)
    status = 'complete' if len(cards) == expected and not missing else 'partial'
    if not cards:
        status = 'failed'
    normalized = {
        'protocol_version': PROTOCOL_VERSION,
        'sample_id': sample_identity(node_id, collected_text),
        'node_id': node_id,
        'node_name': node_name.strip(),
        'collected_at': collected_text,
        'collect_interval': interval,
        'expected_cards': expected,
        'collected_cards': len(cards),
        'received_card_ids': [card['card_id'] for card in cards],
        'missing_card_ids': missing,
        'coverage_percent': round(len(cards) * 100.0 / expected, 2),
        'status': status,
        'cards': cards,
    }
    return normalized


def canonical_payload_hash(normalized):
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
