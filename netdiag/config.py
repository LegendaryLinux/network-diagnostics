"""Validated probe configuration; authentication passphrases are supplied separately."""
import ipaddress
import json
import math
import socket
import struct
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SERVICES = [
    {'name':'YouTube','url':'https://www.youtube.com/'},
    {'name':'Discord','url':'https://discord.com/'},
    {'name':'Steam','url':'https://store.steampowered.com/'},
    {'name':'Crunchyroll','url':'https://www.crunchyroll.com/'},
    {'name':'Netflix','url':'https://www.netflix.com/'},
]


def gateway_address():
    try:
        for line in Path('/proc/net/route').read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1] == '00000000' and int(fields[3],16) & 2:
                return socket.inet_ntoa(struct.pack('<I', int(fields[2],16)))
    except (OSError,ValueError,IndexError):
        pass
    return None


def resolver_address():
    try:
        for line in Path('/etc/resolv.conf').read_text().splitlines():
            fields = line.split()
            if fields and fields[0] == 'nameserver' and len(fields)>1:
                if ipaddress.ip_address(fields[1]).version == 4:
                    return fields[1]
    except (OSError,ValueError):
        pass
    return None


def load_config(path=None):
    config = {'db':'data/netdiag.sqlite3','gateway':None,'remote':None,'fast_interval':0.2,'public_interval':5.0,'timeout':2.0,'dashboard_port':8765,'dns_server':None,'services':DEFAULT_SERVICES}
    if path:
        supplied = json.loads(Path(path).read_text())
        if not isinstance(supplied,dict) or set(supplied)-set(config):
            raise ValueError('Unknown configuration fields; use config.example.json')
        config.update(supplied)
    for field in ('fast_interval','public_interval','timeout'):
        if isinstance(config[field],bool) or not isinstance(config[field],(int,float)) or not math.isfinite(config[field]) or not 0.05 <= config[field] <= 3600:
            raise ValueError(field + ' must be between 0.05 and 3600 seconds')
    if config['timeout'] > 30:
        raise ValueError('timeout must not exceed 30 seconds')
    if not isinstance(config['dashboard_port'],int) or not 1 <= config['dashboard_port'] <= 65535:
        raise ValueError('dashboard_port must be 1–65535')
    if not isinstance(config['db'],str) or not config['db']:
        raise ValueError('db must be a nonempty path')
    config['gateway'] = config['gateway'] or gateway_address()
    config['dns_server'] = config['dns_server'] or resolver_address()
    for field in ('gateway','dns_server'):
        if config[field] is not None:
            if ipaddress.ip_address(config[field]).version != 4:
                raise ValueError(field + ' must be an IPv4 address')
    if config['remote']:
        remote = config['remote']
        if not isinstance(remote,dict) or set(remote)-{'host','port'} or not isinstance(remote.get('host'),str) or not remote['host'] or remote['host'].startswith('-') or len(remote['host'])>253:
            raise ValueError('remote must contain host and optional port')
        remote.setdefault('port',48123)
        if not isinstance(remote['port'],int) or not 1 <= remote['port'] <= 65535:
            raise ValueError('remote port must be 1–65535')
        if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in remote['host']):
            raise ValueError('remote host must be an IPv4 address or DNS hostname')
    if not isinstance(config['services'],list) or len(config['services'])>30:
        raise ValueError('services must contain at most 30 endpoints')
    for service in config['services']:
        if not isinstance(service,dict) or set(service)!={'name','url'} or not isinstance(service['name'],str) or not 1<=len(service['name'])<=100 or not isinstance(service['url'],str):
            raise ValueError('Each service requires name and url')
        url = urlsplit(service['url'])
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment or any(ord(c)<33 for c in service['url']):
            raise ValueError('Service URLs must be HTTP(S), without credentials, query strings or fragments')
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError('Invalid service URL port')
    return config
