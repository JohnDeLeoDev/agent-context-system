#!/usr/bin/env python3

'PreToolUse(mcp__agent-context__add_audit_observation): block a malformed observation.\n\nObservations are the seed corpus for the next /context-audit run, and that run\nfilters by `project` and cites `evidence`. Both are lost when an agent serializes\nthe whole call into the `observation` string -- writing a literal\n`</observation><parameter name="evidence">...` inside the text and leaving the real\nargument null.\n\nWhy this blocks and does not warn. A PostToolUse warning fires after the row is\nwritten, when the malformed row already needs a repair. update_audit_observation can\namend an open row, but a serialization defect is cheapest to refuse before it is\nwritten at all. Every condition below is a pure serialization defect with no\nlegitimate exception, and re-filing correctly costs one tool call, so deny is\ncheaper than the repair it replaces.\n\nRouting rule. This queue is only for defects in the context system: instructions,\nmemory, docs, skills, commands, hooks, scripts, the store itself. Project code\ndefects (a flaky test, an app bug, a deploy fault) have no owner in a context-audit\nloop and would sit in the queue until they age into unverified claims. A\nproject-scoped observation that names no context-system entity is refused and\npointed back at user, who owns the call (there is no task tracker). It is a\nheuristic, so it only blocks for scope=project and only warns at universal scope,\nwhere the store itself is usually the subject.\n\nInput (stdin JSON): { tool_name, tool_input: { observation, scope, project, evidence } }\nOutput: permissionDecision=deny + reason, a systemMessage warning, or allow silently.'
import json
import re
import sys


MARKUP_RE = re.compile(
    r"</?(observation|parameter|scope|project|evidence)>|<parameter name="
)




ENTITY_RE = re.compile(
    r"(^|[^A-Za-z0-9_-])(instructions?|memory|memories|docs?|skills?|commands?|hooks?"
    r"|scripts?|store|daemon|resolver|bootstrap|audit|digest|sync|inbox"
    r"|session[_ -]context|agent-context|get_[a-z_]+|upsert_[a-z_]+)"
    r"([^A-Za-z0-9_-]|$)",
    re.IGNORECASE,
)

ROUTING = (
    "names no context-system entity (instruction, memory, doc, skill, command, "
    "hook, script, store, daemon, resolver, bootstrap, audit, digest, sync). Audit "
    "observations are only for defects in the context system itself. A project code "
    "defect -- a flaky test, an app bug, a deploy fault -- is raised with "
    "user in the conversation; there is no task tracker (global instruction, "
    "\"Self-improvement reporting\" and \"Project task tracking\"). If this really "
    "is a context-system defect, name the entity that is wrong."
)


def add(problems, text):
    problems.append("\n  • " + text)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  

    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    obs = tool_input.get("observation") or ""
    ev = tool_input.get("evidence") or ""
    
    
    
    
    note = tool_input.get("note") or tool_input.get("resolution_note") or ""
    scope = tool_input.get("scope") or ""
    proj = tool_input.get("project") or ""

    problems = []
    names_entity = False

    if MARKUP_RE.search("%s\n%s\n%s" % (obs, note, ev)):
        add(problems,
            "the text contains tool-call markup (`</observation>`, "
            "`<parameter name=...>`). Everything after it was swallowed into the "
            "string. It never landed in its own field, which is the corruption "
            "this guard exists to stop.")

    
    
    if tool == "mcp__agent-context__add_audit_observation":
        if not ev:
            add(problems,
                "`evidence` is empty. It is the field the next audit quotes -- a "
                "finding with no evidence reads as speculation and gets dropped. "
                "Pass a file:line reference or a quoted excerpt.")

        if scope in ("universal", "project"):
            pass
        elif scope == "":
            add(problems, '`scope` is empty (expected "universal" or "project").')
        else:
            add(problems, "`scope` is \"%s\" (expected \"universal\" or \"project\")." % scope)

        
        
        if scope == "project" and not proj:
            add(problems,
                '`scope` is "project" but `project` is null, so this lands outside '
                "every project-scoped audit. Name the project.")

        names_entity = bool(ENTITY_RE.search(obs))
        if not names_entity and scope == "project" and proj:
            add(problems, "the observation %s" % ROUTING)

    if problems:
        reason = ("%s was BLOCKED:%s\n\nRe-issue the call with observation / scope / "
                   "project / evidence as separate tool arguments. Do not paste "
                   "`<parameter name=...>` markup into the observation text -- "
                   "the tool takes each field as its own argument. Nothing was "
                   "written, so there is no malformed row to clean up."
                   % (tool.rsplit("__", 1)[-1], "".join(problems)))
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
        return 0

    if not names_entity and scope == "universal":
        msg = ("audit-observation-guard: this universal-scope observation %s It was "
               "allowed because universal findings are usually about the store "
               "itself -- if this one is a project code defect, resolve it with "
               "a routing note." % ROUTING)
        json.dump({"systemMessage": msg}, sys.stdout)
        return 0

    json.dump({"suppressOutput": True}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("audit-observation-guard crashed and failed open: %r\n" % (exc,))
        sys.exit(0)
