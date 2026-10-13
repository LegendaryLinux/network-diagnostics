"""Independent bounded network probes. Failures describe the tested protocol only."""
import asyncio
import errno
import json
import math
import os
import re
import secrets
import socket
import struct
import time
from urllib.parse import urlsplit


async def process(*args, timeout):
    child = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env={**{k:v for k,v in os.environ.items() if k!='NETDIAG_KEY'},'LC_ALL':'C'})
    try:
        stdout,_ = await asyncio.wait_for(child.communicate(), timeout)
        return child.returncode, stdout.decode(errors='replace')
    finally:
        if child.returncode is None:
            child.kill()
            await child.communicate()


async def ping(host, timeout):
    code, output = await process('ping','-n','-c','1','-W',str(timeout),'--',host, timeout=timeout+0.2)
    match = re.search(r'time[=<]([\d.]+)\s*ms',output)
    if code == 0 and match:
        return {'success':True,'latency_ms':float(match[1])}
    return {'success':False,'error':'icmp_no_reply' if code==1 else 'icmp_local_error'}


async def dns(host, server, timeout):
    if not server:
        return {'success':False,'error':'dns_configuration'}
    identifier = secrets.randbits(16)
    question = b''.join(bytes([len(label)])+label for label in host.rstrip('.').encode('idna').split(b'.'))+b'\x00\x00\x01\x00\x01'
    request = struct.pack('!6H',identifier,0x100,1,0,0,0)+question
    start = time.perf_counter()
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        sock.connect((server,53))
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(sock,request)
        while True:
            response = await asyncio.wait_for(loop.sock_recv(sock,4096),timeout-(time.perf_counter()-start))
            if len(response)>=12 and struct.unpack('!H',response[:2])[0]==identifier:
                break
    elapsed = (time.perf_counter()-start)*1000
    error = dns_response_error(response,host)
    if error:
        return {'success':False,'error':error,'dns_ms':elapsed}
    return {'success':True,'latency_ms':elapsed,'dns_ms':elapsed}


def dns_response_error(response,host):
    """Check the echoed question and complete DNS records, including compressed names."""
    def name_at(offset):
        labels = []
        end = None
        visited = set()
        while True:
            if offset>=len(response) or offset in visited:
                raise ValueError('Truncated name or compression loop')
            visited.add(offset)
            length = response[offset]
            if length & 0xc0 == 0xc0:
                if offset+1>=len(response):
                    raise ValueError('Truncated compression pointer')
                pointer = ((length & 0x3f)<<8)|response[offset+1]
                if pointer>=offset:
                    raise ValueError('Invalid compression pointer')
                if end is None:
                    end = offset+2
                offset = pointer
                continue
            if length & 0xc0 or offset+1+length>len(response):
                raise ValueError('Invalid DNS label')
            offset += 1
            if not length:
                return tuple(labels),end if end is not None else offset
            labels.append(response[offset:offset+length].lower())
            if sum(len(label)+1 for label in labels)>254:
                raise ValueError('DNS name too long')
            offset += length

    try:
        _,flags,questions,answers,authorities,additional = struct.unpack('!6H',response[:12])
        if not flags & 0x8000 or flags & 0x7800 or questions!=1:
            return 'dns_invalid_response'
        if flags & 0x200:
            return 'dns_truncated'
        name,offset = name_at(12)
        if name!=tuple(host.rstrip('.').encode('idna').lower().split(b'.')) or response[offset:offset+4]!=b'\x00\x01\x00\x01':
            return 'dns_invalid_response'
        offset += 4
        has_address = False
        for index in range(answers+authorities+additional):
            _,offset = name_at(offset)
            kind,record_class,_,length = struct.unpack('!HHIH',response[offset:offset+10])
            offset += 10
            if offset+length>len(response) or (kind==1 and length!=4) or (kind==28 and length!=16):
                return 'dns_invalid_response'
            if kind in (2,5,12):
                _,name_end = name_at(offset)
                if name_end!=offset+length:
                    return 'dns_invalid_response'
            if index<answers and kind==1 and record_class==1:
                has_address = True
            offset += length
        if offset!=len(response):
            return 'dns_invalid_response'
        rcode = flags & 15
        if rcode or not has_address:
            return {2:'dns_servfail',3:'dns_nxdomain',5:'dns_refused'}.get(rcode,'dns_no_answer')
    except (ValueError,struct.error,IndexError):
        return 'dns_invalid_response'
    return None


async def tcp(host,port,timeout):
    start = time.perf_counter()
    addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM,family=socket.AF_INET),timeout)
    resolved = time.perf_counter()
    reader,writer = await asyncio.wait_for(asyncio.open_connection(addresses[0][4][0],port),max(0.001,timeout-(resolved-start)))
    connected = time.perf_counter()
    writer.close()
    await asyncio.wait_for(writer.wait_closed(),0.5)
    return {'success':True,'latency_ms':(connected-start)*1000,'dns_ms':(resolved-start)*1000,'connect_ms':(connected-resolved)*1000}


async def http(url,timeout):
    # Fresh HEAD request measures endpoint reachability without downloading media.
    code,output = await process('curl','--disable','--silent','--head','--output',os.devnull,'--noproxy','*','--max-time',str(timeout),'--connect-timeout',str(timeout),'--write-out','%{json}','--',url,timeout=timeout+0.2)
    try:
        info = json.loads(output)
    except (ValueError,TypeError):
        return {'success':False,'error':'http_local_error'}
    status = int(info.get('http_code',0))
    result = {'success':code==0 and 200<=status<400,'http_status':status or None}
    for key,value in (('latency_ms',info.get('time_total')),('dns_ms',info.get('time_namelookup'))):
        if isinstance(value,(float,int)) and math.isfinite(value):
            result[key] = value*1000
    if info.get('time_connect',0)>0:
        result['connect_ms'] = max(0,info['time_connect']-info.get('time_namelookup',0))*1000
    if info.get('time_appconnect',0)>0:
        result['tls_ms'] = max(0,info['time_appconnect']-info.get('time_connect',0))*1000
    if code:
        result['error'] = {5:'dns_failure',6:'dns_failure',7:'connection_failure',28:'timeout',35:'tls_failure',60:'tls_certificate',52:'empty_response',56:'receive_failure'}.get(code,'http_transport_error')
        result['detail'] = 'curl exit '+str(code)
    elif not result['success']:
        result['error'] = 'http_rejected' if status in (401,403,429) else 'http_status'
    return result


class Remote:
    def __init__(self,host,port,key,timeout):
        self.host,self.port,self.key,self.timeout = host,port,key,timeout
        self.reader = self.writer = None
        self.ever_connected = False
        self.sequence = 0

    async def persistent(self):
        from .protocol import FRAME_SIZE,make_request,verify_response
        start = time.perf_counter()
        result = {'success':True,'reconnected':0}
        try:
            if self.writer is None:
                self.reader,self.writer = await asyncio.wait_for(asyncio.open_connection(self.host,self.port,family=socket.AF_INET),self.timeout)
                result['connect_ms'] = (time.perf_counter()-start)*1000
                result['reconnected'] = int(self.ever_connected)
                self.ever_connected = True
            request = make_request(self.key,self.sequence)
            self.sequence = (self.sequence+1) % 2**32
            sent = time.perf_counter()
            self.writer.write(request)
            await self.writer.drain()
            response = await asyncio.wait_for(self.reader.readexactly(FRAME_SIZE),max(0.001,self.timeout-(time.perf_counter()-start)))
            verify_response(response,request,self.key)
            result['latency_ms'] = (time.perf_counter()-sent)*1000
            return result
        except BaseException:
            await self.close()
            raise

    async def udp(self):
        from .protocol import make_request,verify_response
        start = time.perf_counter()
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(self.host,self.port,family=socket.AF_INET,type=socket.SOCK_DGRAM),self.timeout)
        request = make_request(self.key,secrets.randbits(32))
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.connect(addresses[0][4])
            loop = asyncio.get_running_loop()
            sent = time.perf_counter()
            await loop.sock_sendall(sock,request)
            response = await asyncio.wait_for(loop.sock_recv(sock,2048),max(0.001,self.timeout-(sent-start)))
            verify_response(response,request,self.key)
        return {'success':True,'latency_ms':(time.perf_counter()-sent)*1000,'dns_ms':(sent-start)*1000}

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(),0.5)
            except (OSError,asyncio.TimeoutError):
                pass
            self.reader = self.writer = None


async def measure(probe,timeout):
    started = time.time()
    clock = time.perf_counter()
    try:
        result = await asyncio.wait_for(probe(),timeout+0.3)
    except asyncio.TimeoutError:
        result = {'success':False,'error':'timeout'}
    except socket.gaierror:
        result = {'success':False,'error':'dns_failure'}
    except ConnectionRefusedError:
        result = {'success':False,'error':'connection_refused'}
    except (ConnectionError,asyncio.IncompleteReadError):
        result = {'success':False,'error':'connection_lost'}
    except PermissionError:
        result = {'success':False,'error':'local_permission'}
    except FileNotFoundError:
        result = {'success':False,'error':'missing_executable'}
    except ValueError:
        result = {'success':False,'error':'invalid_response'}
    except OSError as error:
        result = {'success':False,'error':'local_resource_error' if error.errno in (errno.EMFILE,errno.ENFILE,errno.ENOMEM,errno.ENOBUFS,errno.ENOSPC,errno.EAGAIN,errno.EADDRINUSE,errno.EADDRNOTAVAIL,errno.EBADF,errno.EINVAL,errno.EAFNOSUPPORT,errno.EPROTONOSUPPORT) else 'network_error','detail':'errno='+str(error.errno)}
    if result.get('error') in ('local_resource_error','local_permission','missing_executable','icmp_local_error','dns_configuration','http_local_error'):
        result['success'] = None
        result['missed'] = 1
    result['ts'] = started
    # Failure duration is useful evidence but is excluded from successful latency percentiles.
    result.setdefault('latency_ms',(time.perf_counter()-clock)*1000)
    return result
