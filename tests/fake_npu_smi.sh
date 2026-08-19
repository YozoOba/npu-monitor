#!/bin/bash

if [ "${FAKE_NPU_SMI_SLEEP:-0}" != "0" ]; then
    sleep "$FAKE_NPU_SMI_SLEEP"
fi

if [ "${FAKE_NPU_SMI_FAIL:-0}" = "1" ]; then
    echo "simulated npu-smi failure" >&2
    exit 1
fi

for card_id in 0 1 2 3 4 5 6 7; do
    echo "| $card_id     910B           | OK                   | 100.0                |"
    echo "| 0       $card_id             | 0000:00:00.0         | 42.5 1024 / 65536   |"
done
