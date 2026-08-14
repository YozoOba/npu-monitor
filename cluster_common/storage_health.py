import os
import shutil


def check_capacity(path, minimum_free_bytes=0, minimum_free_inodes=0):
    try:
        usage = shutil.disk_usage(path)
        free_bytes = usage.free
    except OSError as exc:
        return False, {'error': 'cannot inspect storage: {}'.format(exc)}
    free_inodes = None
    try:
        stat = os.statvfs(path)
        free_inodes = stat.f_favail
    except (AttributeError, OSError):
        pass
    status = {
        'free_bytes': free_bytes,
        'free_inodes': free_inodes,
        'minimum_free_bytes': minimum_free_bytes,
        'minimum_free_inodes': minimum_free_inodes,
    }
    if free_bytes < minimum_free_bytes:
        status['error'] = 'low disk space'
        return False, status
    if (minimum_free_inodes and free_inodes is not None and
            free_inodes < minimum_free_inodes):
        status['error'] = 'low inode count'
        return False, status
    return True, status

