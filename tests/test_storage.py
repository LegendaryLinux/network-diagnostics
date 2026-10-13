import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from netdiag.storage import Reader, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'data'/'test.sqlite3'
        self.store = Store(self.path,{'source':'test'})
        self.target = self.store.target('fixture','icmp','127.0.0.1',.2)

    def tearDown(self):
        if self.store:
            self.store.close()
        self.temp.cleanup()

    def test_raw_rollup_restart_and_pagination(self):
        self.store.append([{'target_id':self.target,'ts':120+i*.2,'success':i%5!=0,'error':'timeout' if i%5==0 else None,'latency_ms':10+i} for i in range(20)])
        self.store.append([{'target_id':self.target,'ts':125,'success':None,'error':'collector_gap','missed':3}])
        reader = Reader(self.path)
        short = reader.series(120,180)['series'][0]['points']
        self.assertEqual(sum(p['count'] for p in short),20)
        self.assertEqual(sum(p['failures'] for p in short),4)
        self.assertEqual(sum(p['gaps'] for p in short),3)
        long = reader.series(0,3600)['series'][0]['points'][0]
        self.assertEqual(long['count'],20)
        self.assertEqual(long['successes'],16)
        self.assertEqual(long['latency_count'],16)
        self.assertGreater(long['latency_p95_ms'],20)
        page = reader.samples(100,200,limit=7)
        second = reader.samples(100,200,limit=7,before_id=page['next_before_id'])
        self.assertFalse(set(r['id'] for r in page['samples']) & set(r['id'] for r in second['samples']))
        self.store.close()
        self.store = Store(self.path,{})
        self.assertEqual(len(reader.samples(100,200)['samples']),21)
        self.assertEqual(len(reader.metadata()['sessions']),2)

    def test_outages_censored_on_gap_and_stop(self):
        for ts,success in ((10,False),(11,False),(12,None),(13,False),(14,True),(15,False)):
            self.store.append([{'target_id':self.target,'ts':ts,'success':success,'error':'collector_gap' if success is None else None,'missed':1 if success is None else 0}])
        rows = Reader(self.path).outages(0,100)['outages']
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[-1]['end_reason'],'measurement_gap')
        self.assertEqual(rows[-1]['duration_seconds'],1)
        self.assertEqual(rows[-2]['end_reason'],'recovered')
        self.store.close()
        self.store = None
        self.assertEqual(Reader(self.path).outages(0,100)['outages'][0]['end_reason'],'session_stopped')

    def test_private_permissions_and_collector_lock(self):
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.path.parent.stat().st_mode&0o777,0o700)
        for suffix in ('-wal','-shm'):
            self.assertEqual(Path(str(self.path)+suffix).stat().st_mode&0o777,0o600)
        with self.assertRaises(RuntimeError):
            Store(self.path,{})
        self.store.close()
        self.store = None
        os.chmod(self.path,0o644)
        self.store = Store(self.path,{})
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)

    def test_existing_sidecars_are_private(self):
        self.store.close()
        self.store = None
        external = sqlite3.connect(self.path)
        external.execute('PRAGMA journal_mode=WAL')
        external.execute("UPDATE sessions SET status='fixture'")
        external.commit()
        try:
            for suffix in ('','-wal','-shm'):
                os.chmod(str(self.path)+suffix,0o644)
            self.store = Store(self.path,{})
            for suffix in ('','-wal','-shm'):
                self.assertEqual(Path(str(self.path)+suffix).stat().st_mode&0o777,0o600)
        finally:
            external.close()

    def test_symlink_rejected(self):
        link = self.path.parent/'linked.sqlite3'
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            Store(link,{})

    def test_bounds_and_index_usage(self):
        reader = Reader(self.path)
        with self.assertRaises(ValueError):
            reader.samples(0,100,limit=1001)
        with self.assertRaises(ValueError):
            reader.series(100,0)
        plan = self.store.conn.execute('EXPLAIN QUERY PLAN SELECT * FROM rollups WHERE resolution=? AND ts>=? AND ts<?',(60,0,100)).fetchall()
        self.assertTrue(any('rollups_time' in row[3] for row in plan),plan)
        plan = self.store.conn.execute('EXPLAIN QUERY PLAN SELECT * FROM samples WHERE ts>=? AND ts<? ORDER BY ts DESC,id DESC LIMIT 100',(0,100)).fetchall()
        self.assertFalse(any('TEMP B-TREE' in row[3] for row in plan),plan)

    def test_atomic_write(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append([{'target_id':self.target,'ts':1,'success':True},{'ts':None,'target_id':self.target,'success':True}])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM samples').fetchone()[0],0)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM rollups').fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
