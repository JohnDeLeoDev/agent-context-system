#!/usr/bin/env python3
'Decision-shaped phrases, defined once.\n\nLoaded by PATH with importlib (the same mechanism verification-claim-gate uses for\nverification-claim-judge), because hooks are projected into ~/.agent-context/global/hooks/ as\nloose files and share no package. A hook that cannot load this module fails OPEN,\nlike every guard here: it is worse to block every turn than to miss one phrase.\n\nPython 3.8-safe on purpose: the Synology nodes run hooks on the system 3.8.15.'
import re

FENCE = re.compile(r"```.*?```", re.S)
CODE = re.compile(r"`[^`]*`")
QUOTE = re.compile(r"^\s*>.*$", re.M)




DECISION = re.compile(
    r"""\b(
          want\s+me\s+to
        | would\s+you\s+like
        | do\s+you\s+want
        | shall\s+I\b
        | should\s+I\b
        | which\s+(one\s+)?(would|do)\s+you
        | (would\s+you\s+)?prefer\s+(that|which|me\s+to)
        | your\s+call\b
        | up\s+to\s+you\b
        | let\s+me\s+know\s+(which|whether|what|if\s+you)
        | open\s+(decision|question|item)s?\b
        | (decision|choice)s?\s+for\s+(you|later|next\s+time)
        | for\s+when\s+you(\s+are|'re)\s+next
        | deferred\s+(decision|choice)
        | tell\s+me\s+which
        | (please\s+)?(confirm|decide)\s+(whether|which|if)
    )""",
    re.I | re.X,
)


OPTION_HEADING = re.compile(r"^\s*(#+\s*)?(\*\*)?((two|three|four|your)\s+)?options\b",
                            re.I | re.M)


def visible_text(msg):
    'Prose only: fenced blocks, code spans and quoted lines are not the agent asking.'
    msg = FENCE.sub(" ", msg)
    msg = CODE.sub(" ", msg)
    return QUOTE.sub(" ", msg)


def find_decisions(msg):
    'Distinct decision phrases in the visible prose of `msg`, in order of first\n    appearance, plus the option heading if present. Empty list means no fork.'
    prose = visible_text(msg)
    seen, out = set(), []
    for m in DECISION.finditer(prose):
        a = m.group(0).strip()
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    h = OPTION_HEADING.search(prose)
    if h:
        out.append(h.group(0).strip())
    return out
