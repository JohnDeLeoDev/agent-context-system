"The gate's failing-id parser and pytest command are not defeated by an inherited terminal\ncolor setting.\n\nFORCE_COLOR in the environment (this session's shell exports it, and a person's interactive\nshell can too) makes pytest wrap FAILED/ERROR in ANSI escapes, and `_failing_ids`'s regex,\nanchored at the line start, then matches nothing: a gate failure is reported with no test names,\nand one worktree's gate run failed outright on a mini-suite for the same reason. Two defenses:\nthe parser strips ANSI escapes before matching, and the pytest command passes --color=no so an\ninherited FORCE_COLOR cannot re-enable color regardless."
from __future__ import annotations

from agent_context import daemon

PLAIN = ("FAILED tests/test_x.py::test_y - assert 1 == 0\n"
         "ERROR tests/test_x.py::test_z - Error\n")
COLORED = ("\x1b[31mFAILED\x1b[0m tests/test_x.py::test_y - assert 1 == 0\n"
           "\x1b[31mERROR\x1b[0m tests/test_x.py::test_z - Error\n")


def test_failing_ids_reads_plain_output():
    assert daemon._failing_ids(PLAIN) == ["tests/test_x.py::test_y", "tests/test_x.py::test_z"]


def test_failing_ids_reads_colored_output_the_same_way():
    assert daemon._failing_ids(COLORED) == daemon._failing_ids(PLAIN)


def test_the_pytest_command_always_passes_color_no():
    assert "--color=no" in getattr(daemon, "_PYTEST_BASE_ARGS", ())
