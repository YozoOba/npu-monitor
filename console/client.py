import json
try:
    from urllib.parse import urlencode
    from urllib.request import urlopen
except ImportError:  # pragma: no cover
    from urllib import urlencode
    from urllib2 import urlopen


class CollectorClient:
    def __init__(self, base_url, timeout=10):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def _get(self, path, parameters=None):
        url = self.base_url + path
        if parameters:
            filtered = {key: value for key, value in parameters.items() if value is not None}
            url += '?' + urlencode(filtered)
        response = urlopen(url, timeout=self.timeout)
        return json.loads(response.read().decode('utf-8'))

    def health(self):
        return self._get('/health')

    def snapshot(self):
        return self._get('/internal/v1/snapshot')

    def series(self, start=None, end=None, bucket=300, node_id=None):
        return self._get('/internal/v1/series', {
            'start': start,
            'end': end,
            'bucket': bucket,
            'node_id': node_id,
        })

