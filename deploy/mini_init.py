#!/usr/bin/env python3
"""Small PID 1/subreaper for offline containers without Docker --init."""

from __future__ import print_function

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time


PR_SET_CHILD_SUBREAPER = 36
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)
POLL_INTERVAL_SECONDS = 0.05


class MiniInit(object):
    def __init__(self, command, kill_after=20.0):
        self.command = command
        self.kill_after = kill_after
        self.child = None
        self.child_status = None
        self.stop_signal = None
        self.kill_deadline = None
        self.kill_sent = False

    @staticmethod
    def log(message):
        print('mini-init[{}]: {}'.format(os.getpid(), message), file=sys.stderr)
        sys.stderr.flush()

    def enable_subreaper(self):
        if not sys.platform.startswith('linux'):
            self.log('warning: child subreaper is only available on Linux')
            return False
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            result = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
            if result != 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
            return True
        except (AttributeError, OSError) as exc:
            self.log('warning: cannot enable child subreaper: {}'.format(exc))
            return False

    def forward(self, signum):
        if self.child is None or self.child_status is not None:
            return
        try:
            if os.name == 'posix':
                os.killpg(self.child.pid, signum)
            else:  # pragma: no cover - production is Linux
                self.child.send_signal(signum)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                self.log('cannot forward signal {}: {}'.format(signum, exc))

    def handle_signal(self, signum, _frame):
        if self.stop_signal is None:
            self.stop_signal = signum
            self.kill_deadline = time.monotonic() + self.kill_after
            self.log('forwarding signal {} to main process'.format(signum))
            self.forward(signum)
        else:
            self.log('received another signal {}; forcing shutdown'.format(signum))
            self.force_kill()

    def force_kill(self):
        if self.kill_sent or self.child_status is not None:
            return
        self.kill_sent = True
        self.log('main process did not stop in time; forwarding SIGKILL')
        self.forward(signal.SIGKILL)

    @staticmethod
    def decode_wait_status(status):
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return 1

    def reap(self):
        while True:
            try:
                waited_pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            except OSError as exc:
                if exc.errno == errno.ECHILD:
                    return
                if exc.errno == errno.EINTR:
                    continue
                raise
            if waited_pid == 0:
                return
            if self.child is not None and waited_pid == self.child.pid:
                self.child_status = self.decode_wait_status(status)
            else:
                self.log('reaped orphan process {}'.format(waited_pid))

    def run(self):
        if not self.command:
            self.log('no command supplied')
            return 2
        if os.name != 'posix':
            self.log('this program must run in a Linux container')
            return 2
        if os.getpid() != 1:
            self.log('warning: expected PID 1, got PID {}'.format(os.getpid()))

        self.enable_subreaper()
        for signum in FORWARDED_SIGNALS:
            signal.signal(signum, self.handle_signal)

        try:
            self.child = subprocess.Popen(self.command, start_new_session=True)
        except OSError as exc:
            self.log('cannot start main process: {}'.format(exc))
            return 127

        self.log('started main process {}: {}'.format(
            self.child.pid, ' '.join(self.command)
        ))
        while self.child_status is None:
            self.reap()
            if (self.kill_deadline is not None and
                    time.monotonic() >= self.kill_deadline):
                self.force_kill()
            time.sleep(POLL_INTERVAL_SECONDS)

        # Collect descendants that exited at the same time as the main process.
        self.reap()
        self.log('main process exited with status {}'.format(self.child_status))
        return self.child_status


def parse_command(argv):
    command = list(argv)
    if command and command[0] == '--':
        command = command[1:]
    return command


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    kill_after_text = os.environ.get('NPU_MONITOR_INIT_KILL_AFTER', '20')
    try:
        kill_after = max(1.0, float(kill_after_text))
    except ValueError:
        MiniInit.log('NPU_MONITOR_INIT_KILL_AFTER must be numeric')
        return 2
    return MiniInit(parse_command(argv), kill_after=kill_after).run()


if __name__ == '__main__':
    sys.exit(main())
