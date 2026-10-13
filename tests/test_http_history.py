import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from netdiag.dashboard import create_server
from netdiag.storage import HistoryUpgradeRequired, Reader, Store, clear_database


class HttpHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'history.sqlite3'
        self.store = Store(self.path,{})
        self.target = self.store.target('fixture','https','https://fixture.test/',5)
        self.other = self.store.target('transport','tcp','fixture.test:443',5)
        self.store.append([dict(target_id=self.target,ts=ts,success=success,error=error,
                                http_status=status,latency_ms=latency,dns_ms=1,connect_ms=2,tls_ms=3,
                                detail='retained diagnostic detail')
                           for ts,success,error,status,latency in (
                               (120,False,'timeout',403,10),
                               (121,False,'http_rejected',403,50),
                               (122,False,'receive_failure',403,80),
                               (123,False,'http_status',500,201),
                               (123.5,False,'timeout',None,20),
                               (124,None,'service_backoff',None,None),
                               (125,False,'http_status',404,100),
                               (126,False,'timeout',None,None),
                               (127,False,'http_rejected',429,None),
                               (128,False,'http_status',199,30),
                               (129,True,None,200,20),
                               (130,False,'http_rejected',405,201),
                               (131,False,'timeout',None,None))])
        self.store.append([dict(target_id=self.other,ts=120,success=False,error='http_status',http_status=403)])

    def tearDown(self):
        if self.store:
            self.store.close()
        self.temp.cleanup()

    def legacy(self,without_outcomes=False):
        self.store.close()
        self.store = None
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('PRAGMA user_version=0')
            if without_outcomes:
                connection.execute('DROP TABLE outcome_counts')

    def reopen(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.store = Store(self.path,{})

    def snapshot(self):
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            return {table:connection.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
                    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def test_correction_preserves_evidence_and_agrees_in_every_representation(self):
        original = Reader(self.path).samples(0,240)['samples']
        other = self.store.conn.execute('SELECT * FROM rollups WHERE target_id=?',(self.other,)).fetchall()
        self.legacy()
        self.reopen()
        reader = Reader(self.path)
        corrected = reader.samples(0,240)['samples']
        for before,after in zip(original,corrected):
            expected = dict(before)
            if before['target_id']==self.target and before['error'] in ('http_rejected','http_status') and 200<=before['http_status']<=599:
                expected.update(success=1,error=None)
            self.assertEqual(after,expected)
        self.assertEqual(self.store.conn.execute('SELECT * FROM rollups WHERE target_id=?',(self.other,)).fetchall(),other)
        for end,buckets in ((240,300),(7200,300),(172800,1)):
            points = reader.series(0,end,[self.target],buckets)['series'][0]['points']
            self.assertEqual(sum(point['successes'] for point in points),6)
            self.assertEqual(sum(point['failures'] for point in points),6)
            self.assertEqual(sum(point['latency_count'] for point in points),5)
        for start,end in ((120,240),(119,241)):
            outcome = reader.outcomes(start,end,[self.target])['buckets']
            self.assertEqual(sum(point['failed'] for point in outcome),6)
            self.assertEqual(sum(point['excellent'] for point in outcome),2)
            self.assertEqual(sum(point['good'] for point in outcome),1)
            self.assertEqual(sum(point['slow'] for point in outcome),2)
            self.assertEqual(sum(point['unknown_latency'] for point in outcome),1)
        runs = sorted(reader.outages(0,240,[self.target])['outages'],key=lambda run:run['started'])
        self.assertEqual([(row['started'],row['ended'],row['end_reason']) for row in runs],
                         [(120,121,'recovered'),(122,123,'recovered'),(123.5,123.5,'measurement_gap'),
                          (126,127,'recovered'),(128,129,'recovered'),(131,131,'session_stopped')])
        self.assertEqual(self.store.conn.execute('PRAGMA user_version').fetchone()[0],1)

    def test_cross_session_runs_keep_stop_and_interruption_boundaries(self):
        first_session = self.store.session
        self.store.close()
        self.store = Store(self.path,{})
        second_session = self.store.session
        self.store.append([dict(target_id=self.target,ts=ts,success=False,error=error,http_status=status)
                           for ts,error,status in ((140,'timeout',None),(141,'http_rejected',403),(142,'timeout',None))])
        self.legacy()
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE sessions SET ended=NULL,status='running' WHERE id=?",(second_session,))
            connection.execute('UPDATE outages SET ended=NULL,end_reason=NULL WHERE session_id=?',(second_session,))
        self.reopen()
        runs = Reader(self.path).outages(0,240,[self.target])['outages']
        self.assertIn((first_session,131,131,'session_stopped'),[(r['session_id'],r['started'],r['ended'],r['end_reason']) for r in runs])
        self.assertIn((second_session,140,141,'recovered'),[(r['session_id'],r['started'],r['ended'],r['end_reason']) for r in runs])
        self.assertIn((second_session,142,142,'session_interrupted'),[(r['session_id'],r['started'],r['ended'],r['end_reason']) for r in runs])

    def test_fault_after_rebuild_writes_rolls_back_all_history_and_allows_retry(self):
        self.legacy()
        before = self.snapshot()
        summarize = Store._summarize
        def fail_after_writes(store,rows,session_id=None):
            summarize(store,rows,session_id)
            raise KeyboardInterrupt('synthetic interruption')
        with patch.object(Store,'_summarize',fail_after_writes),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                Store(self.path,{})
        self.assertEqual(self.snapshot(),before)
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],0)
        self.reopen()  # The failed constructor must also release its collector lock.

    def test_outcome_schema_bootstrap_and_marker_roll_back_together(self):
        self.legacy(without_outcomes=True)
        before = self.snapshot()
        with patch('netdiag.storage.add_outcomes',side_effect=sqlite3.OperationalError('synthetic disk failure')),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(sqlite3.OperationalError):
                Store(self.path,{})
        self.assertEqual(self.snapshot(),before)
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],0)
        self.reopen()
        self.assertEqual(Reader(self.path).outcomes(120,240,[self.target])['buckets'][0]['failed'],6)

    def test_bounded_rebuild_idempotence_and_clear_compatibility(self):
        self.store.append([dict(target_id=self.target,ts=200+i,success=False,error='http_rejected',http_status=403,latency_ms=25) for i in range(2501)])
        self.legacy()
        sizes = []
        summarize = Store._summarize
        def observe(store,rows,session_id=None):
            sizes.append(len(rows))
            return summarize(store,rows,session_id)
        with patch.object(Store,'_summarize',observe):
            self.reopen()
        self.assertGreater(len(sizes),1)
        self.assertLessEqual(max(sizes),1000)
        self.store.close()
        self.store = None
        with patch.object(Store,'_correct_http_history',side_effect=AssertionError('must not repeat migration')):
            self.reopen()
        self.store.clear({},[self.target])
        self.assertEqual(Reader(self.path).samples(0,3000)['samples'],[])
        self.store.append([dict(target_id=self.target,ts=1,success=True,http_status=403,latency_ms=10)])
        self.assertEqual(Reader(self.path).outcomes(0,120)['buckets'][0]['excellent'],1)
        self.store.close()
        self.store = None
        clear_database(self.path)
        self.assertEqual(Reader(self.path).metadata()['sessions'],[])

    def test_readonly_legacy_gate_is_actionable_and_does_not_modify_dataset(self):
        self.legacy()
        before = self.path.read_bytes()
        reader = Reader(self.path)
        for operation in (reader.metadata,lambda:reader.samples(0,240),lambda:reader.series(0,240),
                          lambda:reader.outcomes(0,240),lambda:reader.outages(0,240)):
            with self.assertRaisesRegex(HistoryUpgradeRequired,'Start collection once'):
                operation()
        self.assertEqual(self.path.read_bytes(),before)
        with patch('netdiag.dashboard.DashboardServer') as server:
            create_server(self.path)
        handler_class = server.call_args.args[1]
        handler = handler_class.__new__(handler_class)
        handler.path = '/api/metadata'
        handler.local_request = MagicMock(return_value=True)
        handler.respond = MagicMock()
        handler.do_GET()
        self.assertEqual(handler.respond.call_args.args[0],503)
        self.assertIn('Start collection once',handler.respond.call_args.args[1]['error'])


if __name__=='__main__':
    unittest.main()
