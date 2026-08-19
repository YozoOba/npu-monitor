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

    def snapshot(self, cluster_id=None):
        return self._get('/internal/v1/snapshot', {'cluster_id': cluster_id})

    def clusters(self):
        return self._get('/internal/v1/clusters')

    def nodes(self, page=1, page_size=50, state=None, cluster_id=None,
              search=None):
        return self._get('/internal/v1/nodes', {
            'page': page, 'page_size': page_size, 'state': state,
            'cluster_id': cluster_id, 'q': search,
        })

    def series(self, start=None, end=None, bucket=300, node_id=None,
               cluster_id=None, card_id=None):
        return self._get('/internal/v1/series', {
            'start': start,
            'end': end,
            'bucket': bucket,
            'node_id': node_id,
            'cluster_id': cluster_id,
            'card_id': card_id,
        })

    def samples(self, start=None, end=None, page=1, page_size=100,
                node_id=None, cluster_id=None, status=None, search=None):
        return self._get('/internal/v1/samples', {
            'start': start, 'end': end, 'page': page, 'page_size': page_size,
            'node_id': node_id, 'cluster_id': cluster_id, 'status': status,
            'q': search,
        })

    def alerts(self, start=None, end=None, page=1, page_size=100,
               node_id=None, cluster_id=None, severity=None, status=None,
               alert_type=None):
        return self._get('/internal/v1/alerts', {
            'start': start, 'end': end, 'page': page, 'page_size': page_size,
            'node_id': node_id, 'cluster_id': cluster_id,
            'severity': severity, 'status': status, 'type': alert_type,
        })
