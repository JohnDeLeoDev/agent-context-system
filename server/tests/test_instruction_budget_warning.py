'The instruction layer reached 30% over its ceiling with no signal at any single edit:\nthe budget was measured only by `check_integrity`, which runs when overdue. The session\nthat filed #275 added ~900 bytes to Global Agent Instructions and was told nothing.\n\nMemory rows already warned on write (`_index_budget_warning`); instruction bodies are\nthe more expensive half — they are never trimmed by the runtime — and had no check.'
from agent_context import index as IX
from agent_context import memory as M

BUDGET = IX._ALWAYS_INSTR_BUDGET


def _add(store, title, size, load_behavior="always"):
    return M.upsert_instruction(store, title, "x" * size, load_behavior=load_behavior)


def test_no_warning_while_under_budget(store):
    res = _add(store, "Small", 100)
    assert not res.get("warning")


def test_warning_on_the_write_that_crosses(store):
    _add(store, "Base", BUDGET - 500)
    res = _add(store, "Straw", 1000)
    w = res.get("warning")
    assert w, "expected a warning on the write that crossed the budget"
    assert w.startswith("this write PUT"), w
    assert str(BUDGET) in w


def test_later_write_says_it_left_rather_than_put(store):
    _add(store, "Base", BUDGET + 1000)
    res = _add(store, "Another", 50)
    w = res.get("warning")
    assert w and w.startswith("this write left"), w


def test_edit_body_warns_too(store):
    
    
    M.upsert_instruction(store, "Doc", "ANCHOR\n" + "x" * (BUDGET - 200))
    res = M.edit_instruction_body(store, "Doc", "ANCHOR", "y" * 900)
    w = res.get("warning")
    assert w, "edit_body must warn as well as upsert_instruction"
    assert w.startswith("this write PUT"), w


def test_lazy_instructions_are_not_counted(store):
    _add(store, "Lazy giant", BUDGET * 2, load_behavior="lazy")
    assert IX._always_instruction_bytes(store, "global") == 0
    assert not IX._instruction_budget_warning(store, "global")


def test_scopes_are_budgeted_apart(store):
    _add(store, "Global big", BUDGET + 500)
    assert IX._instruction_budget_warning(store, "global")
    
    assert IX._always_instruction_bytes(store, "project:Nope") == 0
    assert not IX._instruction_budget_warning(store, "project:Nope")


def test_warning_reports_the_real_overage(store):
    _add(store, "Over", BUDGET + 250)
    w = IX._instruction_budget_warning(store, "global")
    assert str(BUDGET + 250) in w
    assert "250 over" in w


def test_existing_warning_is_preserved(store):
    'The budget warning must append to a warning already on the result, not replace it.'
    res = {"warning": "prior"}
    _add(store, "Over", BUDGET + 10)
    out = M._with_instruction_budget_warning(store, res, "global", before=0)
    assert out["warning"].startswith("prior | ")
    assert "over the" in out["warning"]
