import asyncio
import contextlib
from email.utils import formatdate
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from netdiag.collector import ServiceBackoff, collect
from netdiag.config import load_config
from netdiag.probes import http, retry_after_seconds
from netdiag.storage import Reader


class BackoffPolicyTests(unittest.TestCase):
    def test_doubling_cap_recovery_and_transport_failure_history(self):
        backoff = ServiceBackoff(60)
        for index,expected in enumerate((120,240,480,960,1920,3600,3600)):
            result = {'success':False,'http_status':(401,403,429)[index%3]}
            backoff.observe(result,100)
            self.assertEqual(backoff.delay,expected)
            self.assertEqual(backoff.until,100+expected)
            self.assertIn('service backoff',result['detail'])
        backoff.observe({'success':False,'error':'timeout'},5000)
        self.assertEqual(backoff.delay,3600)
        backoff.observe({'success':True,'http_status':200},5001)
        self.assertEqual(backoff.until,0)
        self.assertEqual(backoff.delay,0)
        backoff.observe({'success':False,'http_status':429},5002)
        self.assertEqual(backoff.delay,120)

    def test_minimum_cadence_longer_retry_after_and_nonrejections(self):
        for interval,expected in ((5,60),(60,120),(3600,3600),(7200,7200)):
            backoff = ServiceBackoff(interval)
            backoff.observe({'success':False,'http_status':403},0)
            self.assertEqual(backoff.delay,expected)
        backoff = ServiceBackoff(60)
        backoff.observe({'success':False,'http_status':429,'retry_after_seconds':7200},10)
        self.assertEqual(backoff.until,7210)
        backoff.observe({'success':False,'http_status':503},7211)
        self.assertEqual(backoff.delay,120)
        backoff.observe({'success':False,'http_status':403},7212)
        self.assertEqual(backoff.delay,240)
        self.assertEqual(ServiceBackoff(60).until,0)

    def test_retry_after_seconds_dates_and_hostile_values(self):
        with patch('netdiag.probes.time.time',return_value=1700000000):
            self.assertEqual(retry_after_seconds('7200'),7200)
            self.assertEqual(retry_after_seconds('0'),0)
            self.assertEqual(retry_after_seconds(formatdate(1700007200,usegmt=True)),7200)
            self.assertEqual(retry_after_seconds(formatdate(1699999999,usegmt=True)),0)
            for value in ('','-1','+1','1.5','inf','NaN','9'*10000,'Wed, 99 Oct 99999999999999 99:99:99 GMT','not a date','Tue, 14 Nov 2023 22:13:20'):
                self.assertIsNone(retry_after_seconds(value),value[:50])
            self.assertGreater(retry_after_seconds('9'*128),1e100)


class BackoffIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_headers_only_and_no_raw_header_persistence(self):
        headers = 'HTTP/1.1 103 Early Hints\r\nRetry-After: 99999\r\n\r\nHTTP/2 429\r\nRetry-After: invalid\r\nretry-after: 7200\r\nSet-Cookie: fixture-private-cookie\r\n\r\n'
        with patch('netdiag.probes.process',AsyncMock(return_value=(0,headers+'\n'+json.dumps({'http_code':429,'time_total':.1})))) as process:
            result = await http('https://example.test/',2)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'],'http_rejected')
        self.assertEqual(result['retry_after_seconds'],7200)
        self.assertNotIn('fixture-private-cookie',str(result))
        self.assertIn('--dump-header',process.call_args.args)
        for status in (401,403,429):
            with patch('netdiag.probes.process',AsyncMock(return_value=(0,'HTTP/1.1 '+str(status)+' Rejected\r\nRetry-After: 1\r\n\r\n\n'+json.dumps({'http_code':status})))):
                self.assertEqual((await http('https://example.test/',2))['retry_after_seconds'],1)

    async def test_service_isolation_skip_evidence_and_no_http_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config()
            config.update(db=str(Path(directory)/'evidence.sqlite3'),gateway='127.0.0.1',remote={'host':'fixture.test','port':48123},fast_interval=.05,public_interval=.05,timeout=.2,services=[{'name':'Rejected','url':'https://rejected.test/'},{'name':'Healthy','url':'https://healthy.test/'}])
            stop = asyncio.Event()
            calls = []
            active = maximum = 0
            async def request(url,timeout):
                nonlocal active,maximum
                calls.append(url)
                if 'rejected' in url:
                    active += 1
                    maximum = max(maximum,active)
                    await asyncio.sleep(.12)
                    active -= 1
                    return {'success':False,'error':'http_rejected','http_status':429}
                return {'success':True,'http_status':200,'latency_ms':1}
            remote = MagicMock(host='fixture.test',port=48123)
            remote.persistent = AsyncMock(return_value={'success':True,'latency_ms':1})
            remote.udp = AsyncMock(return_value={'success':True,'latency_ms':1})
            remote.close = AsyncMock()
            asyncio.get_running_loop().call_later(.4,stop.set)
            with patch('netdiag.probes.http',side_effect=request),patch('netdiag.probes.dns',AsyncMock(return_value={'success':True,'latency_ms':1})),patch('netdiag.probes.tcp',AsyncMock(return_value={'success':True,'latency_ms':1})),patch('netdiag.probes.ping',AsyncMock(return_value={'success':True,'latency_ms':1})),patch('netdiag.probes.Remote',return_value=remote),contextlib.redirect_stdout(io.StringIO()):
                await collect(config,serve_dashboard=False,stop=stop,key=b'k'*32)
            self.assertEqual(maximum,1)
            self.assertEqual(calls.count('https://rejected.test/'),1)
            self.assertGreater(calls.count('https://healthy.test/'),3)
            self.assertGreater(remote.persistent.await_count,3)
            self.assertGreater(remote.udp.await_count,3)
            reader = Reader(config['db'])
            targets = {t['id']:t for t in reader.metadata()['targets']}
            rows = reader.samples(0,1e12,limit=1000)['samples']
            skips = [r for r in rows if r['error']=='service_backoff']
            self.assertEqual({targets[r['target_id']]['probe'] for r in skips},{'dns','tcp','https'})
            self.assertTrue(all(targets[r['target_id']]['name']=='Rejected' and r['success'] is None and r['missed']==1 for r in skips))
            self.assertTrue(all(r['ts']<=reader.metadata()['sessions'][0]['ended'] for r in skips))
            failures = [r for r in rows if r['success']==0]
            self.assertEqual(len(failures),1)
            self.assertIn('service backoff 60s',failures[0]['detail'])
            series = reader.series(0,1e12)['series']
            self.assertEqual(sum(p['failures'] for s in series for p in s['points']),1)
            self.assertGreater(sum(p['gaps'] for s in series for p in s['points']),len(skips)-1)
            self.assertEqual(reader.metadata()['sessions'][0]['status'],'stopped')


if __name__=='__main__':
    unittest.main()
