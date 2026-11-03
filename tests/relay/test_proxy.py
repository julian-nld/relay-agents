"""The proxy in front of a service (relay.production.Proxy), with plain sockets: no podman."""

import asyncio
import socket

from relay.production import Proxy


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_client_that_closes_its_side_still_gets_the_answer():
    """A client may send its request and close its side (half-close, EOF), then read the answer:
    the proxy passes the EOF on and keeps the way back open."""
    async def run():
        async def upstream(reader, writer):
            request = await reader.read()  # all of it, until the client's EOF
            writer.write(b"got " + request)
            await writer.drain()
            writer.close()
        service = await asyncio.start_server(upstream, "127.0.0.1", 0)
        proxy = Proxy()
        proxy.target = service.sockets[0].getsockname()[1]
        port = free_port()
        await proxy.start("127.0.0.1", port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"hello")
            await writer.drain()
            writer.write_eof()
            answer = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            return answer
        finally:
            proxy.close()
            service.close()
    assert asyncio.run(run()) == b"got hello"


def test_both_ways_and_nothing_live():
    async def run():
        async def upstream(reader, writer):
            while data := await reader.read(100):
                writer.write(data.upper())
                await writer.drain()
            writer.close()
        service = await asyncio.start_server(upstream, "127.0.0.1", 0)
        proxy = Proxy()
        port = free_port()
        await proxy.start("127.0.0.1", port)
        try:
            # Nothing live: the connection is closed at once
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
            proxy.target = service.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            for word in (b"one", b"two"):
                writer.write(word)
                await writer.drain()
                assert await asyncio.wait_for(reader.read(100), 5) == word.upper()
            writer.close()
        finally:
            proxy.close()
            service.close()
    asyncio.run(run())


def test_when_the_service_is_done_the_connection_ends():
    """The service sends its answer and closes: the client sees the end, though it keeps its own
    side open (a keep-alive client doesn't hold the connection, and the service's socket, forever)."""
    async def run():
        async def upstream(reader, writer):
            writer.write(b"bye")
            await writer.drain()
            writer.close()
        service = await asyncio.start_server(upstream, "127.0.0.1", 0)
        proxy = Proxy()
        proxy.target = service.sockets[0].getsockname()[1]
        port = free_port()
        await proxy.start("127.0.0.1", port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            answer = await asyncio.wait_for(reader.read(), 5)  # (no write_eof from this side)
            writer.close()
            return answer
        finally:
            proxy.close()
            service.close()
    assert asyncio.run(run()) == b"bye"
