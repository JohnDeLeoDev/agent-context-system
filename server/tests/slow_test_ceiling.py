"This suite's whole run is about twenty seconds on twelve workers. A single test past thirty\nseconds is therefore a test waiting on a clock, and it fails here with its time, at the moment\nit is written. The ceiling is on the test's own call, not its fixtures, and it is well above\nthe slowest test the suite has (about eleven seconds), so a loaded machine does not trip it.\n\nLoaded for the suite from conftest.py (`pytest_plugins`). `AGENT_CONTEXT_TEST_CEILING` sets\nanother ceiling, in seconds, for the test of this file."
import os
import time

import pytest

CEILING_SECONDS = float(os.environ.get("AGENT_CONTEXT_TEST_CEILING") or 30.0)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    start = time.monotonic()
    result = yield                      
    took = time.monotonic() - start
    if took > CEILING_SECONDS:
        pytest.fail(
            f"{item.nodeid} ran for {took:.1f} s; the ceiling for one test is "
            f"{CEILING_SECONDS:g} s (policy). A test waits on no real clock: shorten the "
            "timeout it exercises, or stand in for the clock.", pytrace=False)
    return result
