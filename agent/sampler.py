import re
import subprocess


def _parse_hbm(text):
    matches = re.findall(r'(\d+)\s*/\s*(\d+)', text)
    usable = [(used, total) for used, total in matches if int(total) > 0]
    if not usable:
        return None, None
    # Some products print both Memory/HBM and Hugepages as ratios, but their
    # column order differs.  The last ratio with a non-zero capacity selects
    # the actual device memory while ignoring placeholders such as ``0 / 0``.
    used, total = usable[-1]
    return int(used), int(total)


def _memory_column_index(lines):
    """Locate the product-specific device-memory column from table headers."""
    fallback = None
    for line in lines:
        parts = [part.strip().lower() for part in line.split('|')]
        for index, part in enumerate(parts):
            if 'hbm-usage' in part:
                return index
            if 'memory-usage' in part:
                fallback = index
    return fallback


def _parse_device_id(parts, fallback):
    """Return the logical Device/Phy-ID from the second npu-smi table row.

    Ascend products do not always use the first-row NPU number as the logical
    device id.  For example, a 310P3 board can report physical NPU numbers
    ``1, 1, 2, 2, 4, 4, 5, 5`` while the corresponding Device ids are
    ``0..7``.  The latter is the stable identity used by applications and by
    this monitor.

    Real npu-smi output puts ``Chip Device`` or ``Chip Phy-ID`` in the first
    table cell.  The latter is 0..15 on an eight-card, dual-die 910C.  The
    second branch keeps compatibility with the older synthetic/test layout
    where Chip and Device were separated by a pipe.
    """
    identifiers = re.findall(r'\d+', parts[1]) if len(parts) > 1 else []
    if len(identifiers) >= 2:
        return int(identifiers[1])
    if (
        len(identifiers) == 1 and len(parts) > 2
        and re.fullmatch(r'\d+', parts[2])
    ):
        return int(parts[2])
    return fallback


def parse_npu_smi_output(output):
    """Keep the parser shape proven by the original single-node monitor."""
    cards = {}
    lines = output.splitlines()
    memory_column = _memory_column_index(lines)
    for index, line in enumerate(lines[:-1]):
        # A record header contains a numeric physical NPU id followed by a
        # product name such as 910B or 310P3.  Requiring a letter in the
        # product token prevents the numeric ``Chip Device`` detail row from
        # being mistaken for the next record header.
        match = re.match(r'^\|\s*(\d+)\s+(?=\S*[A-Za-z])\S+', line)
        if not match:
            continue
        physical_npu_id = int(match.group(1))
        parts = [part.strip() for part in lines[index + 1].split('|')]
        if len(parts) < 4:
            continue
        card_id = _parse_device_id(parts, physical_npu_id)
        utilization_match = re.match(r'^([0-9]+(?:\.[0-9]+)?)\s*%?', parts[3])
        if not utilization_match:
            continue
        utilization = float(utilization_match.group(1))
        if not 0.0 <= utilization <= 100.0 or card_id in cards:
            continue
        memory_text = (
            parts[memory_column]
            if memory_column is not None and memory_column < len(parts)
            else ' | '.join(parts[3:])
        )
        hbm_used, hbm_total = _parse_hbm(memory_text)
        cards[card_id] = {
            'card_id': card_id,
            'utilization': utilization,
            'hbm_used_mb': hbm_used,
            'hbm_total_mb': hbm_total,
        }
    return [cards[key] for key in sorted(cards)]


def collect(command='npu-smi', timeout=10):
    try:
        result = subprocess.run(
            [command, 'info'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return [], 'npu-smi timed out after {} seconds'.format(timeout)
    except OSError as exc:
        return [], 'unable to execute npu-smi: {}'.format(exc)
    if result.returncode != 0:
        return [], 'npu-smi exit {}: {}'.format(
            result.returncode, result.stderr.strip()[:1000]
        )
    cards = parse_npu_smi_output(result.stdout)
    if not cards:
        return [], 'npu-smi returned no usable card data'
    return cards, None
