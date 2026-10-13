import asyncio
import os
import socket
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from netdiag.protocol import FRAME_SIZE, derive_key, load_key, make_request, make_response, parse_frame, verify_response
from netdiag.remote import EchoService, serve


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
            self.assertEqual(load_key(), derive_key(KEY.decode()))

    def test_passphrase_derivation_and_authentication(self):
        with patch.dict(os.environ,{'NETDIAG_KEY':'tiny'}):
            server_key = load_key()
        client_key = derive_key('tiny')
        self.assertEqual(len(client_key),32)
        self.assertEqual(client_key,server_key)
        request = make_request(client_key)
        verify_response(make_response(request,server_key),request,client_key)
        with self.assertRaises(ValueError):
            parse_frame(request,derive_key('other'),b'Q')
        with self.assertRaises(ValueError):
            parse_frame(request,b'tiny',b'Q')
        self.assertNotEqual(derive_key(' tiny '),client_key)
        self.assertNotEqual(derive_key('é'),derive_key('e\u0301'))
        for value in ('',None,'private-\ud800'):
            with self.assertRaises(ValueError) as error:
                derive_key(value)
            self.assertNotIn('private',str(error.exception))
        with patch('netdiag.protocol.hashlib.pbkdf2_hmac',return_value=b'k'*32) as derive:
            key = derive_key(' one ')
            derive.assert_called_once_with('sha256',b' one ',b'network-diagnostics/auth-key/v1',600_000,dklen=32)
            request = make_request(key)
            verify_response(make_response(request,key),request,key)
            self.assertEqual(derive.call_count,1)

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


class RemoteShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.server = asyncio.base_events.Server(self.loop, [], None, None, 64, None)
        self.service = EchoService(KEY)
        self.writer = MagicMock()
        self.writer.wait_closed = AsyncMock()
        # Server tracks transports on newer Python and a count on older releases.
        if hasattr(self.server, "_clients"):
            self.server._attach(self.writer)
            self.writer.close.side_effect = lambda: self.server._detach(self.writer)
        else:
            self.server._attach()
            self.writer.close.side_effect = self.server._detach
        self.service.accept(asyncio.StreamReader(), self.writer)
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        self.server.close()
        await self.service.close()
        await self.server.wait_closed()

    async def test_cancel_closes_persistent_clients_and_udp_before_waiting(self):
        started = asyncio.Event()
        udp = MagicMock()

        async def endpoint(*args, **kwargs):
            self.service.connection_made(udp)
            started.set()
            return udp, self.service

        with patch("netdiag.remote.EchoService", return_value=self.service), \
                patch("asyncio.start_server", AsyncMock(return_value=self.server)), \
                patch.object(self.loop, "create_datagram_endpoint", side_effect=endpoint), \
                patch.object(self.server, "serve_forever", side_effect=asyncio.Event().wait), \
                patch("builtins.print"):
            # Do not let newer serve_forever implementations close clients for us.
            task = asyncio.create_task(serve(key=KEY))
            await started.wait()
            task.cancel()
            try:
                done, _ = await asyncio.wait([task], timeout=1)
                self.assertIn(task, done, "shutdown waited for clients before closing them")
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.writer.close.assert_called_once()
                udp.close.assert_called_once()
                self.assertFalse(self.service.clients)
            finally:
                if not task.done():
                    await self.service.close()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_udp_setup_failure_closes_tcp_clients(self):
        with patch("netdiag.remote.EchoService", return_value=self.service), \
                patch("asyncio.start_server", AsyncMock(return_value=self.server)), \
                patch.object(self.loop, "create_datagram_endpoint",
                             AsyncMock(side_effect=OSError("UDP bind failed"))):
            with self.assertRaisesRegex(OSError, "UDP bind failed"):
                await asyncio.wait_for(serve(key=KEY), 1)
        self.writer.close.assert_called_once()
        self.assertFalse(self.service.clients)
        self.assertEqual(self.server.sockets, ())


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
        await self.service.close()
        await self.server.wait_closed()

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
