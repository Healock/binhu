from __future__ import annotations

import asyncio
import os


LISTEN_HOST = os.getenv("INGRESS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("INGRESS_LISTEN_PORT", "48727"))
TARGET_HOST = os.getenv("INGRESS_TARGET_HOST", "receiver")
TARGET_PORT = int(os.getenv("INGRESS_TARGET_PORT", "48727"))


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


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
    done, pending = await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)
    for writer in (client_writer, upstream_writer):
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(proxy_connection, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
