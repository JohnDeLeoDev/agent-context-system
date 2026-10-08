"Two hooks stop a provider-issued credential from being published:\n\n  global/hooks/memory-husk-guard.py    — on the way INTO the store, where a write is\n                                         committed and pushed to four remotes in seconds\n  global/hooks/subagent-return-gate.py — on the way OUT of a worker, into the lead's\n                                         transcript\n\nThey guard different surfaces, so both must exist. But each defines the provider-prefix\nalternation independently, and today's DRY sweep confirmed the two are byte-equivalent\nonly by hand. Adding a new vendor's token shape to one would not reach the other, and\nnothing would say so.\n\nWHY A TEST AND NOT A SHARED FILE. The obvious fix is one definition both read. It is the\nwrong fix here: these are security guards that fail OPEN by design (a guard that wedges\nevery session gets switched off), so making them depend on a file at runtime adds a path\nwhere the file is missing and the credential sails through — worse than the duplication.\nA test costs nothing at runtime and catches the drift at release, since release-server.py\ngates on pytest.\n\nIf you are here because this test failed: you changed one copy. Change the other."
import re
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "global" / "hooks"
HUSK = HOOKS / "memory-husk-guard.py"
GATE = HOOKS / "subagent-return-gate.py"











pytestmark = pytest.mark.skipif(
    not (HUSK.exists() and GATE.exists()),
    reason="store hooks absent (release verifiers get only the server tree)",
)





EXPECTED_PREFIXES = ("gh[pousr]_", "sk-", "AKIA", "xox[abprs]-", "AIza")




MATCHES = [
    "ghp_" + "A" * 20,
    "ghs_" + "b" * 16,
    "sk-" + "A" * 24,
    "AKIA" + "A" * 16,
    "xoxb-" + "1" * 12,
    "AIza" + "z" * 35,
]
NON_MATCHES = [
    "a" * 40,                                  
    "5b0f8d11fd174b25ae2c0831164c0a63",        
    "B5B75545-B2E5-59A1-B327-47062408E8A2",    
    "ghp_short",                               
    "sk-tiny",
    "not-a-credential-at-all",
]


def _extract(path: Path, start: str) -> str:
    'Pull the alternation out of a file and normalize it to one line.\n\n    The two copies differ only in presentation — each wraps the alternation across\n    lines at a different point — so whitespace and quoting are stripped before\n    comparing. Anything left is a real difference.'
    text = path.read_text(encoding="utf-8")
    i = text.index(start)
    body = text[i:]
    
    end = body.index(")", body.index("AIza"))
    frag = body[: end + 1]
    frag = re.sub(r'\br"|["\']', "", frag)      
    return re.sub(r"\s+", "", frag)


def test_both_hooks_define_the_same_credential_pattern():
    'The one assertion that matters: the two copies have not drifted apart.'
    assert _extract(HUSK, r"\b(gh[pousr]_") == _extract(GATE, r"\b(gh[pousr]_"), (
        "memory-husk-guard.py and subagent-return-gate.py no longer define the same "
        "credential pattern. One was changed without the other, so a token shape is now "
        "blocked on one surface and published on the other."
    )


def test_pattern_carries_every_expected_provider_prefix():
    'A prefix silently dropped from both copies would pass the parity test above.'
    both = _extract(HUSK, r"\b(gh[pousr]_") + _extract(GATE, r"\b(gh[pousr]_")
    for prefix in EXPECTED_PREFIXES:
        assert prefix.replace(" ", "") in both, (
            f"provider prefix {prefix!r} is no longer in the credential pattern. If it "
            f"was removed deliberately, remove it from EXPECTED_PREFIXES too and say why."
        )


def _compiled():
    'The pattern as Python sees it, taken from the file rather than retyped.'
    src = GATE.read_text(encoding="utf-8")
    i = src.index("CREDENTIAL = re.compile(")
    
    
    
    frag = src[i : src.index("\n)", i) + 2]
    parts = re.findall(r'r"([^"]*)"', frag)
    assert parts, "could not read the CREDENTIAL pattern out of subagent-return-gate.py"
    return re.compile("".join(parts))


def test_pattern_matches_real_credential_shapes():
    rx = _compiled()
    for sample in MATCHES:
        assert rx.search(sample), f"{sample[:12]}... should be caught as a credential"


def test_pattern_does_not_match_shas_uuids_or_short_lookalikes():
    'False positives are what get a fail-open guard disabled.'
    rx = _compiled()
    for sample in NON_MATCHES:
        assert not rx.search(sample), (
            f"{sample!r} is not a credential; matching it would deny ordinary writes"
        )
