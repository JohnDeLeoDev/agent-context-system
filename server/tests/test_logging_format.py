'Every line the daemon writes carries a clock.'
import logging
import re

from agent_context import server

STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (INFO|WARNING|ERROR):[\w.-]+:")


def _lines(fmt, record):
    return fmt.format(record).split("\n")


def test_a_multi_line_message_is_stamped_on_every_line():
    fmt = server._EveryLineStamped(server._LOG_FORMAT)
    git = ("CONFLICT (content): Merge conflict in projects/x/BACKLOG.md\n"
           "Automatic merge failed; fix conflicts and then commit the result.")
    rec = logging.LogRecord("agent-context", logging.WARNING, __file__, 1,
                            "agent-context: sync cycle unhealthy — merge: %s", (git,), None)
    lines = _lines(fmt, rec)
    assert len(lines) == 2                      
    assert all(STAMP.match(line) for line in lines), lines
    assert lines[0].endswith("CONFLICT (content): Merge conflict in projects/x/BACKLOG.md")
    assert lines[1].endswith("Automatic merge failed; fix conflicts and then commit the result.")


def test_a_single_line_message_is_unchanged():
    fmt = server._EveryLineStamped(server._LOG_FORMAT)
    rec = logging.LogRecord("agent-context", logging.INFO, __file__, 1, "one line", (), None)
    lines = _lines(fmt, rec)
    assert len(lines) == 1 and STAMP.match(lines[0])


def test_a_traceback_is_stamped_too():
    fmt = server._EveryLineStamped(server._LOG_FORMAT)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("agent-context", logging.ERROR, __file__, 1, "failed",
                                (), sys.exc_info())
    lines = _lines(fmt, rec)
    assert len(lines) > 3
    assert all(STAMP.match(line) for line in lines), lines


def test_the_root_handler_carries_the_formatter_and_uvicorn_propagates_into_it():
    'uvicorn is started with log_config=None, so its loggers keep the default\n    propagate=True and reach the root handler configured at import.'
    root = logging.getLogger()
    assert any(isinstance(h.formatter, server._EveryLineStamped) for h in root.handlers)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).propagate is True
