#!/usr/bin/env python3

'PreToolUse(every store write that carries text): two guards on store writes.\nThe matcher is the one `store-prose-tools --matcher` prints, and\n`prose-guard-covers-every-store-writer` fails when it drifts from it.\n\n(1) Secret guard -- blocking for provider-prefixed tokens, warning for the fuzzy\n    heuristics. The store is a git repo that fans out to GitHub + s1/s2/ls on every\n    write (the daemon commits and pushes it, policy), so a credential written here is\n    published and lands in history where redaction cannot reach it. That is\n    irreversible, so a warning after the write is too late: the guard runs on\n    PreToolUse and denies.\n    Only the near-zero-false-positive patterns block (gh[pousr]_, sk-, AKIA, xox-,\n    AIza); the heuristics that can misfire on a hash or an example still warn and\n    allow, so a legitimate write is never wedged.\n\n(2) Husk guard -- non-blocking. The description is loaded into every session and the\n    memory-management rule says not to park "RETIRED"/"SUPERSEDED" husks there: fold\n    the durable lesson into the successor and delete the slug. It warns and does\n    not block: whether a memory is dead is a judgment call, and a\n    transitional description is sometimes correct.\n\nFleet policy: every credential lives in 1Password;\nmemories reference the item, never the value.\n\nInput (stdin JSON): { tool_name, tool_input: { slug, description, body, ... } }\nOutput: deny (hard secret), or { systemMessage } (warnings), or suppress.'
import json
import re
import sys


def _list_field(tool_input, name):
    val = tool_input.get(name)
    return val if val is not None else None


def _replacement_values(tool_input):
    out = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            replacements = edit.get("replacements")
            if not isinstance(replacements, list):
                continue
            for repl in replacements:
                if isinstance(repl, list) and len(repl) > 1:
                    out.append(repl[1])
    return out


def _join_nonnull(*values):
    parts = [v for v in values if v is not None]
    return "\n".join(str(p) for p in parts)


def _first_nonnull(*values):
    for v in values:
        if v is not None:
            return v
    return "<entity>"






PROVIDER_TOKEN_RE = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})"
)



HUSK_RE = re.compile(
    r"RETIRED|SUPERSEDED|DEPRECATED|OBSOLETE|no longer (exists|used|applies|valid)"
    r"|kept (only )?(as|for) (historical|reference)",
    re.IGNORECASE,
)

CARVEOUT_NEGATION_RE = re.compile(
    r"(no|not|without|never)\s+(an?\s+)?(exception|carve-?out|caveat)s?"
)
CARVEOUT_RE = re.compile(
    r"(^|[^a-z])(the )?(one |single |only )?(exception|carve-?out|caveat)([^a-z]|$)"
    r"|except that|does not apply (to|when)"
)

CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(api[_ -]?key|apikey|auth[_ -]?token|access[_ -]?token|secret|password|passwd)"
    r"\"?'?\s*[:=]\s*[`\"']?[A-Za-z0-9/+_-]{16,}",
    re.IGNORECASE,
)
HEX32_RE = re.compile(r"\b[a-f0-9]{32}\b")
HEX32_CONTEXT_RE = re.compile(r"md5|checksum|digest|commit [a-f0-9]{32}", re.IGNORECASE)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  

    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    desc = tool_input.get("description") or ""

    
    
    
    
    
    body = _join_nonnull(
        _list_field(tool_input, "body"),
        _list_field(tool_input, "script_body"),
        _list_field(tool_input, "new_string"),
        _list_field(tool_input, "text"),
        _list_field(tool_input, "content"),
        *_replacement_values(tool_input),
    )

    
    
    
    
    
    
    
    
    
    
    secret_text = _join_nonnull(
        _list_field(tool_input, "body"),
        _list_field(tool_input, "script_body"),
        _list_field(tool_input, "new_string"),
        _list_field(tool_input, "text"),
        _list_field(tool_input, "content"),
        _list_field(tool_input, "description"),
        _list_field(tool_input, "title"),
        _list_field(tool_input, "metadata"),
        _list_field(tool_input, "observation"),
        _list_field(tool_input, "evidence"),
        _list_field(tool_input, "note"),
        _list_field(tool_input, "resolution_note"),
        *_replacement_values(tool_input),
    )

    
    observation_id = tool_input.get("observation_id")
    obs_label = "observation #%s" % observation_id if observation_id is not None else None
    slug = _first_nonnull(
        tool_input.get("slug"),
        tool_input.get("name"),
        tool_input.get("path"),
        tool_input.get("key"),
        tool_input.get("title"),
        obs_label,
    )

    if secret_text and PROVIDER_TOKEN_RE.search(secret_text):
        reason = (
            "BLOCKED: the body of `%s` contains a provider-issued credential "
            "(GitHub / OpenAI-style / AWS / Slack / Google prefix).\n\n"
            "The agent-context store auto-commits and pushes to GitHub + s1/s2/ls "
            "on every write, so this would be published within seconds and would "
            "persist in git history on four remotes even if a later edit removed "
            "it. Redaction alone would not be sufficient -- the credential would "
            "need rotating.\n\n"
            "Nothing was written. Re-issue the call with the body referencing the "
            "1Password item. Fleet policy: every credential "
            "lives in 1Password; a store entity names the item, never the "
            "secret.\n\n"
            "If this is not a credential -- a hash, a public identifier, "
            "a redacted example -- reword it so it does not carry the provider "
            "prefix, and say so in your response."
        ) % slug
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

    
    warn_parts = []

    if tool == "mcp__agent-context__upsert_memory" and desc and HUSK_RE.search(desc):
        warn_parts.append(
            "⚠ memory `%s` is being saved with a description that marks it as "
            "dead (retired/superseded/obsolete). The memory index loads into every "
            "session -- do not park husks there. Fold any durable "
            "invariant/gotcha into its successor memory, then "
            "delete_entity('memory', slug). If it is "
            "still live, rewrite the description to drop the "
            "dead-marker wording." % slug
        )

    loaded = tool_input.get("load_behavior") or "always"
    body_neg = CARVEOUT_NEGATION_RE.sub(" ", body.lower())
    if (tool == "mcp__agent-context__upsert_memory" and loaded != "lazy"
            and desc and body
            and CARVEOUT_RE.search(body_neg)
            and "→ body" not in desc):
        warn_parts.append(
            "⚠ `%s`: the body carries an exception or carve-out, and the "
            "description does not say so. The description is all that loads into "
            "a session, so one that reads as self-contained gets acted on without "
            "the body. Either fold the "
            "exception into the description, or end the description with the "
            "marker `→ body` so a reader knows it is incomplete. If the match "
            "is incidental prose, leave it and say so." % slug
        )

    
    
    if secret_text:
        hit = ""
        if CREDENTIAL_ASSIGNMENT_RE.search(secret_text):
            hit = "a credential assignment"
        if not hit:
            m = HEX32_RE.search(body)
            if m and not HEX32_CONTEXT_RE.search(body):
                hit = "a bare 32-hex key literal"
        if hit:
            warn_parts.append(
                "\U0001f534 `%s` looks like it contains %s. The agent-context store "
                "pushes to GitHub + s1/s2/ls, so this is now published and will "
                "persist in git history even if you edit it out -- redaction "
                "alone is not sufficient. Do all three: (a) rewrite the body to "
                "reference the 1Password item, (b) tell user "
                "the credential needs rotating, (c) confirm the value is not also "
                "duplicated in another memory or doc. If this is a false positive "
                "(a hash, ID, or example), leave it and say so." % (slug, hit)
            )

    if warn_parts:
        json.dump({"systemMessage": "\n\n".join(warn_parts)}, sys.stdout)
    else:
        json.dump({"suppressOutput": True}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  
        sys.stderr.write("memory-husk-guard crashed and failed open: %r\n" % (exc,))
        sys.exit(0)
