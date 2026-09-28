import asyncio

import pytest

from app.ingress_proxy import copy_stream


class _Reader:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        await asyncio.sleep(0)
        return next(self._chunks, b"")


class _Writer:
    def __init__(self):
        self.data = bytearray()
        self.eof = False
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def write_eof(self):
        self.eof = True

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_copy_stream_half_closes_destination_without_closing_connection():
    writer = _Writer()

    await copy_stream(_Reader([b"request", b""]), writer)

    assert writer.data == b"request"
    assert writer.eof is True
    assert writer.closed is False
