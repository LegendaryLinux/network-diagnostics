"""Monotonic scheduling with bounded work and explicit measurement coverage gaps."""
import asyncio
import functools
import math
import signal
import sys
import time
from urllib.parse import urlsplit

from . import probes
from .storage import Store


class ServiceBackoff:
    def __init__(self,interval):
        self.interval = interval
        self.delay = 0
        self.until = 0
        self.status = None

    def observe(self,result,now):
        if result.get('http_status') in (401,403,429):
            self.delay = min(max(self.interval,3600),max(60,2*self.interval) if not self.delay else 2*self.delay)
            wait = max(self.delay,result.get('retry_after_seconds',0))
            self.until = now+wait
            self.status = result['http_status']
            result['detail'] = (result.get('detail','')+'; ' if result.get('detail') else '')+f'service backoff {wait:g}s after HTTP {self.status}'
        elif result.get('success'):
            self.delay = self.until = 0
            self.status = None


async def collect(config, *, serve_dashboard=True, stop=None, key=None):
    if config['remote']:
        if key is None:
            from .protocol import load_key
            key = load_key()
        if not isinstance(key,bytes) or len(key)<32:
            raise ValueError('The derived authentication key must contain at least 32 bytes')
    store = Store(config['db'],config)
    stop = stop or asyncio.Event()
    server = None
    remote = None
    streams = []
    tasks = {}
    pending = []
    status = 'stopped'
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        loop.add_signal_handler(sig,stop.set)

    def add(name,kind,endpoint,interval,function,service=None):
        streams.append({'id':store.target(name,kind,endpoint,interval),'probe':function,'interval':interval,'due':loop.time(),'active':0,'service':service,'http':kind in ('http','https'),'capacity':1 if kind in ('persistent_tcp','http','https') else min(64,math.ceil(config['timeout']/interval)+2)})

    try:
        if config['gateway']:
            add('Gateway','icmp',config['gateway'],config['fast_interval'],functools.partial(probes.ping,config['gateway'],config['timeout']))
        else:
            print('No default IPv4 gateway detected: configure gateway to measure the local link.',file=sys.stderr)
        if config['remote']:
            remote = probes.Remote(config['remote']['host'],config['remote']['port'],key,config['timeout'])
            endpoint = remote.host+':'+str(remote.port)
            add('Remote','icmp',remote.host,config['fast_interval'],functools.partial(probes.ping,remote.host,config['timeout']))
            add('Remote','persistent_tcp',endpoint,config['fast_interval'],remote.persistent)
            add('Remote','udp',endpoint,config['fast_interval'],remote.udp)
        else:
            print('Remote is not configured: persistent TCP and UDP tests are disabled.',file=sys.stderr)
        for service in config['services']:
            backoff = ServiceBackoff(config['public_interval'])
            parsed = urlsplit(service['url'])
            add(service['name'],'dns',parsed.hostname,config['public_interval'],functools.partial(probes.dns,parsed.hostname,config['dns_server'],config['timeout']),backoff)
            add(service['name'],'tcp',parsed.hostname+':'+str(parsed.port or (443 if parsed.scheme=='https' else 80)),config['public_interval'],functools.partial(probes.tcp,parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80),config['timeout']),backoff)
            add(service['name'],'https' if parsed.scheme=='https' else 'http',service['url'],config['public_interval'],functools.partial(probes.http,service['url'],config['timeout']),backoff)
        if not streams:
            raise ValueError('No probe targets configured')
        if serve_dashboard:
            import threading
            from .dashboard import create_server
            server = create_server(config['db'],port=config['dashboard_port'])
            threading.Thread(target=server.serve_forever,daemon=True).start()
            print('Dashboard: http://127.0.0.1:'+str(config['dashboard_port']),flush=True)
        print('Collecting into '+config['db']+'; Ctrl+C stops and flushes results.',flush=True)
        last_flush = loop.time()
        previous_clock,previous_wall = loop.time(),time.time()
        while not stop.is_set():
            now = loop.time()
            wall = time.time()
            clock_difference = wall-previous_wall-(now-previous_clock)
            if abs(clock_difference)>1:
                for stream in streams:
                    pending.append({'target_id':stream['id'],'ts':wall,'success':None,'error':'collector_gap','missed':max(1,int(abs(clock_difference)/stream['interval'])),'detail':'system suspend or wall clock adjustment: '+str(round(clock_difference,3))+' seconds'})
            previous_clock,previous_wall = now,wall
            for task,stream in list(tasks.items()):
                if task.done():
                    tasks.pop(task)
                    stream['active'] -= 1
                    result = task.result()
                    if stream['http']:
                        stream['service'].observe(result,now)
                    pending.append({'target_id':stream['id'],**result})
            for stream in streams:
                if now < stream['due']:
                    continue
                missed = max(0,int((now-stream['due'])//stream['interval']))
                if missed:
                    pending.append({'target_id':stream['id'],'ts':time.time()-(now-stream['due']),'success':None,'error':'collector_gap','missed':missed,'detail':'scheduler delayed'})
                    stream['due'] += missed*stream['interval']
                if stream['service'] and now<stream['service'].until:
                    pending.append({'target_id':stream['id'],'ts':time.time(),'success':None,'error':'service_backoff','missed':1,'detail':f"intentionally skipped after HTTP {stream['service'].status}; {stream['service'].until-now:g}s cooldown remaining"})
                elif stream['active']>=stream['capacity'] or len(tasks)>=256:
                    pending.append({'target_id':stream['id'],'ts':time.time(),'success':None,'error':'collector_gap','missed':1,'detail':'previous probe still pending or concurrency limit reached'})
                else:
                    task = asyncio.create_task(probes.measure(stream['probe'],config['timeout']))
                    task.netdiag_started = time.time()
                    tasks[task] = stream
                    stream['active'] += 1
                stream['due'] += stream['interval']
            if now-last_flush>=1 or len(pending)>=1000:
                # Keep outcomes in timestamp order even when earlier requests time out.
                # This preserves failed-run chronology across batched commits.
                cutoff = min((task.netdiag_started for task in tasks),default=float('inf'))
                ready = [row for row in pending if row['ts']<cutoff]
                store.append(sorted(ready,key=lambda row:row['ts']))
                pending[:] = [row for row in pending if row['ts']>=cutoff]
                last_flush = now
            try:
                await asyncio.wait_for(stop.wait(),min(0.05,max(0.001,min(stream['due'] for stream in streams)-loop.time())))
            except asyncio.TimeoutError:
                pass
    except BaseException:
        status = 'failed'
        raise
    finally:
        # Finish bounded pending probes on normal stop; preserve outcomes before closing.
        if tasks:
            results = await asyncio.gather(*tasks,return_exceptions=True)
            for (task,stream),result in zip(tasks.items(),results):
                if isinstance(result,dict):
                    if stream['http']:
                        stream['service'].observe(result,loop.time())
                    pending.append({'target_id':stream['id'],**result})
                else:
                    pending.append({'target_id':stream['id'],'ts':time.time(),'success':None,'error':'collector_gap','missed':1,'detail':'probe task interrupted'})
        try:
            store.append(sorted(pending,key=lambda row:row['ts']))
        except Exception:
            status = 'failed'
            print('FATAL: storage write failed; '+str(len(pending))+' collected results could not be saved. Collection stopped.',file=sys.stderr,flush=True)
            raise
        finally:
            if remote:
                await remote.close()
            if server:
                server.shutdown()
                server.server_close()
            store.close(status)
            for sig in (signal.SIGINT,signal.SIGTERM):
                loop.remove_signal_handler(sig)
