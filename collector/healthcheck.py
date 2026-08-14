#!/usr/bin/env python3
import json
import sys
try:
    from urllib.request import urlopen
except ImportError:  # pragma: no cover
    from urllib2 import urlopen

from . import config


def main():
    try:
        response = urlopen('http://127.0.0.1:{}/health'.format(config.PORT), timeout=3)
        value = json.loads(response.read().decode('utf-8'))
        print(value.get('status'))
        return 0 if response.getcode() == 200 and value.get('status') == 'healthy' else 1
    except Exception as exc:
        print('unhealthy: {}'.format(exc))
        return 1


if __name__ == '__main__':
    sys.exit(main())

