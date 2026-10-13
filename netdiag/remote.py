"""Bounded authenticated TCP and UDP diagnostic companion."""

import argparse
import asyncio
from collections import OrderedDict
import contextlib
import time

from .protocol import FRAME_SIZE, MAX_AGE, load_key, make_response, parse_frame


class EchoService(asyncio.DatagramProtocol):
    def __init__(self, key, max_clients=64, rate=200, idle_timeout=10):
        self.key = key
        self.max_clients = max_clients
        self.rate = rate
        self.idle_timeout = idle_timeout
        self.clients = set()
        self.seen = OrderedDict()
        self.tokens = float(rate)
        self.last_refill = time.monotonic()
        self.transport = None

    def response(self, frame):
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self.last_refill) * self.rate)
        self.last_refill = now
        if self.tokens < 1:
            return None
        self.tokens -= 1
        try:
            _, nonce, sequence, _ = parse_frame(frame, self.key, b"Q")
        except ValueError:
            return None
        # Future-dated accepted frames remain replay-blocked through their full lifetime.
        while self.seen and next(iter(self.seen.values())) < now:
            self.seen.popitem(last=False)
        identity = (nonce, sequence)
        if identity in self.seen or len(self.seen) >= 16384:
            return None
        # Insert by receipt time; expires conservatively after 2 * MAX_AGE.
        self.seen[identity] = now + 2 * MAX_AGE
        return make_response(frame, self.key)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        if len(data) == FRAME_SIZE:
            response = self.response(data)
            if response is not None:
                self.transport.sendto(response, address)

    def accept(self, reader, writer):
        if len(self.clients) >= self.max_clients:
            writer.close()
            return
        task = asyncio.create_task(self.handle_client(reader, writer))
        self.clients.add(task)
        task.add_done_callback(self.clients.discard)

    async def handle_client(self, reader, writer):
        try:
            while True:
                frame = await asyncio.wait_for(reader.readexactly(FRAME_SIZE), self.idle_timeout)
                response = self.response(frame)
                if response is None:
                    break
                writer.write(response)
                await asyncio.wait_for(writer.drain(), self.idle_timeout)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), self.idle_timeout)

    async def close(self):
        if self.transport is not None:
            self.transport.close()
        tasks = list(self.clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve(host="0.0.0.0", port=48123, *, key=None):
    service = EchoService(key if key is not None else load_key())
    server = await asyncio.start_server(service.accept, host, port, limit=FRAME_SIZE * 2,
                                        backlog=64)
    try:
        await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: service, local_addr=(host, port))
        print(f"Diagnostic companion listening on {host}:{port} TCP and UDP", flush=True)
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0", help="listen address (default: all IPv4 interfaces)")
    parser.add_argument("--port", type=int, default=48123, help="same TCP and UDP port (default: 48123)")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        key = load_key()
    except ValueError as exc:
        parser.error(str(exc))
    try:
        asyncio.run(serve(args.bind, args.port, key=key))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
