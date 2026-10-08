'A recorded pass is keyed on what the verdict depends on. Server tests execute some\nglobal/scripts and global/hooks files, so those are inputs too: a change to\ntoken-usage-collect.py broke test_token_collector while the key, taken over server/ alone,\nstill matched the recorded pass.'
from agent_context import gate_record


def _tree(tmp_path):
    server = tmp_path / "server"
    (server / "src" / "agent_context").mkdir(parents=True)
    (server / "src" / "agent_context" / "m.py").write_text("x = 1\n")
    (server / "tests").mkdir()
    (server / "tests" / "test_a.py").write_text(
        'SCRIPT = ROOT / "global" / "scripts" / "used.py"\n')
    scripts = tmp_path / "global" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "used.py").write_text("import helper\nstore_task.run(\"tasked\", [])\n")
    (scripts / "helper.py").write_text("h = 1\n")
    (scripts / "tasked.py").write_text("t = 1\n")
    (scripts / "unrelated.py").write_text("u = 1\n")
    return server, scripts


def test_named_scripts_and_what_they_run_are_inputs(tmp_path):
    server, _ = _tree(tmp_path)
    names = [p.name for p in gate_record.global_test_inputs(server)]
    assert names == ["helper.py", "tasked.py", "used.py"]


def test_a_change_to_a_script_the_suite_runs_changes_the_key(tmp_path):
    server, scripts = _tree(tmp_path)
    before = gate_record.gate_key(server)
    (scripts / "helper.py").write_text("h = 2\n")
    assert gate_record.gate_key(server) != before


def test_an_unrelated_script_edit_keeps_the_key(tmp_path):
    server, scripts = _tree(tmp_path)
    before = gate_record.gate_key(server)
    (scripts / "unrelated.py").write_text("u = 2\n")
    assert gate_record.gate_key(server) == before
