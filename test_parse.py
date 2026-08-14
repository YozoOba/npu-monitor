#!/usr/bin/env python3
"""Run the production parser against npu-smi on an NPU server."""
import subprocess
import sys

from config import EXPECTED_NPU_COUNT, NPU_SMI_TIMEOUT
from npu_monitor import parse_npu_smi_output


def main():
    try:
        result = subprocess.run(
            ['npu-smi', 'info'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=NPU_SMI_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f'Unable to run npu-smi: {exc}', file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return 1

    cards = parse_npu_smi_output(result.stdout)
    for card in cards:
        print(
            f"Card {card['card_id']}: AICore={card['utilization']}% "
            f"HBM={card['hbm_used_mb']}/{card['hbm_total_mb']} MB"
        )

    if len(cards) != EXPECTED_NPU_COUNT:
        print(
            f'WARNING: expected {EXPECTED_NPU_COUNT} cards, parsed {len(cards)}',
            file=sys.stderr,
        )
        return 1
    print(f'SUCCESS: parsed all {len(cards)} cards')
    return 0


if __name__ == '__main__':
    sys.exit(main())
