# Tests for no-pytest-mark-asyncio-without-plugin rule.
# In projects/lakehouse, pytest-asyncio is not a BUILD dependency.
# @pytest.mark.asyncio decorated async tests silently never execute because
# the plugin that installs the asyncio event loop is not available.
# Use asyncio.run(coroutine()) inside a plain def test_...() instead.

import asyncio

import pytest


# ruleid: no-pytest-mark-asyncio-without-plugin
@pytest.mark.asyncio
async def test_bare_asyncio_marked():
    result = await some_coroutine()
    assert result == 42


# ruleid: no-pytest-mark-asyncio-without-plugin
@pytest.mark.asyncio
async def test_with_fixture(tmp_path):
    result = await some_coroutine()
    assert result is not None


# ruleid: no-pytest-mark-asyncio-without-plugin
@pytest.mark.asyncio
async def test_multi_await():
    a = await some_coroutine()
    b = await some_coroutine()
    assert a == b


# ok: no-pytest-mark-asyncio-without-plugin — plain def using asyncio.run
def test_using_asyncio_run():
    result = asyncio.run(some_coroutine())
    assert result == 42


# ok: no-pytest-mark-asyncio-without-plugin — synchronous test, no decorator needed
def test_plain_sync():
    assert 1 + 1 == 2


# ok: no-pytest-mark-asyncio-without-plugin — not a test_* function name
@pytest.mark.asyncio
async def helper_async():
    return await some_coroutine()


async def some_coroutine():
    return 42
