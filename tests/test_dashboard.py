"""Exercise HTTP routing directly, without requiring sandbox socket access."""
import io
import json
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netdiag.dashboard import create_server
from netdiag.storage import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite'
        store = Store(self.path, {})
        target = store.target('Gateway <test>', 'icmp', '127.0.0.1', 0.2)
        store.append([{'target_id': target, 'ts': 120., 'success': True, 'latency_ms': 2.5},
                      {'target_id': target, 'ts': 121., 'success': False, 'error': 'timeout'}])
        store.close()
        with patch('netdiag.dashboard.DashboardServer') as server:
            create_server(self.path)
            self.handler = server.call_args.args[1]

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path, host='127.0.0.1:8765', origin=None, site=None):
        handler = self.handler.__new__(self.handler)
        handler.path = path
        handler.client_address = ('127.0.0.1',12345)
        handler.server = SimpleNamespace(server_port=8765)
        handler.headers = Message()
        handler.headers['Host'] = host
        if origin is not None:
            handler.headers['Origin'] = origin
        if site is not None:
            handler.headers['Sec-Fetch-Site'] = site
        handler.request_version = 'HTTP/1.1'
        handler.command = 'GET'
        handler.requestline = 'GET ' + path + ' HTTP/1.1'
        handler.wfile = io.BytesIO()
        handler.do_GET()
        headers, body = handler.wfile.getvalue().split(b'\r\n\r\n',1)
        return int(headers.split()[1]), headers, body

    def test_read_endpoints_and_assets(self):
        for path in ('/', '/app.js', '/style.css', '/api/metadata', '/api/series?start=100&end=200', '/api/samples?start=100&end=200&limit=1', '/api/outages?start=100&end=200'):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status,200)
                self.assertIn(b'Content-Security-Policy:',headers)
                self.assertIn(b'Cache-Control: no-store',headers)
                self.assertTrue(body)
        result = json.loads(self.request('/api/samples?start=100&end=200&limit=1')[2])
        self.assertEqual(len(result['samples']),1)
        self.assertIsNotNone(result['next_before_id'])
        self.assertEqual(result['samples'][0]['error'],'timeout')
        result = json.loads(self.request('/api/series?start=100&end=200')[2])
        self.assertEqual(sum(p['count'] for p in result['series'][0]['points']),2)

    def test_query_validation(self):
        for query in ('start=nan&end=200','start=100&end=inf','start=200&end=100','start=100&end=200&limit=201','start=100&end=200&limit=0','start=100&end=200&before_id=-1','start=100&end=200&targets=1%29%3BDROP','start=100&end=200&targets=0','start=100&end=200&targets=','start=100&start=101&end=200','start=100&end=200&secret=x','end=200'):
            with self.subTest(query=query):
                self.assertEqual(self.request('/api/samples?'+query)[0],400)
        self.assertEqual(self.request('/api/series?start=100&end=200&buckets=601')[0],400)
        self.assertEqual(self.request('/api/metadata?start=1')[0],400)

    def test_local_access_boundaries(self):
        self.assertEqual(self.request('/api/metadata',host='attacker.example:8765')[0],403)
        self.assertEqual(self.request('/api/metadata',origin='https://attacker.example')[0],403)
        self.assertEqual(self.request('/api/metadata',origin='null')[0],403)
        self.assertEqual(self.request('/api/metadata',site='cross-site')[0],403)
        self.assertEqual(self.request('/api/metadata',origin='http://127.0.0.1:8765')[0],200)
        self.assertEqual(self.request('/api/metadata',host='localhost:8765')[0],200)
        self.assertEqual(self.request('/../../etc/passwd')[0],404)
        with self.assertRaises(ValueError):
            create_server(self.path,host='0.0.0.0')

    def test_missing_dataset_is_actionable_error(self):
        self.path.unlink()
        status, _, body = self.request('/api/metadata')
        self.assertEqual(status,503)
        self.assertIn('Dataset unavailable',json.loads(body)['error'])
        self.assertNotIn(str(self.path),body.decode())


if __name__ == '__main__':
    unittest.main()
