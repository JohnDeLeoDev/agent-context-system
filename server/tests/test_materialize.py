'Test the materialized-content projection map.'
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from agent_context.materialize import _strip_frontmatter, build_materialized_map, read_global_doc


def _make_store(root):
    'Create a minimal store-like directory tree.'
    g = os.path.join(root, "global")
    for sub in ("hooks", "scripts", "skills", "commands", "agents", "docs"):
        os.makedirs(os.path.join(g, sub), exist_ok=True)

    
    with open(os.path.join(g, "hooks", "test-hook.sh"), "w") as f:
        f.write("#!/bin/bash\necho hi\n")

    
    with open(os.path.join(g, "scripts", "test-script.py"), "w") as f:
        f.write("print('ok')\n")

    
    os.makedirs(os.path.join(g, "skills", "my-skill"), exist_ok=True)
    with open(os.path.join(g, "skills", "my-skill", "SKILL.md"), "w") as f:
        f.write('---\n'
                'uuid: "abc-123"\n'
                'type: "skill"\n'
                'name: "my-skill"\n'
                'description: "does things"\n'
                'disable_model_invocation: true\n'
                '---\n'
                '\n'
                '## Body\n'
                'content here\n')
    
    with open(os.path.join(g, "skills", "my-skill", "helper.py"), "w") as f:
        f.write("x = 1\n")

    
    with open(os.path.join(g, "commands", "my-cmd.md"), "w") as f:
        f.write('---\n'
                'uuid: "def-456"\n'
                'type: "command"\n'
                'name: "my-cmd"\n'
                'description: "runs a cmd"\n'
                'allowed_tools: ["Bash", "Read"]\n'
                'disable_model_invocation: true\n'
                'argument_hint: "[branch]"\n'
                '---\n'
                '\n'
                'echo run\n')

    
    with open(os.path.join(g, "commands", "my-cmd-embedded.md"), "w") as f:
        f.write('---\n'
                'uuid: "def-789"\n'
                'type: "command"\n'
                'name: "my-cmd-embedded"\n'
                'description: "runs another cmd"\n'
                'allowed_tools: ["Bash"]\n'
                '---\n'
                '\n'
                '---\n'
                'description: runs another cmd\n'
                'allowed-tools: ["Bash"]\n'
                '---\n'
                '\n'
                'echo run again\n')

    
    with open(os.path.join(g, "agents", "worker.md"), "w") as f:
        f.write('---\n'
                'uuid: "ghi-789"\n'
                'type: "agent_definition"\n'
                'name: "worker"\n'
                'description: "a worker agent"\n'
                'model: "opus"\n'
                'effort: "medium"\n'
                'permission_mode: "bypassPermissions"\n'
                '---\n'
                '\n'
                'You are a worker.\n')

    
    with open(os.path.join(g, "docs", "my-doc.md"), "w") as f:
        f.write('---\n'
                'uuid: "jkl-012"\n'
                'type: "doc"\n'
                'title: "My Doc"\n'
                '---\n'
                '\n'
                'Doc body\n')

    
    with open(os.path.join(g, "hooks", "test-hook.sh.meta.toml"), "w") as f:
        f.write('type = "hook"\n')

    
    proj = os.path.join(root, "projects", "myproj")
    os.makedirs(os.path.join(proj, "commands"), exist_ok=True)
    os.makedirs(os.path.join(proj, "docs"), exist_ok=True)
    with open(os.path.join(proj, "commands", "myproj-cmd.md"), "w") as f:
        f.write('---\nuuid: "p1"\ntype: "command"\nname: "myproj-cmd"\n---\n'
                '\necho proj\n')
    with open(os.path.join(proj, "docs", "myproj-cmd.md"), "w") as f:
        f.write('---\nuuid: "p2"\ntype: "doc"\ntitle: "Myproj cmd doc"\n---\n'
                '\nbacking doc\n')


def test_map_has_expected_keys():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        m = build_materialized_map(root)
        
        assert "hooks/test-hook.sh" in m
        assert "scripts/test-script.py" in m
        
        assert "skills/my-skill/SKILL.md" in m
        assert "skills/my-skill/helper.py" in m
        
        assert "commands/my-cmd.md" in m
        
        assert "agents/worker.md" in m
        
        assert "docs/my-doc.md" not in m
        assert "docs/myproj/myproj-cmd.md" not in m
        
        assert "hooks/test-hook.sh.meta.toml" not in m


def test_hook_content_is_raw():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        m = build_materialized_map(root)
        assert m["hooks/test-hook.sh"] == "#!/bin/bash\necho hi\n"


def test_skill_frontmatter_stripped():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        m = build_materialized_map(root)
        content = m["skills/my-skill/SKILL.md"]
        
        assert "uuid" not in content
        
        assert 'name: "my-skill"' in content
        assert 'description: "does things"' in content
        assert "disable-model-invocation: true" in content
        
        assert "## Body" in content


def test_agent_frontmatter_stripped():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        m = build_materialized_map(root)
        content = m["agents/worker.md"]
        assert "uuid" not in content
        assert 'name: "worker"' in content
        assert 'model: "opus"' in content
        assert 'permissionMode: "bypassPermissions"' in content


def test_command_frontmatter_stripped():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        m = build_materialized_map(root)
        content = m["commands/my-cmd.md"]
        assert "uuid" not in content
        assert "echo run" in content


def test_command_frontmatter_reemits_harness_keys():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        m = build_materialized_map(root)
        content = m["commands/my-cmd.md"]
        assert 'description: "runs a cmd"' in content
        assert 'allowed-tools: ["Bash", "Read"]' in content
        assert "disable-model-invocation: true" in content
        assert 'argument-hint: "[branch]"' in content
        assert "name" not in content.split("---")[1]


def test_command_with_embedded_frontmatter_kept_verbatim():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        m = build_materialized_map(root)
        content = m["commands/my-cmd-embedded.md"]
        assert "uuid" not in content
        
        assert content.count("allowed-tools:") == 1
        assert "echo run again" in content


def test_doc_frontmatter_stripped():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        content = read_global_doc(root, "my-doc.md")
        assert "uuid" not in content
        assert "Doc body" in content


def test_project_backing_doc_not_bundled():
    with tempfile.TemporaryDirectory() as root:
        _make_store(root)
        m = build_materialized_map(root)
        assert not [k for k in m if k.startswith("docs/")]


def test_strip_frontmatter_no_store_block():
    
    text = "---\ntitle: hello\n---\nbody"
    assert _strip_frontmatter(text, "/some/path.md") == text


def test_strip_frontmatter_non_skill_md():
    
    text = '---\nuuid: "x"\ntype: "doc"\ntitle: "T"\n---\nbody'
    out = _strip_frontmatter(text, "/global/docs/foo.md")
    assert "uuid" not in out
    assert "body" in out
