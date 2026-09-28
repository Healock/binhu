from __future__ import annotations

import asyncio
import os
from contextlib import suppress


LISTEN_HOST = os.getenv("INGRESS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("INGRESS_LISTEN_PORT", "48727"))
TARGET_HOST = os.getenv("INGRESS_TARGET_HOST", "receiver")
TARGET_PORT = int(os.getenv("INGRESS_TARGET_PORT", "48727"))


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        # The peer may disappear while the other half is still draining its
        # response.  The proxy must not turn that into a cross-connection
        # response or close the upstream before it can finish the response.
        pass
    finally:
        # A request body can finish before the response is available.  Half
        # close only the write direction so the opposite stream can continue.
        with suppress(Exception):
            writer.write_eof()


async def proxy_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except (OSError, asyncio.TimeoutError):
        client_writer.close()
        await client_writer.wait_closed()
        return

    transfers = {
        asyncio.create_task(copy_stream(client_reader, upstream_writer)),
        asyncio.create_task(copy_stream(upstream_reader, client_writer)),
    }
    await asyncio.gather(*transfers, return_exceptions=True)
    for writer in (client_writer, upstream_writer):
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(proxy_connection, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
