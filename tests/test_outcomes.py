import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from netdiag.storage import OutcomeHistoryUnavailable, Reader, Store


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'outcomes.sqlite3'
        self.store = Store(self.path,{})
        self.target = self.store.target('First','https','https://first.test/',5)
        self.other = self.store.target('Historical','icmp','127.0.0.1',.2)
        self.reader = Reader(self.path)

    def tearDown(self):
        if self.store:
            self.store.close()
        self.temp.cleanup()

    def add_fixture(self):
        self.store.append([{'target_id':self.target,'ts':120+i,'success':True,'latency_ms':latency} for i,latency in enumerate((0,50,50.001,200,200.001,None))]+[
            {'target_id':self.target,'ts':126,'success':False,'latency_ms':1},
            {'target_id':self.target,'ts':127,'success':False,'latency_ms':999},
            {'target_id':self.target,'ts':128,'success':None,'error':'service_backoff','missed':3},
            {'target_id':self.other,'ts':239.999,'success':True,'latency_ms':50},
            {'target_id':self.target,'ts':240,'success':True,'latency_ms':201},
        ])

    def make_legacy(self):
        self.store.close()
        self.store = None
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            connection.execute('DROP TABLE outcome_counts')

    def test_exact_thresholds_denominator_filters_and_empty_bars(self):
        self.add_fixture()
        result = self.reader.outcomes(0,360,target_ids=[self.target])
        self.assertEqual(result['source'],'summaries')
        self.assertEqual(len(result['buckets']),3)
        self.assertEqual(result['buckets'][0]['total'],0)
        point = result['buckets'][1]
        self.assertEqual({k:point[k] for k in ('excellent','good','slow','failed','unknown_latency','gaps','total')},{'excellent':2,'good':2,'slow':1,'failed':2,'unknown_latency':1,'gaps':3,'total':8})
        self.assertEqual(self.reader.outcomes(120,240)['buckets'][0]['excellent'],3)
        self.assertEqual(self.reader.outcomes(120,240,[self.other])['buckets'][0]['total'],1)
        self.assertEqual(result['buckets'][2]['slow'],1)

    def test_partial_boundaries_and_legacy_readonly_agreement(self):
        self.add_fixture()
        exact = self.reader.outcomes(122,240,[self.target])
        self.assertEqual(exact['buckets'][0]['excellent'],0)
        self.assertEqual(exact['buckets'][0]['total'],6)
        self.assertEqual(exact['source'],'mixed')
        mixed = self.reader.outcomes(121,241)
        self.make_legacy()
        before = self.path.read_bytes()
        legacy = self.reader.outcomes(121,241)
        self.assertEqual(legacy['buckets'],mixed['buckets'])
        self.assertEqual(legacy['source'],'raw')
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(self.reader.outcomes(122,240,[self.target])['buckets'],exact['buckets'])

    def test_atomic_backfill_and_retry_after_interruption(self):
        self.add_fixture()
        expected = self.reader.outcomes(0,360)['buckets']
        self.make_legacy()
        with patch('netdiag.storage.add_outcomes',side_effect=sqlite3.OperationalError('fixture interrupted')),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(sqlite3.OperationalError):
                Store(self.path,{})
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='outcome_counts'").fetchone())
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM samples').fetchone()[0],11)
        with contextlib.redirect_stderr(io.StringIO()) as progress:
            self.store = Store(self.path,{})
        self.assertIn('one-time scan',progress.getvalue())
        self.assertEqual(self.reader.outcomes(0,360)['buckets'],expected)
        self.store.append([{'target_id':self.target,'ts':241,'success':False}])
        self.assertEqual(self.reader.outcomes(240,360)['buckets'][0]['failed'],1)
        self.store.close()
        self.store = None
        with patch('netdiag.storage.add_outcomes') as rebuild:
            self.store = Store(self.path,{})
            rebuild.assert_not_called()

    def test_append_rollback_covers_outcomes_and_raw(self):
        with patch('netdiag.storage.histogram_bin',side_effect=ValueError('fixture downstream failure')):
            with self.assertRaises(ValueError):
                self.store.append([{'target_id':self.target,'ts':120,'success':True,'latency_ms':50}])
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0],0)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM outcome_counts').fetchone()[0],0)

    def test_query_bounds_and_dense_legacy_pages_fail_honestly(self):
        for start,end,targets in ((0,14600,None),(10,0,None),(float('nan'),100,None),(0,120,[]),(0,120,list(range(101)))):
            with self.assertRaises(ValueError):
                self.reader.outcomes(start,end,targets)
        self.store.conn.executemany('INSERT INTO samples(session_id,target_id,ts,success,latency_ms) VALUES(?,?,?,?,?)',((self.store.session,self.target,121+i*.00001,1,10) for i in range(250001)))
        self.store.conn.commit()
        self.make_legacy()
        with self.assertRaises(OutcomeHistoryUnavailable):
            self.reader.outcomes(120,240)
        with contextlib.redirect_stderr(io.StringIO()):
            self.store = Store(self.path,{})
        self.assertEqual(self.reader.outcomes(120,240)['buckets'][0]['excellent'],250001)
        with self.assertRaises(OutcomeHistoryUnavailable):
            self.reader.outcomes(120.5,240)
        plan = self.store.conn.execute('EXPLAIN QUERY PLAN SELECT * FROM outcome_counts WHERE ts>=? AND ts<?',(120,240)).fetchall()
        self.assertTrue(any('PRIMARY KEY' in row[3] for row in plan),plan)


if __name__=='__main__':
    unittest.main()
