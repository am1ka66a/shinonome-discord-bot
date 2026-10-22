"""Opt-in DNS over HTTPS for this local bot process only."""
import ipaddress
import json
import socket
import threading
import time
import urllib.parse
import urllib.request


class LocalDNS:
    def __init__(self, original, mysql_hosts=()):
        self.original = original
        self.mysql_hosts = {h.lower().rstrip('.') for h in mysql_hosts if h}
        self.cache = {}
        self.lock = threading.Lock()

    def supports(self, host):
        if not isinstance(host, str):
            return False
        host = host.lower().rstrip('.')
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        if host.endswith(('.internal', '.local')) or '.' not in host:
            return False
        domains = ('discord.com', 'discord.gg', 'discordapp.com', 'discordapp.net')
        return host in self.mysql_hosts or any(host == d or host.endswith('.' + d) for d in domains)

    def addresses(self, host, family):
        kind = 28 if family == socket.AF_INET6 else 1
        key = (host, kind)
        with self.lock:
            entry = self.cache.get(key)
            if entry and entry[0] > time.monotonic():
                return entry[1]
            # Numeric HTTPS endpoint avoids depending on the broken OS DNS.
            # urllib retains standard TLS certificate verification.
            query = urllib.parse.urlencode({'name': host, 'type': 'AAAA' if kind == 28 else 'A'})
            request = urllib.request.Request('https://1.1.1.1/dns-query?' + query,
                                             headers={'Accept': 'application/dns-json'})
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.load(response)
            records = [r for r in payload.get('Answer', []) if r.get('type') == kind]
            if payload.get('Status') != 0 or not records:
                raise socket.gaierror(socket.EAI_NONAME, 'DNS lookup returned no addresses')
            addresses = [str(ipaddress.ip_address(r['data'])) for r in records]
            ttl = max(1, min(60, min(int(r.get('TTL', 30)) for r in records)))
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic() + ttl, addresses)
            return addresses

    def getaddrinfo(self, host, port, family=0, type=0, proto=0, flags=0):
        if not self.supports(host) or family not in (0, socket.AF_INET, socket.AF_INET6) or flags & socket.AI_NUMERICHOST:
            return self.original(host, port, family, type, proto, flags)
        try:
            addresses = self.addresses(host.lower().rstrip('.'), family)
        except (OSError, ValueError, KeyError, TypeError):
            return self.original(host, port, family, type, proto, flags)
        results = []
        for address in addresses:
            results.extend(self.original(address, port, family, type, proto, flags | socket.AI_NUMERICHOST))
        return results


def install(environ):
    if environ.get('LOCAL_DNS_OVER_HTTPS', '').lower() not in ('1', 'true', 'yes'):
        return
    hosts = [environ.get('MYSQLHOST'), environ.get('DB_HOST')]
    for name in ('MYSQL_URL', 'DATABASE_URL'):
        if environ.get(name):
            hosts.append(urllib.parse.urlparse(environ[name]).hostname)
    resolver = LocalDNS(socket.getaddrinfo, hosts)
    socket.getaddrinfo = resolver.getaddrinfo
    print('本機 Bot 已啟用專用 HTTPS DNS 解析。')
