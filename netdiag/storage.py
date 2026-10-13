"""Append-only raw evidence and mergeable, multi-resolution time summaries."""
import json
import fcntl
import math
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, started REAL NOT NULL, ended REAL, config TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY, name TEXT NOT NULL, probe TEXT NOT NULL, endpoint TEXT NOT NULL, interval REAL NOT NULL, UNIQUE(name,probe,endpoint,interval));
CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY,session_id INTEGER NOT NULL,target_id INTEGER NOT NULL,ts REAL NOT NULL,success INTEGER,error TEXT,latency_ms REAL,dns_ms REAL,connect_ms REAL,tls_ms REAL,http_status INTEGER,detail TEXT,reconnected INTEGER NOT NULL DEFAULT 0,missed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS samples_time ON samples(ts,id);
CREATE INDEX IF NOT EXISTS samples_target_time ON samples(target_id,ts);
CREATE TABLE IF NOT EXISTS outages(id INTEGER PRIMARY KEY,session_id INTEGER NOT NULL,target_id INTEGER NOT NULL,started REAL NOT NULL,ended REAL,last_failure REAL NOT NULL,failures INTEGER NOT NULL,end_reason TEXT);
CREATE INDEX IF NOT EXISTS outages_time ON outages(started);
CREATE INDEX IF NOT EXISTS outages_open ON outages(session_id,target_id,ended);
CREATE TABLE IF NOT EXISTS rollups(resolution INTEGER NOT NULL,target_id INTEGER NOT NULL,ts INTEGER NOT NULL,count INTEGER NOT NULL,successes INTEGER NOT NULL,failures INTEGER NOT NULL,gaps INTEGER NOT NULL,total REAL NOT NULL,maximum REAL,hist TEXT NOT NULL,reconnected INTEGER NOT NULL,PRIMARY KEY(resolution,target_id,ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS rollups_time ON rollups(resolution,ts,target_id);
"""
FIELDS = ('ts','success','error','latency_ms','dns_ms','connect_ms','tls_ms','http_status','detail','reconnected','missed')


def histogram_bin(value):
    return int(math.ceil(math.log2(max(0.125, value)) * 4))


def percentiles(hist):
    total = sum(hist.values())
    answer = []
    for fraction in (0.5, 0.95):
        cumulative = 0
        value = None
        for bucket, count in sorted((int(k), v) for k, v in hist.items()):
            cumulative += count
            if cumulative >= total * fraction:
                value = round(2 ** (bucket / 4), 3)
                break
        answer.append(value)
    return answer


class Store:
    def __init__(self, path, config):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.path.parent.stat()
        if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
            raise PermissionError('Dataset directory must be owned by you and not writable by other users')
        for suffix in ('.lock','','-wal','-shm','-journal'):
            try:
                fd = os.open(str(self.path)+suffix,os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT if suffix in ('','.lock') else 0),0o600)
            except FileNotFoundError:
                continue
            try:
                metadata = os.fstat(fd)
                if metadata.st_uid!=os.getuid() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
                    raise PermissionError('Dataset and lock must be regular single-link files owned by you')
                os.fchmod(fd,0o600)
            finally:
                os.close(fd)
        self.lock = open(str(self.path)+'.lock','a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('Another collector is already writing this dataset') from None
        self.conn = sqlite3.connect(path)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript(SCHEMA)
        # An unclean stop is evidence, never pretend it was a graceful shutdown.
        self.conn.execute("UPDATE outages SET ended=last_failure,end_reason='session_interrupted' WHERE ended IS NULL")
        self.conn.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
        self.session = self.conn.execute('INSERT INTO sessions(started,config,status) VALUES(?,?,?)', (time.time(), json.dumps(config), 'running')).lastrowid
        self.conn.commit()

    def target(self, name, probe, endpoint, interval):
        self.conn.execute('INSERT OR IGNORE INTO targets(name,probe,endpoint,interval) VALUES(?,?,?,?)', (name, probe, endpoint, interval))
        self.conn.commit()
        return self.conn.execute('SELECT id FROM targets WHERE name=? AND probe=? AND endpoint=? AND interval=?', (name, probe, endpoint, interval)).fetchone()[0]

    def append(self, rows):
        if not rows:
            return
        grouped = {}
        with self.conn:
            self.conn.executemany('INSERT INTO samples(session_id,target_id,' + ','.join(FIELDS) + ') VALUES(' + ','.join('?' for _ in range(13)) + ')', [(self.session, row['target_id'], *(row.get(field, 0 if field in ('reconnected','missed') else None) for field in FIELDS)) for row in rows])
            for row in sorted(rows,key=lambda item:item['ts']):
                active = self.conn.execute('SELECT id FROM outages WHERE session_id=? AND target_id=? AND ended IS NULL', (self.session,row['target_id'])).fetchone()
                if row.get('success') is False or row.get('success') == 0:
                    if active:
                        self.conn.execute('UPDATE outages SET last_failure=?,failures=failures+1 WHERE id=?',(row['ts'],active[0]))
                    else:
                        self.conn.execute('INSERT INTO outages(session_id,target_id,started,last_failure,failures) VALUES(?,?,?,?,1)',(self.session,row['target_id'],row['ts'],row['ts']))
                elif row.get('success') is None and active:
                    self.conn.execute("UPDATE outages SET ended=last_failure,end_reason='measurement_gap' WHERE id=?",(active[0],))
                elif row.get('success') and active:
                    self.conn.execute("UPDATE outages SET ended=?,end_reason='recovered' WHERE id=?",(max(row['ts'],self.conn.execute('SELECT last_failure FROM outages WHERE id=?',(active[0],)).fetchone()[0]),active[0]))
                for resolution in (60, 3600, 86400):
                    key = (resolution, row['target_id'], int(row['ts'] // resolution) * resolution)
                    group = grouped.setdefault(key, [0, 0, 0, 0, 0., None, {}, 0])
                    if row.get('success') is None:
                        group[3] += row.get('missed', 1)
                    else:
                        group[0] += 1
                        group[1 if row['success'] else 2] += 1
                    latency = row.get('latency_ms')
                    if latency is not None and row.get('success'):
                        group[4] += latency
                        group[5] = max(group[5] or 0, latency)
                        bucket = str(histogram_bin(latency))
                        group[6][bucket] = group[6].get(bucket, 0) + 1
                    group[7] += row.get('reconnected', 0)
            for key, group in grouped.items():
                old = self.conn.execute('SELECT count,successes,failures,gaps,total,maximum,hist,reconnected FROM rollups WHERE resolution=? AND target_id=? AND ts=?', key).fetchone()
                if old:
                    for i in (0,1,2,3,4,7):
                        group[i] += old[i]
                    group[5] = max(group[5] or 0, old[5] or 0) if group[5] is not None or old[5] is not None else None
                    for bucket, count in json.loads(old[6]).items():
                        group[6][bucket] = group[6].get(bucket, 0) + count
                group[6] = json.dumps(group[6], separators=(',', ':'))
                self.conn.execute('INSERT OR REPLACE INTO rollups VALUES(?,?,?,?,?,?,?,?,?,?,?)', (*key, *group))

    def close(self, status='stopped'):
        self.conn.execute("UPDATE outages SET ended=last_failure,end_reason='session_stopped' WHERE session_id=? AND ended IS NULL",(self.session,))
        self.conn.execute('UPDATE sessions SET ended=?,status=? WHERE id=?', (time.time(), status, self.session))
        self.conn.commit()
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.conn.close()
        self.lock.close()


class Reader:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def connect(self):
        connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection

    def metadata(self):
        with closing(self.connect()) as connection:
            targets = [dict(row) for row in connection.execute('SELECT * FROM targets ORDER BY id DESC LIMIT 1000')]
            sessions = [dict(row) for row in connection.execute('SELECT * FROM sessions ORDER BY id DESC LIMIT 100')]
            for session in sessions:
                session['config'] = json.loads(session['config'])
            first = connection.execute('SELECT ts FROM samples ORDER BY ts LIMIT 1').fetchone()
            last = connection.execute('SELECT ts FROM samples ORDER BY ts DESC LIMIT 1').fetchone()
        return {'targets': targets, 'sessions': sessions, 'storage_bytes': sum(os.path.getsize(str(self.path) + suffix) for suffix in ('','-wal','-shm') if os.path.exists(str(self.path) + suffix)), 'targets_truncated': len(targets)==1000, 'first_ts': first[0] if first else None, 'last_ts': last[0] if last else None}

    def samples(self, start, end, target_ids=None, limit=100, before_id=None):
        if not 1 <= limit <= 1000 or not math.isfinite(start) or not math.isfinite(end) or start >= end:
            raise ValueError('Invalid sample query')
        where, args = ['ts>=?', 'ts<?'], [start,end]
        if target_ids:
            where.append('target_id IN (' + ','.join('?' for _ in target_ids) + ')')
            args.extend(target_ids)
        with closing(self.connect()) as connection:
            if before_id is not None:
                cursor = connection.execute('SELECT ts FROM samples WHERE id=?',(before_id,)).fetchone()
                if not cursor:
                    raise ValueError('Unknown pagination cursor')
                where.append('(ts,id)<(?,?)')
                args.extend((cursor[0],before_id))
            rows = [dict(row) for row in connection.execute('SELECT * FROM samples WHERE ' + ' AND '.join(where) + ' ORDER BY ts DESC,id DESC LIMIT ?', (*args,limit+1))]
        return {'samples':rows[:limit], 'next_before_id': rows[limit-1]['id'] if len(rows)>limit else None}

    def series(self, start, end, target_ids=None, buckets=300):
        if not math.isfinite(start) or not math.isfinite(end) or start >= end or not 1 <= buckets <= 1000:
            raise ValueError('Invalid series query')
        if target_ids and len(target_ids)>100:
            raise ValueError('Select at most 100 probe streams')
        if target_ids is None:
            with closing(self.connect()) as connection:
                if len(connection.execute('SELECT id FROM targets LIMIT 101').fetchall())>100:
                    raise ValueError('Dataset has over 100 streams; select up to 100 for graphing')
        # Small windows may use raw rows, with a strict work cap before rollup fallback.
        if end-start <= 900:
            with closing(self.connect()) as connection:
                where,args = ['ts>=?','ts<?'],[start,end]
                if target_ids:
                    where.append('target_id IN ('+','.join('?' for _ in target_ids)+')')
                    args.extend(target_ids)
                rows = connection.execute('SELECT target_id,ts,success,latency_ms,missed,reconnected FROM samples WHERE '+' AND '.join(where)+' LIMIT 100001',args).fetchall()
            if len(rows)<=100000:
                width = max(1,math.ceil((end-start)/buckets))
                groups = {}
                for row in rows:
                    key = (row['target_id'],int(row['ts']//width)*width)
                    point = groups.setdefault(key,{'ts':key[1],'count':0,'successes':0,'failures':0,'gaps':0,'total':0.,'latency_max_ms':None,'hist':{},'reconnected':0})
                    if row['success'] is None:
                        point['gaps'] += row['missed']
                    else:
                        point['count'] += 1
                        point['successes' if row['success'] else 'failures'] += 1
                    if row['success'] and row['latency_ms'] is not None:
                        point['total'] += row['latency_ms']
                        point['latency_max_ms'] = max(point['latency_max_ms'] or 0,row['latency_ms'])
                        bucket = str(histogram_bin(row['latency_ms']))
                        point['hist'][bucket] = point['hist'].get(bucket,0)+1
                    point['reconnected'] += row['reconnected']
                return self.finish_series(groups,width,False)
        # Long windows use persistent summaries rather than reading every raw result.
        width = max(60, math.ceil((end-start)/buckets/60)*60)
        resolution = 86400 if width >= 86400 else 3600 if width >= 3600 else 60
        width = math.ceil(width/resolution)*resolution
        where, args = ['resolution=?','ts>=?','ts<?'], [resolution, int(start//resolution)*resolution,end]
        if target_ids:
            where.append('target_id IN (' + ','.join('?' for _ in target_ids) + ')')
            args.extend(target_ids)
        groups = {}
        with closing(self.connect()) as connection:
            for row in connection.execute('SELECT * FROM rollups WHERE ' + ' AND '.join(where), args):
                key = (row['target_id'], int(row['ts']//width)*width)
                point = groups.setdefault(key, {'ts':key[1], 'count':0,'successes':0,'failures':0,'gaps':0,'total':0.,'latency_max_ms':None,'hist':{},'reconnected':0})
                for field in ('count','successes','failures','gaps','total','reconnected'):
                    point[field] += row[field]
                if row['maximum'] is not None:
                    point['latency_max_ms'] = max(point['latency_max_ms'] or 0, row['maximum'])
                for bucket,count in json.loads(row['hist']).items():
                    point['hist'][bucket] = point['hist'].get(bucket,0)+count
        return self.finish_series(groups,width,True)

    @staticmethod
    def finish_series(groups,width,full_boundaries):
        result = {}
        for (target_id,_), point in sorted(groups.items()):
            count = sum(point['hist'].values())
            point['latency_count'] = count
            point['latency_avg_ms'] = point.pop('total')/count if count else None
            point['latency_p50_ms'],point['latency_p95_ms'] = percentiles(point.pop('hist'))
            result.setdefault(target_id, []).append(point)
        return {'bucket_seconds':width,'approximate_percentiles':True,'boundary_buckets_include_full_interval':full_boundaries,'series':[{'target_id':target_id,'points':points} for target_id,points in result.items()]}


    def outages(self,start,end,target_ids=None,limit=100):
        if not math.isfinite(start) or not math.isfinite(end) or start>=end or not 1<=limit<=1000:
            raise ValueError('Invalid outage query')
        where,args = ['started<?','COALESCE(ended,last_failure)>=?'],[end,start]
        if target_ids:
            where.append('target_id IN ('+','.join('?' for _ in target_ids)+')')
            args.extend(target_ids)
        with closing(self.connect()) as connection:
            rows = [dict(row) for row in connection.execute('SELECT * FROM outages WHERE '+' AND '.join(where)+' ORDER BY started DESC LIMIT ?',(*args,limit+1))]
        for row in rows:
            row['duration_seconds'] = (row['ended'] if row['ended'] is not None else row['last_failure'])-row['started']
        return {'outages':rows[:limit],'truncated':len(rows)>limit}
