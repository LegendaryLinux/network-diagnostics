import asyncio
import errno
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from netdiag.config import load_config
from netdiag import probes
from netdiag.protocol import FRAME_SIZE,make_response


class ConfigTests(unittest.TestCase):
    def test_defaults_and_secret_rejection(self):
        config = load_config()
        self.assertEqual(config['fast_interval'],.2)
        self.assertEqual(len(config['services']),5)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'config.json'
            for value in ({'key':'secret'},{'services':[{'name':'private','url':'https://a:b@example.com/'}]},{'fast_interval':0},{'services':[{'name':'token','url':'https://example.com/?token=secret'}]}):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    load_config(path)


    def test_legacy_gateway_is_ignored_without_route_discovery(self):
        def read_fixture(path,*args,**kwargs):
            if str(path)=='legacy.json':
                return json.dumps({'gateway':{'unused':'not an IP address'},'public_interval':60})
            if str(path)=='/etc/resolv.conf':
                return 'nameserver 127.0.0.1'
            self.fail('Unexpected configuration read: '+str(path))
        with patch('netdiag.config.Path.read_text',autospec=True,side_effect=read_fixture):
            config = load_config('legacy.json')
            defaults = load_config()
        self.assertNotIn('gateway',config)
        self.assertNotIn('gateway',defaults)
        self.assertEqual(config['public_interval'],60)
        self.assertEqual(config['dns_server'],'127.0.0.1')


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_errors_and_local_gaps(self):
        for error,category in ((asyncio.TimeoutError(),'timeout'),(ConnectionRefusedError(),'connection_refused'),(FileNotFoundError(),'missing_executable'),(PermissionError(),'local_permission'),(ValueError(),'invalid_response')):
            result = await probes.measure(AsyncMock(side_effect=error),1)
            self.assertEqual(result['error'],category)
            self.assertIsNone(result['success']) if category in ('missing_executable','local_permission') else self.assertFalse(result['success'])
            self.assertGreaterEqual(result['latency_ms'],0)

    async def test_http_timing_and_rejection(self):
        with patch('netdiag.probes.process',AsyncMock(return_value=(0,json.dumps({'http_code':403,'time_total':.1,'time_namelookup':.01,'time_connect':.03,'time_appconnect':.07})))):
            result = await probes.http('https://example.com',1)
        self.assertTrue(result['success'])
        self.assertEqual(result['http_status'],403)
        self.assertNotIn('error',result)
        self.assertIn('request rejected',result['detail'])
        self.assertAlmostEqual(result['connect_ms'],20)
        self.assertAlmostEqual(result['tls_ms'],40)
        with patch('netdiag.probes.process',AsyncMock(return_value=(28,'{"http_code":0,"time_total":1.0}'))):
            self.assertEqual((await probes.http('https://example.com',1))['error'],'timeout')

    async def test_http_completed_statuses_and_partial_transport_failures(self):
        for status in (200,204,301,399,400,401,403,404,405,429,500,503,599):
            with self.subTest(status=status),patch('netdiag.probes.process',AsyncMock(return_value=(0,json.dumps({'http_code':status,'time_total':.05})))):
                result = await probes.http('https://fixture.test/',1)
                self.assertTrue(result['success'])
                self.assertEqual(result['http_status'],status)
                self.assertEqual(result['latency_ms'],50)
                self.assertNotIn('error',result)
        for status in (100,101,199,0,600,None,True,200.5,'200','garbage'):
            with self.subTest(status=status),patch('netdiag.probes.process',AsyncMock(return_value=(0,json.dumps({'http_code':status})))):
                result = await probes.http('https://fixture.test/',1)
                self.assertFalse(result['success'])
                self.assertEqual(result['error'],'http_invalid_response')
        for code,error in ((28,'timeout'),(35,'tls_failure'),(60,'tls_certificate'),(56,'receive_failure')):
            with self.subTest(code=code),patch('netdiag.probes.process',AsyncMock(return_value=(code,json.dumps({'http_code':403,'time_total':1})))):
                result = await probes.http('https://fixture.test/',1)
                self.assertFalse(result['success'])
                self.assertEqual(result['http_status'],403)
                self.assertEqual(result['error'],error)
                self.assertEqual(result['detail'],'curl exit '+str(code))
        for payload in ('not-json','null','[]'):
            with self.subTest(payload=payload),patch('netdiag.probes.process',AsyncMock(return_value=(28,payload))):
                self.assertEqual((await probes.http('https://fixture.test/',1))['error'],'timeout')

    async def test_persistent_frames_reconnect_and_timeout(self):
        key = b'x'*32
        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        reader = MagicMock()
        async def respond(size):
            self.assertEqual(size,FRAME_SIZE)
            return make_response(writer.write.call_args.args[0],key)
        reader.readexactly = AsyncMock(side_effect=respond)
        with patch('asyncio.open_connection',AsyncMock(return_value=(reader,writer))):
            remote = probes.Remote('127.0.0.1',48123,key,1)
            self.assertEqual((await remote.persistent())['reconnected'],0)
            self.assertEqual((await remote.persistent())['reconnected'],0)
            await remote.close()
            self.assertEqual((await remote.persistent())['reconnected'],1)
            reader.readexactly.side_effect = asyncio.TimeoutError
            result = await probes.measure(remote.persistent,1)
            self.assertEqual(result['error'],'timeout')
            self.assertIsNone(remote.writer)

    async def test_dns_response_and_udp_authenticated_response(self):
        loop = asyncio.get_running_loop()
        sock = MagicMock()
        sock.__enter__.return_value = sock
        with patch('socket.socket',return_value=sock),patch.object(loop,'sock_sendall',AsyncMock()) as send,patch.object(loop,'sock_recv',AsyncMock()) as receive:
            async def dns_reply(*args):
                request = send.call_args.args[1]
                return request[:2]+struct.pack('!5H',0x8180,1,1,0,0)+request[12:]+b'\xc0\x0c'+struct.pack('!HHIH',1,1,60,4)+b'\x7f\x00\x00\x01'
            receive.side_effect = dns_reply
            self.assertTrue((await probes.dns('example.com','127.0.0.1',1))['success'])
            async def udp_reply(*args):
                return make_response(send.call_args.args[1],b'x'*32)
            receive.side_effect = udp_reply
            with patch.object(loop,'getaddrinfo',AsyncMock(return_value=[(2,2,17,'',('127.0.0.1',48123))])):
                self.assertTrue((await probes.Remote('localhost',48123,b'x'*32,1).udp())['success'])


    async def test_scheduler_gaps_flush_and_resume(self):
        from netdiag.collector import collect
        from netdiag.storage import Reader
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp)/'data.sqlite3')
            config = load_config()
            config.update(db=path,services=[],remote={'host':'localhost','port':48123},fast_interval=.05,timeout=.1)
            stop = asyncio.Event()
            async def slow():
                await asyncio.sleep(.14)
                return {'success':True,'latency_ms':140}
            remote = MagicMock()
            remote.host,remote.port = 'localhost',48123
            remote.persistent = slow
            remote.udp = AsyncMock(return_value={'success':False,'error':'timeout'})
            remote.close = AsyncMock()
            asyncio.get_running_loop().call_later(.36,stop.set)
            with patch('netdiag.protocol.load_key',return_value=b'x'*32),patch('netdiag.probes.Remote',return_value=remote):
                await collect(config,serve_dashboard=False,stop=stop)
            metadata = Reader(path).metadata()
            self.assertEqual(metadata['sessions'][0]['status'],'stopped')
            rows = Reader(path).samples(0,1e12)['samples']
            self.assertTrue(any(row['error']=='collector_gap' for row in rows))
            self.assertTrue(any(row['error']=='timeout' for row in rows))
            self.assertTrue(any(row['success']==1 for row in rows))
            self.assertFalse(any('xxxxxxxx' in str(row) for row in rows))

    async def test_final_flush_failure_marks_session_failed(self):
        from netdiag.collector import collect
        from netdiag.storage import Reader
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp)/'data.sqlite3')
            config = load_config()
            config.update(db=path,services=[{'name':'Fixture','url':'https://fixture.test/'}],remote=None,public_interval=.05,timeout=.1)
            stop = asyncio.Event()
            asyncio.get_running_loop().call_later(.12,stop.set)
            stderr = io.StringIO()
            with patch('netdiag.storage.Store.append',side_effect=OSError(errno.ENOSPC,'fixture full disk')) as append,patch('netdiag.probes.dns',AsyncMock(return_value={'success':True,'latency_ms':1})),patch('netdiag.probes.tcp',AsyncMock(return_value={'success':True,'latency_ms':1})),patch('netdiag.probes.http',AsyncMock(return_value={'success':True,'latency_ms':1})),patch('sys.stderr',stderr):
                with self.assertRaises(OSError):
                    await collect(config,serve_dashboard=False,stop=stop)
            self.assertTrue(any(row['success'] for row in append.call_args.args[0]))
            self.assertEqual(Reader(path).metadata()['sessions'][0]['status'],'failed')
            self.assertIn('FATAL: storage write failed',stderr.getvalue())
            self.assertIn('could not be saved',stderr.getvalue())

    async def test_local_resource_errors_are_coverage_gaps(self):
        for number in (errno.EMFILE,errno.ENFILE,errno.ENOMEM,errno.ENOBUFS,errno.EADDRNOTAVAIL):
            result = await probes.measure(AsyncMock(side_effect=OSError(number,'fixture')),1)
            self.assertIsNone(result['success'])
            self.assertEqual(result['error'],'local_resource_error')
            self.assertEqual(result['missed'],1)
            self.assertEqual(result['detail'],'errno='+str(number))
        result = await probes.measure(AsyncMock(side_effect=OSError(errno.ENETUNREACH,'fixture')),1)
        self.assertFalse(result['success'])
        self.assertEqual(result['error'],'network_error')

    async def test_dns_malformed_responses_and_compressed_answer(self):
        header = struct.pack('!6H',42,0x8180,1,1,0,0)
        question = b'\x07example\x03com\x00\x00\x01\x00\x01'
        answer = b'\xc0\x0c'+struct.pack('!HHIH',1,1,60,4)+b'\x7f\x00\x00\x01'
        self.assertIsNone(probes.dns_response_error(header+question+answer,'example.com'))
        for invalid in (header,header+b'answer',header+question,header+question+answer[:-1],header+question+answer[:-4],header+question+b'\xc0\xff'+answer[2:]):
            self.assertEqual(probes.dns_response_error(invalid,'example.com'),'dns_invalid_response')
        self.assertEqual(probes.dns_response_error(header+question+answer,'different.com'),'dns_invalid_response')
        self.assertEqual(probes.dns_response_error(struct.pack('!6H',42,0x8183,1,0,0,0)+question,'example.com'),'dns_nxdomain')

    async def test_fresh_tcp_success(self):
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',AsyncMock(return_value=[(2,1,6,'',('127.0.0.1',48123))])),patch('asyncio.open_connection',AsyncMock(return_value=(MagicMock(),writer))):
            result = await probes.tcp('localhost',48123,1)
            self.assertTrue(result['success'])
            self.assertGreaterEqual(result['connect_ms'],0)
            writer.close.assert_called_once()


if __name__=='__main__':
    unittest.main()
