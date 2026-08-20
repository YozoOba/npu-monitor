#!/usr/bin/env python3
import unittest

from agent.app import resolve_expected_cards
from agent.sampler import parse_npu_smi_output


class NpuSmiParserTests(unittest.TestCase):
    def test_310p_uses_logical_device_id_instead_of_physical_npu_id(self):
        output = """
| NPU     Name            | Health                | Power(W)              |
| Chip    Device          | Bus-Id                | AICore(%) Memory-Usage(MB) | Hugepages-Usage(page) |
| 1       310P3           | OK                    | NA                    |
| 0       0               | 0000:01:00.0          | 0       1558 / 44278  | 99 / 100 |
| 1       310P3           | OK                    | NA                    |
| 1       1               | 0000:01:00.0          | 3       1406 / 43693  |
| 2       310P3           | OK                    | NA                    |
| 0       2               | 0000:02:00.0          | 7       1350 / 44278  |
| 2       310P3           | OK                    | NA                    |
| 1       3               | 0000:02:00.0          | 12      1613 / 43693  |
| 4       310P3           | OK                    | NA                    |
| 0       4               | 0000:81:00.0          | 0       1841 / 44278  |
| 4       310P3           | OK                    | NA                    |
| 1       5               | 0000:81:00.0          | 25      1124 / 43693  |
| 5       310P3           | OK                    | NA                    |
| 0       6               | 0000:82:00.0          | 50      1609 / 44278  |
| 5       310P3           | OK                    | NA                    |
| 1       7               | 0000:82:00.0          | 100     1356 / 43693  |
"""
        cards = parse_npu_smi_output(output)
        self.assertEqual([card['card_id'] for card in cards], list(range(8)))
        self.assertEqual(cards[1]['utilization'], 3.0)
        self.assertEqual(cards[7]['hbm_used_mb'], 1356)
        self.assertEqual(cards[7]['hbm_total_mb'], 43693)

    def test_910_layout_remains_supported(self):
        output = """
| 0       910B            | OK                    | 100.0                 |
| 0       0               | 0000:01:00.0          | 42.5    1024 / 65536  |
| 1       910B            | OK                    | 100.0                 |
| 0       1               | 0000:02:00.0          | 75      2048 / 65536  |
"""
        cards = parse_npu_smi_output(output)
        self.assertEqual([card['card_id'] for card in cards], [0, 1])
        self.assertEqual([card['utilization'] for card in cards], [42.5, 75.0])

    def test_910c_dual_die_layout_uses_all_sixteen_phy_ids(self):
        rows = [
            '| NPU Name | Health | Power(W) | Temp(C) | Hugepages-Usage(page) |',
            '| Chip Phy-ID | Bus-Id | AICore(%) | Memory-Usage(MB) | HBM-Usage(MB) |',
        ]
        for phy_id in range(16):
            physical_npu = phy_id // 2
            chip_id = phy_id % 2
            rows.extend([
                '| {} Ascend910 | OK | 164.0 | 36 | 0 / 0 |'.format(physical_npu),
                '| {} {} | 0000:85:00.0 | {} | 0 / 0 | {} / 65536 |'.format(
                    chip_id, phy_id, phy_id, 61000 + phy_id,
                ),
            ])
        cards = parse_npu_smi_output('\n'.join(rows))
        self.assertEqual([card['card_id'] for card in cards], list(range(16)))
        self.assertEqual(cards[15]['utilization'], 15.0)
        self.assertEqual(cards[15]['hbm_used_mb'], 61015)
        self.assertEqual(cards[15]['hbm_total_mb'], 65536)
        self.assertEqual(resolve_expected_cards(cards, 8), 16)

    def test_legacy_split_chip_device_layout_remains_supported(self):
        output = """
| 3       910B            | OK                    | 100.0                 |
| 0                       | 3                     | 8       512 / 65536   |
"""
        cards = parse_npu_smi_output(output)
        self.assertEqual(cards[0]['card_id'], 3)


if __name__ == '__main__':
    unittest.main()
