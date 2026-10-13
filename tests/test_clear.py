import asyncio
import contextlib
import concurrent.futures
from email.message import Message
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from netdiag.collector import collect
from netdiag.config import load_config
from netdiag.dashboard import create_server
from netdiag.storage import ClearBusy, Reader, Store, clear_database


class ClearStorageTests(unittest.TestCase):
    def test_clear_retains_active_context_and_rollback_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'data.sqlite3'
            store = Store(path,{'source':'fixture'})
            current = store.target('current','tcp','fixture',5)
            old = store.target('old','icmp','fixture',.2)
            store.append([{'target_id':old,'ts':1,'success':False},{'target_id':current,'ts':2,'success':True,'latency_ms':1}])
            store.conn.execute("CREATE TRIGGER fail_clear BEFORE DELETE ON targets BEGIN SELECT RAISE(ABORT,'fixture'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                store.clear({'source':'fixture'},[current])
            self.assertEqual(len(Reader(path).samples(0,10)['samples']),2)
            store.conn.execute('DROP TRIGGER fail_clear')
            store.clear({'source':'fixture'},[current])
            for table in ('samples','rollups','outcome_counts','outages'):
                self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
            self.assertEqual([r['id'] for r in Reader(path).metadata()['targets']],[current])
            self.assertEqual(len(Reader(path).metadata()['sessions']),1)
            self.assertEqual(Reader(path).metadata()['sessions'][0]['config'],{'source':'fixture'})
            store.append([{'target_id':current,'ts':3,'success':True,'latency_ms':10}])
            self.assertEqual(Reader(path).outcomes(0,120)['buckets'][0]['total'],1)
            with self.assertRaises(ClearBusy):
                clear_database(path)
            store.close()
            clear_database(path)
            metadata = Reader(path).metadata()
            self.assertEqual(metadata['sessions'],[])
            self.assertEqual(metadata['targets'],[])
            self.assertIsNone(metadata['first_ts'])
            clear_database(path)  # Already-empty history is a valid confirmed action.

    def test_legacy_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'legacy.sqlite3'
            store = Store(path,{})
            target = store.target('old','icmp','fixture',.2)
            store.append([{'target_id':target,'ts':1,'success':True,'latency_ms':1}])
            store.close()
            with contextlib.closing(sqlite3.connect(path)) as connection:
                connection.execute('DROP TABLE outcome_counts')
            clear_database(path)
            with contextlib.closing(sqlite3.connect(path)) as connection:
                self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='outcome_counts'").fetchone())
            self.assertEqual(Reader(path).samples(0,10)['samples'],[])
            missing = Path(directory)/'missing.sqlite3'
            with self.assertRaises(FileNotFoundError):
                clear_database(missing)
            self.assertFalse(missing.exists())
            unrelated = Path(directory)/'unrelated.sqlite3'
            with contextlib.closing(sqlite3.connect(unrelated)) as connection:
                connection.execute('CREATE TABLE samples(id INTEGER PRIMARY KEY,ts REAL)')
                connection.execute('INSERT INTO samples VALUES(1,123)')
                connection.commit()
            with self.assertRaises(ValueError):
                clear_database(unrelated)
            with contextlib.closing(sqlite3.connect(unrelated)) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM samples').fetchone()[0],1)


class ClearAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'data.sqlite3'
        store = Store(self.path,{})
        target = store.target('fixture','tcp','fixture',5)
        store.append([{'target_id':target,'ts':120,'success':True,'latency_ms':1}])
        store.close()
        with patch('netdiag.dashboard.DashboardServer') as server:
            create_server(self.path)
            self.handler = server.call_args.args[1]
        self.token = self.request('GET','/api/metadata')[1]['clear']['token']

    def tearDown(self):
        self.temp.cleanup()

    def request(self,method,path='/api/clear',body=b'{"confirm":"CLEAR"}',headers=None):
        handler = self.handler.__new__(self.handler)
        handler.path,handler.client_address = path,('127.0.0.1',12345)
        handler.server = SimpleNamespace(server_port=8765)
        handler.headers = Message()
        defaults = {'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','Content-Type':'application/json','Content-Length':str(len(body)),'X-CSRF-Token':getattr(self,'token','')}
        defaults.update(headers or {})
        for key,value in defaults.items():
            if value is not None:
                handler.headers[key] = value
        handler.request_version,handler.command = 'HTTP/1.1',method
        handler.requestline = method+' '+path+' HTTP/1.1'
        handler.wfile,handler.rfile = io.BytesIO(),io.BytesIO(body)
        getattr(handler,'do_'+method)()
        head,data = handler.wfile.getvalue().split(b'\r\n\r\n',1)
        return int(head.split()[1]),json.loads(data)

    def test_confirmation_origin_body_and_get_protection(self):
        for headers in ({'Origin':None},{'Origin':'http://evil.test'},{'Host':'evil.test'},{'X-CSRF-Token':'bad'},{'X-CSRF-Token':'é'},{'Content-Type':'text/plain'},{'Content-Length':'513'},{'Content-Length':'+19'},{'Transfer-Encoding':'chunked'}):
            self.assertIn(self.request('POST',headers=headers)[0],(400,403))
        for body in (b'{}',b'{"confirm":"no"}',b'{"confirm":"CLEAR","path":"other.sqlite3"}',b'{"confirm":"NO","confirm":"CLEAR"}',b'[["confirm","CLEAR"]]'):
            self.assertEqual(self.request('POST',body=body)[0],400)
        self.assertEqual(self.request('GET','/api/clear')[0],404)
        self.assertEqual(len(Reader(self.path).samples(0,200)['samples']),1)

    def test_success_token_rotation_busy_and_failure(self):
        active = Store(self.path,{})
        self.assertEqual(self.request('POST')[0],409)
        active.close()
        with patch('netdiag.dashboard.clear_database',side_effect=sqlite3.OperationalError('private data')):
            status,result = self.request('POST')
        self.assertEqual(status,503)
        self.assertNotIn('private data',result['error'])
        self.assertEqual(self.request('GET','/api/metadata')[1]['clear']['token'],self.token)
        status,result = self.request('POST')
        self.assertEqual((status,result),(200,{'cleared':True,'collection':'stopped'}))
        self.assertNotEqual(self.request('GET','/api/metadata')[1]['clear']['token'],self.token)
        self.assertEqual(self.request('POST')[0],403)
        self.assertEqual(Reader(self.path).metadata()['sessions'],[])


class ClearCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def call_clear(self,callback):
        # The sandbox blocks asyncio's executor-shutdown wakeup socket. Use an
        # ordinary request thread and poll its completion, as the HTTP server does.
        result = concurrent.futures.Future()
        def request():
            try:
                result.set_result(callback())
            except BaseException as error:
                result.set_exception(error)
        thread = threading.Thread(target=request,daemon=True)
        thread.start()
        while not result.done():
            await asyncio.sleep(.005)
        thread.join(timeout=1)
        return result.result()

    async def exercise(self,fail=False,stop_during_clear=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'data.sqlite3'
            store = Store(path,{})
            old = store.target('old','icmp','fixture',.2)
            store.append([{'target_id':old,'ts':1,'success':True,'latency_ms':1}])
            store.close()
            config = load_config()
            config.update(db=str(path),remote=None,services=[{'name':'current','url':'https://fixture.test/'}],public_interval=.05,timeout=.2)
            stop,started = asyncio.Event(),asyncio.Event()
            callbacks = []
            server = MagicMock()
            def create(*args,**kwargs):
                callbacks.append(kwargs['clear_callback'])
                return server
            async def probe(*args):
                started.set()
                await asyncio.sleep(.08)
                return {'success':True,'latency_ms':80}
            with patch('netdiag.dashboard.create_server',side_effect=create),patch('netdiag.probes.dns',side_effect=probe),patch('netdiag.probes.tcp',side_effect=probe),patch('netdiag.probes.http',side_effect=probe),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                task = asyncio.create_task(collect(config,stop=stop))
                await asyncio.wait_for(started.wait(),1)
                if fail:
                    with patch('netdiag.storage.Store.clear',side_effect=sqlite3.OperationalError('fixture')):
                        with self.assertRaises(sqlite3.OperationalError):
                            await asyncio.wait_for(self.call_clear(callbacks[0]),2)
                elif stop_during_clear:
                    request = asyncio.create_task(self.call_clear(callbacks[0]))
                    await asyncio.sleep(.005)
                    stop.set()
                    with self.assertRaises(ClearBusy):
                        await asyncio.wait_for(request,2)
                else:
                    await asyncio.wait_for(self.call_clear(callbacks[0]),2)
                if not stop_during_clear:
                    await asyncio.sleep(.16)
                    stop.set()
                await asyncio.wait_for(task,2)
            rows = Reader(path).samples(0,1e12,limit=1000)['samples']
            metadata = Reader(path).metadata()
            if fail or stop_during_clear:
                self.assertTrue(any(row['ts']==1 for row in rows))
                self.assertGreater(len(rows),1)
            else:
                self.assertEqual(len(metadata['sessions']),1)
                self.assertEqual({t['name'] for t in metadata['targets']},{'current'})
                self.assertTrue(rows)
                self.assertTrue(all(row['ts']>=metadata['sessions'][0]['started'] for row in rows))
                self.assertEqual(metadata['sessions'][0]['config'],config)
            server.shutdown.assert_called_once()

    async def test_clear_with_inflight_probes_resumes_without_old_results(self):
        await self.exercise()

    async def test_failed_clear_keeps_results_and_resumes(self):
        await self.exercise(fail=True)

    async def test_stopping_does_not_strand_clear_request(self):
        await self.exercise(stop_during_clear=True)


if __name__=='__main__':
    unittest.main()
