import asyncio
import os
import socket
import time
import unittest
from unittest.mock import patch

from netdiag.protocol import FRAME_SIZE, load_key, make_request, make_response, parse_frame, verify_response
from netdiag.remote import EchoService


KEY = b"local-test-only-secret-32-bytes-long"


class ProtocolTests(unittest.TestCase):
    def test_authentication_type_and_request_matching(self):
        request = make_request(KEY, 42)
        self.assertEqual(len(request), FRAME_SIZE)
        verify_response(make_response(request, KEY), request, KEY)
        with self.assertRaises(ValueError):
            parse_frame(request, b"wrong-key", b"Q")
        with self.assertRaises(ValueError):
            verify_response(request, request, KEY)
        with self.assertRaises(ValueError):
            verify_response(make_response(make_request(KEY, 43), KEY), request, KEY)
        for invalid in (b"", request[:-1], request + b"x", b"x" * FRAME_SIZE):
            with self.assertRaises(ValueError):
                parse_frame(invalid, KEY, b"Q")

    def test_freshness(self):
        now = time.time()
        with patch("netdiag.protocol.time.time", return_value=now - 40):
            old = make_request(KEY)
        with patch("netdiag.protocol.time.time", return_value=now + 40):
            future = make_request(KEY)
        for request in (old, future):
            with self.assertRaises(ValueError):
                parse_frame(request, KEY, b"Q")

    def test_environment_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                load_key()
        with patch.dict(os.environ, {"NETDIAG_KEY": KEY.decode()}):
            self.assertEqual(load_key(), KEY)

    def test_replay_and_global_rate_bound(self):
        service = EchoService(KEY, rate=2)
        request = make_request(KEY)
        self.assertIsNotNone(service.response(request))
        self.assertIsNone(service.response(request))
        self.assertIsNone(service.response(make_request(KEY)))
        service.last_refill -= 2
        self.assertIsNotNone(service.response(make_request(KEY)))
        service.seen.clear()
        service.seen.update(((i, i), time.monotonic() + 60) for i in range(16384))
        service.tokens = 2
        self.assertIsNone(service.response(make_request(KEY)))


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError:
            self.skipTest("environment forbids loopback sockets; live TCP/UDP integration unverified")
        else:
            probe_socket.close()
        self.service = EchoService(KEY, max_clients=2, idle_timeout=0.15)
        self.server = await asyncio.start_server(self.service.accept, "127.0.0.1", 0,
                                                limit=FRAME_SIZE * 2)
        self.port = self.server.sockets[0].getsockname()[1]
        await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: self.service, local_addr=("127.0.0.1", self.port))

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await self.service.close()

    async def test_tcp_persistence_and_invalid_frame_closes(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            for sequence in range(3):
                request = make_request(KEY, sequence)
                writer.write(request)
                await writer.drain()
                verify_response(await asyncio.wait_for(reader.readexactly(FRAME_SIZE), 1), request, KEY)
            writer.write(make_request(b"wrong-key"))
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_idle_partial_and_oversize_input(self):
        for payload in (b"", b"partial", b"x" * (FRAME_SIZE * 10)):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            try:
                writer.write(payload)
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_client_limit(self):
        connections = [await asyncio.open_connection("127.0.0.1", self.port) for _ in range(3)]
        try:
            self.assertLessEqual(len(self.service.clients), 2)
            self.assertEqual(await asyncio.wait_for(connections[-1][0].read(), 1), b"")
        finally:
            for _, writer in connections:
                writer.close()
                await writer.wait_closed()

    async def test_udp_and_no_unauthenticated_or_replay_response(self):
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.connect(("127.0.0.1", self.port))
            request = make_request(KEY)
            await loop.sock_sendall(sock, request)
            verify_response(await asyncio.wait_for(loop.sock_recv(sock, 2048), 1), request, KEY)
            for invalid in (request, make_request(b"wrong-key"), b"junk", b"x" * 2048):
                await loop.sock_sendall(sock, invalid)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(loop.sock_recv(sock, 2048), 0.05)


if __name__ == "__main__":
    unittest.main()
