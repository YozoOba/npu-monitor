#!/usr/bin/env python3
import unittest

from agent.sampler import parse_npu_smi_output


class NpuSmiParserTests(unittest.TestCase):
    def test_310p_uses_logical_device_id_instead_of_physical_npu_id(self):
        output = """
| NPU     Name            | Health                | Power(W)              |
| Chip    Device          | Bus-Id                | AICore(%) Memory-Usage(MB) |
| 1       310P3           | OK                    | NA                    |
| 0       0               | 0000:01:00.0          | 0       1558 / 44278  |
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

    def test_legacy_split_chip_device_layout_remains_supported(self):
        output = """
| 3       910B            | OK                    | 100.0                 |
| 0                       | 3                     | 8       512 / 65536   |
"""
        cards = parse_npu_smi_output(output)
        self.assertEqual(cards[0]['card_id'], 3)


if __name__ == '__main__':
    unittest.main()
