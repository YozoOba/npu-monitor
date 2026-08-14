import re
import subprocess


def _parse_hbm(text):
    matches = re.findall(r'(\d+)\s*/\s*(\d+)', text)
    if not matches:
        return None, None
    used, total = matches[-1]
    return int(used), int(total)


def parse_npu_smi_output(output):
    """Keep the parser shape proven by the original single-node monitor."""
    cards = {}
    lines = output.splitlines()
    for index, line in enumerate(lines[:-1]):
        match = re.match(r'^\|\s*(\d+)\s+\w+', line)
        if not match:
            continue
        card_id = int(match.group(1))
        parts = [part.strip() for part in lines[index + 1].split('|')]
        if len(parts) < 4:
            continue
        utilization_match = re.match(r'^([0-9]+(?:\.[0-9]+)?)\s*%?', parts[3])
        if not utilization_match:
            continue
        utilization = float(utilization_match.group(1))
        if not 0.0 <= utilization <= 100.0 or card_id in cards:
            continue
        hbm_used, hbm_total = _parse_hbm(parts[3])
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

