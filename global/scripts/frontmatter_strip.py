#!/usr/bin/env python3
"Strip store-frontmatter from projected content entities.\n\nShared by home-materialize.py (the ~/.claude projection), harness-materialize.py\n(shared-skills and the Xcode commands) and project-materialize.py (the project\n.claude/ projection), so every harness sees the same bytes. The server's\nmaterialize.py mirrors this logic for the relay bundle; keep the two in step."
import os


_HARNESS_RENAME = {"allowed_tools": "allowed-tools",
                   "disable_model_invocation": "disable-model-invocation",
                   "argument_hint": "argument-hint",
                   "permission_mode": "permissionMode"}
_SKILL_KEYS = ("name", "description", "allowed_tools", "disable_model_invocation")
_COMMAND_KEYS = ("description", "allowed_tools", "disable_model_invocation", "argument_hint")
_AGENT_KEYS = ("name", "description", "tools", "model", "effort", "permission_mode")


def _harness_keep(head, keys):
    '`head` lines for `keys`, renamed to harness spelling. A `false` line is\n    dropped: it is the clear value for disable_model_invocation, and an absent key\n    means the same thing to every harness, so the two must render identically.'
    keep = []
    for l in head:
        k = l.split(":", 1)[0]
        if k not in keys or l[len(k):].strip(": ") == "false":
            continue
        keep.append(l.replace(k + ":", _HARNESS_RENAME.get(k, k) + ":", 1))
    return keep


def strip_store_text(text, path):
    "`text` with its store-frontmatter block (the one carrying `uuid:`) replaced\n    by the native frontmatter the harness reads for `path`'s kind."
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None or not any(l.startswith("uuid:") for l in lines[1:end]):
        return text  
    head = lines[1:end]
    rest = lines[end + 1:]
    while rest and rest[0].strip() == "":
        rest.pop(0)
    if rest and rest[0].strip() == "---":
        return "\n".join(rest)  
    
    
    
    
    
    
    
    if os.path.basename(path) == "SKILL.md":
        keys = _SKILL_KEYS
    elif (os.sep + "commands" + os.sep) in path:
        keys = _COMMAND_KEYS
    elif (os.sep + "agents" + os.sep) in path:
        keys = _AGENT_KEYS
    else:
        return "\n".join(rest)
    keep = _harness_keep(head, keys)
    if any(l.startswith("description:") for l in keep):
        rest = ["---"] + keep + ["---", ""] + rest
    return "\n".join(rest)


def strip_store_frontmatter(path):
    'Rewrite one staged .md file in place with strip_store_text, keeping its mtime.'
    st = os.stat(path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    out = strip_store_text(text, path)
    if out == text:
        return
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    os.utime(path, (st.st_atime, st.st_mtime))


def strip_frontmatter_tree(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, _dn, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(".md"):
                    strip_store_frontmatter(os.path.join(dp, name))
