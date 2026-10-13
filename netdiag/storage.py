"""Append-only raw evidence and mergeable, multi-resolution time summaries."""
import json
import fcntl
import math
import os
import sqlite3
import stat
import sys
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
OUTCOME_FIELDS = ('excellent','good','slow','failed','unknown_latency','gaps')
OUTCOME_CASES = (
    'success=1 AND latency_ms>=0 AND latency_ms<=50',
    'success=1 AND latency_ms>50 AND latency_ms<=200',
    'success=1 AND latency_ms>200 AND latency_ms<=1.7976931348623157e308',
    'success=0',
    'success=1 AND (latency_ms IS NULL OR latency_ms<0 OR latency_ms>1.7976931348623157e308)',
)


class HistoryUpgradeRequired(RuntimeError):
    """The retained dataset still uses the old HTTP failure classification."""


class OutcomeHistoryUnavailable(Exception):
    """The requested exact raw history exceeds the bounded read budget."""


def add_outcomes(connection,after_id,target_id=None):
    connection.execute(
        'INSERT INTO outcome_counts SELECT CAST(ts/120 AS INTEGER)*120,target_id,'
        + ','.join('SUM(CASE WHEN '+condition+' THEN 1 ELSE 0 END)' for condition in OUTCOME_CASES)
        + ',SUM(CASE WHEN success IS NULL THEN missed ELSE 0 END) FROM samples WHERE id>?'
        + (' AND target_id=?' if target_id is not None else '')+' GROUP BY 1,2 '
        + 'ON CONFLICT(ts,target_id) DO UPDATE SET '
        + ','.join(field+'=outcome_counts.'+field+'+excluded.'+field for field in OUTCOME_FIELDS),
        (after_id,) if target_id is None else (after_id,target_id))



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


class ClearBusy(RuntimeError):
    """A collector or another clear operation owns the dataset."""


def lock_dataset(path,create=False):
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = path.parent.stat()
    if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
        raise PermissionError('Dataset directory must be owned by you and not writable by other users')
    for suffix in ('.lock','','-wal','-shm','-journal'):
        try:
            fd = os.open(str(path)+suffix,os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT if suffix=='.lock' or (create and suffix=='') else 0),0o600)
        except FileNotFoundError:
            if suffix=='':
                raise
            continue
        try:
            metadata = os.fstat(fd)
            if metadata.st_uid!=os.getuid() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
                raise PermissionError('Dataset and lock must be regular single-link files owned by you')
            os.fchmod(fd,0o600)
        finally:
            os.close(fd)
    lock = open(str(path)+'.lock','a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ClearBusy('Another collector is already writing this dataset') from None
    return lock


def delete_history(connection):
    existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {
        'samples':{'id','session_id','target_id','ts','success','error','latency_ms'},
        'sessions':{'id','started','ended','config','status'},
        'targets':{'id','name','probe','endpoint','interval'},
        'rollups':{'resolution','target_id','ts','count','successes','failures','gaps','total','maximum','hist','reconnected'},
        'outages':{'id','session_id','target_id','started','ended','last_failure','failures'},
    }
    if 'outcome_counts' in existing:
        required['outcome_counts'] = {'ts','target_id',*OUTCOME_FIELDS}
    for table,columns in required.items():
        if table not in existing or not columns.issubset({row[1] for row in connection.execute('PRAGMA table_info('+table+')')}):
            raise ValueError('The loaded file is not a supported diagnostics dataset')
    for table in ('samples','rollups','outcome_counts','outages','sessions','targets'):
        if table in existing:
            connection.execute('DELETE FROM '+table)


def clear_database(path):
    """Clear a stopped dataset without creating a dataset, sessions, or migration work."""
    path = Path(path).absolute()
    with lock_dataset(path):
        with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=5)) as connection:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                delete_history(connection)


class Store:
    def __init__(self, path, config):
        self.path = Path(path)
        self.lock = lock_dataset(self.path,create=True)
        self.conn = sqlite3.connect(path)
        try:
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=FULL')
            self.conn.executescript(SCHEMA)
            with self.conn:
                self.conn.execute('BEGIN')
                if self.conn.execute('PRAGMA user_version').fetchone()[0]<1:
                    self._correct_http_history()
                if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_counts'").fetchone():
                    historical = self.conn.execute('SELECT 1 FROM samples LIMIT 1').fetchone()
                    if historical:
                        print('Building exact two-minute outcome summaries from retained history; collection starts after this one-time scan.',file=sys.stderr,flush=True)
                    self.conn.execute('CREATE TABLE outcome_counts(ts INTEGER NOT NULL,target_id INTEGER NOT NULL,'+','.join(field+' INTEGER NOT NULL' for field in OUTCOME_FIELDS)+',PRIMARY KEY(ts,target_id)) WITHOUT ROWID')
                    add_outcomes(self.conn,0)
                    if historical:
                        print('Outcome summaries ready.',file=sys.stderr,flush=True)
            # An unclean stop is evidence, never pretend it was a graceful shutdown.
            self.conn.execute("UPDATE outages SET ended=last_failure,end_reason='session_interrupted' WHERE ended IS NULL")
            self.conn.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
            self.session = self.conn.execute('INSERT INTO sessions(started,config,status) VALUES(?,?,?)', (time.time(), json.dumps(config), 'running')).lastrowid
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            self.conn.close()
            self.lock.close()
            raise

    def _correct_http_history(self):
        condition = "success=0 AND error IN ('http_rejected','http_status') AND typeof(http_status)='integer' AND http_status BETWEEN 200 AND 599"
        outcomes = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_counts'").fetchone()
        for (target,) in self.conn.execute("SELECT id FROM targets WHERE probe IN ('http','https') AND EXISTS(SELECT 1 FROM samples WHERE target_id=targets.id AND "+condition+")"):
            print('Updating HTTP reachability history for stream '+str(target)+'; collection starts after this one-time correction.',file=sys.stderr,flush=True)
            self.conn.execute('UPDATE samples SET success=1,error=NULL WHERE target_id=? AND '+condition,(target,))
            self.conn.execute('DELETE FROM rollups WHERE target_id=?',(target,))
            self.conn.execute('DELETE FROM outages WHERE target_id=?',(target,))
            cursor = self.conn.execute('SELECT session_id,target_id,'+','.join(FIELDS)+' FROM samples WHERE target_id=? ORDER BY ts,id',(target,))
            while rows := cursor.fetchmany(1000):
                self._summarize([dict(zip(('session_id','target_id',*FIELDS),row)) for row in rows])
            self.conn.execute("UPDATE outages SET ended=last_failure,end_reason=CASE WHEN (SELECT status FROM sessions WHERE id=session_id) IN ('running','interrupted') THEN 'session_interrupted' ELSE 'session_stopped' END WHERE target_id=? AND ended IS NULL",(target,))
            if outcomes:
                self.conn.execute('DELETE FROM outcome_counts WHERE target_id=?',(target,))
                add_outcomes(self.conn,0,target)
        self.conn.execute('PRAGMA user_version=1')

    def target(self, name, probe, endpoint, interval):
        self.conn.execute('INSERT OR IGNORE INTO targets(name,probe,endpoint,interval) VALUES(?,?,?,?)', (name, probe, endpoint, interval))
        self.conn.commit()
        return self.conn.execute('SELECT id FROM targets WHERE name=? AND probe=? AND endpoint=? AND interval=?', (name, probe, endpoint, interval)).fetchone()[0]

    def append(self, rows):
        if not rows:
            return
        with self.conn:
            previous_id = self.conn.execute('SELECT COALESCE(MAX(id),0) FROM samples').fetchone()[0]
            self.conn.executemany('INSERT INTO samples(session_id,target_id,' + ','.join(FIELDS) + ') VALUES(' + ','.join('?' for _ in range(13)) + ')', [(self.session, row['target_id'], *(row.get(field, 0 if field in ('reconnected','missed') else None) for field in FIELDS)) for row in rows])
            add_outcomes(self.conn,previous_id)
            self._summarize(rows,self.session)

    def _summarize(self,rows,session_id=None):
        grouped = {}
        for row in sorted(rows,key=lambda item:item['ts']):
            session = session_id if session_id is not None else row['session_id']
            active = self.conn.execute('SELECT id FROM outages WHERE session_id=? AND target_id=? AND ended IS NULL', (session,row['target_id'])).fetchone()
            if row.get('success') is False or row.get('success') == 0:
                if active:
                    self.conn.execute('UPDATE outages SET last_failure=?,failures=failures+1 WHERE id=?',(row['ts'],active[0]))
                else:
                    self.conn.execute('INSERT INTO outages(session_id,target_id,started,last_failure,failures) VALUES(?,?,?,?,1)',(session,row['target_id'],row['ts'],row['ts']))
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

    def clear(self,config,target_ids):
        """Reset history atomically, retaining only this collector's active context."""
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            targets = self.conn.execute('SELECT * FROM targets WHERE id IN ('+','.join('?' for _ in target_ids)+')',target_ids).fetchall()
            delete_history(self.conn)
            self.conn.executemany('INSERT INTO targets VALUES(?,?,?,?,?)',targets)
            session = self.conn.execute('INSERT INTO sessions(started,config,status) VALUES(?,?,?)',(time.time(),json.dumps(config),'running')).lastrowid
        self.session = session

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
        if connection.execute('PRAGMA user_version').fetchone()[0]<1:
            connection.close()
            raise HistoryUpgradeRequired('Start collection once with this dataset to update HTTP reachability history; the dashboard will not display outdated failure classifications.')
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


    def outcomes(self,start,end,target_ids=None):
        if not all(math.isfinite(value) and 0<=value<=32503680000 for value in (start,end)) or start>=end:
            raise ValueError('Invalid outcome interval')
        first,last = int(start//120)*120,math.ceil(end/120)*120
        if (last-first)//120>121 or (target_ids is not None and not 1<=len(target_ids)<=100):
            raise ValueError('Outcome query exceeds page or target bounds')
        points = {ts:{'ts':ts,'end':ts+120,**dict.fromkeys(OUTCOME_FIELDS,0)} for ts in range(first,last,120)}
        with closing(self.connect()) as connection:
            connection.execute('BEGIN')  # Counts and partial edges share one consistent snapshot.
            if target_ids is None and len(connection.execute('SELECT id FROM targets LIMIT 101').fetchall())>100:
                raise ValueError('Select at most 100 probe streams')
            summary = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_counts'").fetchone()
            full_start,full_end = math.ceil(start/120)*120,int(end//120)*120
            raw_ranges = [(start,end)]
            source = 'raw'
            if summary:
                source = 'summaries'
                where,args = ['ts>=?','ts<?'],[full_start,full_end]
                if target_ids:
                    where.append('target_id IN ('+','.join('?' for _ in target_ids)+')')
                    args.extend(target_ids)
                for row in connection.execute('SELECT ts,'+','.join('SUM('+field+') AS '+field for field in OUTCOME_FIELDS)+' FROM outcome_counts WHERE '+' AND '.join(where)+' GROUP BY ts',args):
                    for field in OUTCOME_FIELDS:
                        points[row['ts']][field] = row[field]
                if full_start>=full_end:
                    raw_ranges = [(start,end)] if start!=full_start or end!=full_end else []
                else:
                    raw_ranges = [(left,right) for left,right in ((start,full_start),(full_end,end)) if left<right]
                if raw_ranges:
                    source = 'mixed'
            budget = 250000
            for left,right in raw_ranges:
                where,args = ['ts>=?','ts<?'],[left,right]
                if target_ids:
                    where.append('target_id IN ('+','.join('?' for _ in target_ids)+')')
                    args.extend(target_ids)
                rows = connection.execute('SELECT ts,success,latency_ms,missed FROM samples WHERE '+' AND '.join(where)+' LIMIT ?',(*args,budget+1)).fetchall()
                if len(rows)>budget:
                    raise OutcomeHistoryUnavailable('Outcome history exceeds the exact read limit; start collection once to build summaries, or choose a shorter interval/fewer targets.')
                budget -= len(rows)
                for row in rows:
                    point = points[int(row['ts']//120)*120]
                    latency = row['latency_ms']
                    if row['success'] is None:
                        point['gaps'] += row['missed']
                    elif not row['success']:
                        point['failed'] += 1
                    elif latency is None or not math.isfinite(latency) or latency<0:
                        point['unknown_latency'] += 1
                    else:
                        point['excellent' if latency<=50 else 'good' if latency<=200 else 'slow'] += 1
        for point in points.values():
            point['total'] = sum(point[field] for field in OUTCOME_FIELDS if field!='gaps')
        return {'bucket_seconds':120,'buckets':list(points.values()),'page_start':start,'page_end':end,'source':source}


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
