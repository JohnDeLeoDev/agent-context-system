'Heading sections of markdown bodies (context graph T4).\n\nA section is an ATX heading plus every line up to the next heading of the same or a\nhigher level, so it holds its subsections. Headings inside ``` and ~~~ fences are text.\nHeading text is compared the way Obsidian writes `[[note#Heading]]` anchors: whitespace\ncollapsed, case-folded. This module keeps no state: graph.headings caches `parse` per\nbody version beside the link parse. The plan and its criteria:\nget_doc("context-graph/plan.md").'
from __future__ import annotations

import re



TOC_MIN_BYTES = 16 * 1024

KINDS = ("memory", "doc", "skill", "command")




_LINE = re.compile(
    r"^ {0,3}(?:(?P<fence>`{3,}|~{3,})(?P<info>.*)"
    r"|(?P<hashes>#{1,6})(?:[ \t]+(?P<text>.*?))?(?:[ \t]+#+)?[ \t]*)$",
    re.MULTILINE)


def norm(text):
    'Heading text as compared: whitespace collapsed, case-folded.'
    return " ".join(str(text).split()).casefold()


def parse(body):
    '[(level, text, start, end)] for each heading outside fences, in document order.\n\n    `text` has its whitespace collapsed and body[start:end] is the section, subsections\n    included. An empty heading (`##` alone) ends the sections it closes but is not\n    listed, since nothing can name it. An unclosed fence runs to the end of the body.'
    levels: list[int] = []
    texts: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    running: list[int] = []  
    fence = None
    for m in _LINE.finditer(body):
        mark = m.group("fence")
        if mark:
            info = m.group("info")
            if fence is None:
                
                if not (mark[0] == "`" and "`" in info):
                    fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not info.strip():
                fence = None
            continue
        if fence is not None:
            continue
        level = len(m.group("hashes"))
        while running and levels[running[-1]] >= level:
            ends[running.pop()] = m.start()
        running.append(len(levels))
        levels.append(level)
        texts.append(" ".join((m.group("text") or "").split()))
        starts.append(m.start())
        ends.append(len(body))
    return [h for h in zip(levels, texts, starts, ends, strict=True) if h[1]]


def over_toc_min(body):
    'Is `body` over TOC_MIN_BYTES of UTF-8? Encodes only when its length cannot say.'
    n = len(body)
    return n > TOC_MIN_BYTES or (n * 4 > TOC_MIN_BYTES and len(body.encode()) > TOC_MIN_BYTES)


def toc(body, heads):
    '[[heading, level, bytes]] for `heads` of `body`, bytes covering subsections.'
    ascii_only = body.isascii()
    return [[text, level, end - start if ascii_only else len(body[start:end].encode())]
            for level, text, start, end in heads]


def shape(out, body, section, heads, label):
    "A full read's result `out`, cut to `section` or given a toc.\n\n    `heads` is a callable returning parse(body), called only when needed, so a small\n    full read never parses. With no section, a body over TOC_MIN_BYTES gains `toc`. With\n    one, `body` becomes the first matching section and `section` its heading text, and\n    `ambiguous` counts the matches when there are several. A section that matches\n    nothing is an error carrying the toc, whatever the body's size. `label` names the\n    entity in that error."
    if section is None:
        if over_toc_min(body):
            out["toc"] = toc(body, heads())
        return out
    found = heads()
    want = norm(section)
    hits = [h for h in found if h[1].casefold() == want]
    if not hits:
        return {"error": f"section '{section}' not found in {label}", "toc": toc(body, found)}
    _level, text, start, end = hits[0]
    out = {**out, "body": body[start:end], "section": text}
    if len(hits) > 1:
        out["ambiguous"] = len(hits)
    return out
