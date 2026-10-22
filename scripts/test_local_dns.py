import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import Mock, patch

from local_dns import LocalDNS


class LocalDNSTests(unittest.TestCase):
    def test_public_lookup_is_cached_and_keeps_numeric_resolution_local(self):
        original = Mock(return_value=[('numeric-result',)])
        resolver = LocalDNS(original, ['database.example.com'])
        payload = {'Status': 0, 'Answer': [{'type': 1, 'data': '192.0.2.1', 'TTL': 60}]}
        with patch('local_dns.urllib.request.urlopen',
                   side_effect=lambda *a, **k: io.BytesIO(json.dumps(payload).encode())) as fetch:
            for _ in range(2):
                self.assertEqual(resolver.getaddrinfo('database.example.com', 3306), [('numeric-result',)])
            fetch.assert_called_once()
        original.assert_called_with('192.0.2.1', 3306, 0, 0, 0, socket.AI_NUMERICHOST)

    def test_private_names_other_sites_and_numeric_addresses_use_os(self):
        original = Mock(return_value=[])
        resolver = LocalDNS(original, ['mysql.railway.internal'])
        with patch('local_dns.urllib.request.urlopen') as fetch:
            for host in ('mysql.railway.internal', 'localhost', '192.0.2.1', 'private.example.com'):
                resolver.getaddrinfo(host, 80)
            fetch.assert_not_called()
        self.assertEqual(original.call_count, 4)

    def test_https_failure_falls_back_to_os(self):
        original = Mock(return_value=['fallback'])
        resolver = LocalDNS(original)
        with patch('local_dns.urllib.request.urlopen', side_effect=urllib.error.URLError('offline')):
            self.assertEqual(resolver.getaddrinfo('discord.com', 443), ['fallback'])
        original.assert_called_once_with('discord.com', 443, 0, 0, 0, 0)

    def test_ipv6_queries_aaaa_and_expired_cache_refreshes(self):
        resolver = LocalDNS(Mock(return_value=[]))
        payload = {'Status': 0, 'Answer': [{'type': 28, 'data': '2001:db8::1', 'TTL': 1}]}
        with patch('local_dns.urllib.request.urlopen',
                   side_effect=lambda *a, **k: io.BytesIO(json.dumps(payload).encode())) as fetch:
            with patch('local_dns.time.monotonic', return_value=10):
                resolver.getaddrinfo('discord.com', 443, socket.AF_INET6)
            with patch('local_dns.time.monotonic', return_value=12):
                resolver.getaddrinfo('discord.com', 443, socket.AF_INET6)
            self.assertEqual(fetch.call_count, 2)
            self.assertIn('type=AAAA', fetch.call_args.args[0].full_url)


if __name__ == '__main__':
    unittest.main()
