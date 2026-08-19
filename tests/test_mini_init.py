#!/usr/bin/env python3

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
MINI_INIT = ROOT / 'deploy' / 'mini_init.py'


@unittest.skipUnless(os.name == 'posix', 'mini-init runs in Linux containers')
class MiniInitProcessTests(unittest.TestCase):
    def run_init(self, child_code):
        return subprocess.run(
            [sys.executable, str(MINI_INIT), sys.executable, '-c', child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=10,
        )

    def test_preserves_main_process_exit_status(self):
        result = self.run_init('raise SystemExit(7)')
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_reaps_orphaned_descendant(self):
        result = self.run_init(
            'import os,time; pid=os.fork(); '
            'os._exit(0) if pid == 0 else time.sleep(0.2)'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('reaped orphan process', result.stderr)

    def test_forwards_stop_signal(self):
        process = subprocess.Popen(
            [
                sys.executable, str(MINI_INIT), sys.executable, '-c',
                'import time; time.sleep(30)',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        try:
            time.sleep(0.2)
            process.send_signal(signal.SIGTERM)
            _stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
