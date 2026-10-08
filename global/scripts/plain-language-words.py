#!/usr/bin/env python3
'The canonical plain-language word lists. Every consumer reads this file.\n\nWhy it exists. plain-language-check.py, plain-language-sweep.py, prose-sweep.py and\nterse-judge.py all need the same lists. With a copy in each, a word added to the hook\nalone leaves the sweeps disagreeing with the guard. One definition, four readers.\n\nTwo consumer shapes. Python callers import the module. The bash hook evaluates\n`python3 plain-language-words.py --sh`, which prints the same lists as shell\nvariable assignments holding extended-regex alternations.\n\nWhat is deliberately not here. Identifiers are never scanned by anything that\nreads this file, so no list needs an exception for a descriptive function name.\nCode spans and fenced blocks are stripped by each consumer before matching, so a\nreal API spelled the British way is always safe in backticks.'

import sys


HARD = [
    r"leverag(e|es|ed|ing)",
    r"utiliz(e|es|ed|ing|ation)",
    r"robust(ly|ness)?",
    r"comprehensive(ly)?",
    r"seamless(ly)?",
    r"delv(e|es|ed|ing)",
    r"posture",
    r"paradigm",
    r"holistic",
    r"synerg(y|ies)",
    r"best-in-class",
    r"cutting-edge",
    r"off-box",
    r"barrel(s|ed|ing)?",
    r"adjudicat(e|es|ed|ing|ion|ions)",
    r"break-glass",
    r"idiomatic(ally)?", r"substantive(ly)?",
    
    r"essentially", r"fundamentally", r"basically", r"actually", r"arguably",
    r"simply", r"straightforward(ly)?", r"powerful", r"elegant(ly)?", r"very", r"quite",
    r"facilitat(e|es|ed|ing)",
    r"genuine(ly)?", r"truly", r"honest(ly)?", r"frankly", r"candidly", r"plainly",
    r"notably", r"importantly", r"crucially", r"interestingly", r"ultimately",
    r"entirely", r"precisely", r"cleanly", r"silently",
    r"hygiene", r"bespoke", r"cadence(s)?", r"orthogonal(ly)?", r"footgun(s)?", r"tractable",
    r"unasked",
    r"canonical(ly)?",
]

HARD_PHRASE = [
    r"it['’]?s worth noting",
    r"it is worth noting",
    r"it['’]?s important to note",
    r"it is important to note",
    r"at the end of the day",
    r"worth knowing about",
    
    
    
    r"\b(chang|shift|alter|reshap)\w* the shape of\b",
    r"\bthe shape of (this|the|that|our) (work|task|problem|plan|project|effort|change|approach)\b",
    r"\b(would|could|might) have (shipped|landed|slipped through|gone out) (silently|unnoticed|quietly)\b",
    r"\bshipped (silently|unnoticed|quietly)\b",
    r"\bthe kind (of \w+ )?that would have\b",
    r"\bchang\w* the (picture|calculus|equation)\b",
    r"\bthe real story\b",
    r"(^|\n)[ \t>*_#-]*checkpoint\.",
    r"\bworth your (call|decision|look|attention|input|review)\b",
    r"\b(one|two|three|a few|some) (more )?things? worth\b",
    r"\bworth (flagging|mentioning|calling out|raising)\b",
    r"\bwhen I get (there|to it|to that)\b",
    r"\bunless you(['’]?d| would) rather\b",
    r"\b(I['’]?ll|I will|let me|I want to|I should|I['’]?d) flag\b",
    r"(^|\n)[ \t>*_#-]*heads[- ]up[:!,]",
    r"\bmore on (that|this) (later|below|in a moment)\b",
    r"\bbefore I get to\b",
    
    
    r"\bin order to\b", r"\ba number of\b", r"\bin terms of\b", r"\bwith respect to\b",
    r"\bthe fact that\b", r"\bthat being said\b", r"\bneedless to say\b", r"\bthe reason is\b",
    r"\brather than\b", r"\binstead of\b", r"\bas opposed to\b", r"\bnot because\b",
    r"\b\w+, not (a |an |the )?[\w-]+[.;]",
    r"\bnot (just |merely |only |simply )?\w+( \w+)?, but\b",
    
    r"\bexactly\b(?! (once|one|two|three|four|five|zero|n\b|\d|equal))",
    r"\bfully\b(?! qualified)",
    r"\bto be (clear|frank|fair|honest)\b", r"\bfor what it['’]?s worth\b",
    r"\b(almost certainly|most likely)\b",
    
    
    r"\bI (got|had) (this|that|it) wrong\b", r"\bI want to be (careful|clear|upfront|transparent|honest)( here| with you)\b",
    r"\bthe real (bug|problem|issue|cause|fix|question|reason|answer|point|failure|defect|culprit|win|cost|concern|lesson|trap|danger|work|gap|risk)\b",
    r"\ba real (bug|problem|issue|fault|defect|risk|gap|break|finding|concern|regression|failure)\b",
    
    r"\b(by design|in practice|on purpose|by construction|for the record|net effect|in principle|going forward|bottom line|in short|in a nutshell|the gist|for good measure|it turns out)\b",
    r"\btl;?dr\b",
    
    r"\bload[- ]bearing\b", r"\bsanity[- ]check(s|ed|ing)?\b", r"\bblast radius\b",
    r"\bsharp edges?\b", r"\bmoving (parts|pieces)\b", r"\blow-hanging fruit\b",
    r"\brabbit hole\b", r"\bsmoking gun\b", r"\bsilver bullet\b",
    r"\bbelt and (braces|suspenders)\b",
    r"\b(bites?|bitten) (you|us|them|anyone|someone|later|again|hard)\b",
    
    r"\bsurfac(ed|es|ing)\b",
    r"\bsurface (the|a|an|it|this|that|them|these|those|any|every|what|errors?|failures?)\b",
    
    r"\bworth\b(?! of\b)",
    
    
    r"(^|\n)[ \t>*_#-]*(good|great|perfect|excellent|nice|awesome|alright|okay|got it)[,.!]",
    r"\blet me\b", r"\blet['’]s\b(?! encrypt)",
    r"(^|\n)[ \t>*_#-]*(now |next,? )?(I['’]ll now|I will now|time to)\b",
    r"\bgiven the (effort|time|token) budget\b", r"\bfinal report now\b",
    
    r"\bup front\b", r"\bbefore (I|we) (start|begin)\b", r"\b(one|a quick|quick) note\b",
    r"\b(two|three|a few|some|a couple of) things (you should|to) (know|note|flag|mention)\b",
    r"\byou should know\b", r"\b(keep|bear) in mind\b",
    r"(^|\n)[ \t>*_#-]*(just |I['’]m )?flagging\b", r"\bflagging (this|that|it|one|two)\b",
    
    r"\bsay the word\b", r"\bif you(['’]d| would)? (like|want|prefer)\b", r"\bhappy to\b",
    r"\bfeel free to\b", r"\bsay so and I['’]ll\b",
    r"\b(as you asked|as requested|per your request|as instructed)\b",
    
    r"\b(confidence|severity):\s*(high|medium|low|critical)\b",
    r"\b(measured|read|guessed), not (measured|read|guessed)\b",
]



EM_DASH = "\u2014"

BRITISH_ISE_STEMS = [
    "normal", "initial", "serial", "deserial", "organ", "synchron", "author",
    "custom", "optim", "priorit", "minim", "maxim", "summar", "categor",
    "standard", "visual", "sanit", "token", "final", "capital", "item",
    "apolog", "real", "util", "emphas", "special", "general", "material",
    "parameter", "container", "virtual", "modular", "colour", "digit", "monet",
    "legitim", "familiar", "human", "popular", "rational", "stabil", "symbol",
    "central", "neutral", "equal",
]

BRITISH_OUR_STEMS = [
    "col", "behavi", "fav", "hon", "neighb", "lab", "rum", "hum", "arm",
    "flav", "harb", "od", "parl", "savi", "splend", "val", "vap", "endeav",
    "demean",
]

BRITISH_RE_STEMS = [
    "cent", "met", "lit", "fib", "theat", "calib", "somb", "spect", "lust",
    "manoeuv",
]

BRITISH_MISC = [
    r"cancell(ed|ing)", r"travell(ed|ing)", r"labell(ed|ing)",
    r"modell(ed|ing)", r"fuell(ed|ing)", r"signall(ed|ing)", r"marvellous",
    r"defence", r"offence", r"pretence", r"licence", r"practise", r"grey",
    r"catalogue", r"dialogue", r"analogue", r"monologue", r"aluminium",
    r"maths", r"programme", r"whilst", r"amongst", r"learnt", r"spelt",
    r"enquir(y|ies)", r"judgement", r"acknowledgement", r"ageing",
    r"sceptical", r"storey", r"behavioural", r"whinge",
]


def alt(words):
    'One extended-regex alternation, word bounded.'
    return r"\b(" + "|".join(words) + r")\b"


def phrase_alt(phrases):
    'One alternation with no word boundaries, for multi word phrases.'
    return "(" + "|".join(phrases) + ")"


BRITISH_ISE = alt([s + r"is(e|es|ed|ing|ation|ations|able|er|ers)"
                   for s in BRITISH_ISE_STEMS])
BRITISH_ANALYSE = r"\banalys(e|es|ed|ing|er|ers)\b"
BRITISH_OUR = alt([s + r"(our|ours|ouring|oured|ourite|ourites|ourable|ourful)"
                   for s in BRITISH_OUR_STEMS])
BRITISH_RE = r"\b(" + "|".join(BRITISH_RE_STEMS) + r")res?\b"
BRITISH_MISC_RE = alt(BRITISH_MISC)


def shell():
    'Print the lists as shell assignments for the bash guard to evaluate.'
    out = [
        "HARD=%s" % _q(alt(HARD)),
        "HARD_PHRASE=%s" % _q(phrase_alt(HARD_PHRASE)),
        "EM_DASH=%s" % _q(EM_DASH),
        "BRIT_ISE=%s" % _q(BRITISH_ISE),
        "BRIT_ANALYSE=%s" % _q(BRITISH_ANALYSE),
        "BRIT_OUR=%s" % _q(BRITISH_OUR),
        "BRIT_RE=%s" % _q(BRITISH_RE),
        "BRIT_MISC=%s" % _q(BRITISH_MISC_RE),
    ]
    print("\n".join(out))


def _q(s):
    'Single quote for shell. The lists contain no single quotes today; the\n    escape is here so adding one later cannot break every guard at once.'
    return "'" + s.replace("'", "'\\''") + "'"






HARD_DISPLAY = [
    "leverage", "utilize", "robust", "comprehensive", "seamless", "delve",
    "posture", "paradigm", "holistic", "synergy", "best-in-class",
    "cutting-edge", "off-box", "barrel", "adjudicate", "adjudication",
    "break-glass", "essentially", "fundamentally", "basically", "actually",
    "arguably", "simply", "straightforward", "powerful", "elegant", "very", "quite",
    "facilitate", "genuine", "truly", "honest", "frankly", "candidly", "plainly",
    "notably", "importantly", "crucially", "interestingly", "ultimately", "entirely",
    "precisely", "cleanly", "silently", "hygiene", "bespoke", "cadence", "orthogonal",
    "footgun", "tractable", "unasked", "canonical", "idiomatic", "substantive",
]




HARD_PHRASE_DISPLAY = [
    "it's worth noting", "it is worth noting", "it's important to note",
    "it is important to note", "at the end of the day", "worth knowing about",
    "changed the shape of", "the shape of this work", "would have shipped silently",
    "shipped silently", "the kind that would have", "changed the picture",
    "the real story", "Checkpoint.", "worth your call", "one thing worth",
    "worth flagging", "worth mentioning", "when I get there", "unless you'd rather", "I'll flag",
    "Heads-up:", "more on that later", "before I get to",
    "in order to", "a number of", "in terms of", "with respect to", "the fact that",
    "that being said", "needless to say", "the reason is", "rather than", "instead of",
    "as opposed to", "not because", "wrong, not the code.", "not a bug, but",
    "exactly the", "fully", "to be clear", "for what it's worth", "almost certainly",
    "most likely", "I got this wrong", "I want to be careful here", "the real bug", "a real gap", "by design", "in practice", "on purpose",
    "by construction", "for the record", "net effect", "in principle", "going forward",
    "bottom line", "in short", "in a nutshell", "the gist", "for good measure",
    "it turns out", "tl;dr", "load-bearing", "sanity check", "blast radius", "sharp edge",
    "moving parts", "low-hanging fruit", "rabbit hole", "smoking gun", "silver bullet",
    "belt and braces", "bites you", "surfaced", "surface the", "worth", "Good,", "Perfect.",
    "Let me", "Let's", "Time to", "given the effort budget", "final report now", "up front",
    "before I start", "one note", "two things to know", "you should know", "keep in mind",
    "Flagging", "flagging this", "say the word", "if you want", "happy to", "feel free to", "say so and I'll",
    "as you asked", "as requested", "Confidence: high", "Severity: low", "read, not measured",
]

BRIEF = """user's language rules. These are not style preferences; he has asked
repeatedly and they are enforced by hooks on every harness that supports them.

No em dashes. Use a period, a comma, or a colon.
Never use: {hard}.
Never write: {phrases}.
Say a finding when you have it, as the fact. Never announce it ahead of time and never
rate how much it matters.
American spelling: color, behavior, normalize, canceled, gray, catalog, defense.
Write imperative, not observational. "Export one component per file", never "A file
exports one component". The second sounds self-important.
State the fact. Do not compare to an alternative and do not justify. Cut "rather
than", "instead of", "as opposed to".
No filler narration ("Let me", "Now I'll", "Good,"), no offers, no Confidence or
Severity tags. Name only the claims you did not verify. Between tool calls in a long
turn, a one-line status is allowed: what you found and what you do next.
Do not report counts a person can get themselves: file counts, module counts, line
tallies. Say only what is not clear from looking at the repo.
No narrative, no preamble, no recap of what he just watched. A finished task is its
result. One word is a complete answer when one word conveys it.
Terse, not dumbed down. He is technical and well read. Use the exact term, which is
usually the short one. Cut padding, never vocabulary. Names stay descriptive: this
shortens prose, never identifiers.
Full lists and his own examples: get_doc("plain-language.md")."""


def brief():
    'The compact rule block, for harnesses with no output hook.\n\n    opencode, copilot and antigravity raise no Stop event, so terse-output-gate\n    cannot run there and no guard can refuse a finished message. Injecting the\n    rules into the system prompt is prevention where enforcement is unavailable.\n    Generated from the lists above so the injected text cannot drift from what the\n    write guard actually blocks.'
    plain = HARD_DISPLAY
    return BRIEF.format(hard=", ".join(plain),
                        phrases=", ".join('"%s"' % p for p in HARD_PHRASE_DISPLAY))


def display_drift():
    'HARD entries with no plain spelling in HARD_DISPLAY, and the reverse.\n\n    Two lists describing one set is the drift shape this whole file exists to kill,\n    so the file reports on itself. Called by --check and by the default output.'
    import re as _re
    missing = []
    for pat in HARD:
        stem = pat.split("(")[0]
        if not any(d.startswith(stem) for d in HARD_DISPLAY):
            missing.append(pat)
    extra = [d for d in HARD_DISPLAY
             if not any(_re.fullmatch(p, d, _re.I) for p in HARD)]
    return missing, extra


def phrase_drift():
    'HARD_PHRASE patterns no display entry spells, and display entries no pattern matches.'
    import re as _re
    missing = [p for p in HARD_PHRASE
               if not any(_re.search(p, d, _re.I) for d in HARD_PHRASE_DISPLAY)]
    extra = [d for d in HARD_PHRASE_DISPLAY
             if not any(_re.search(p, d, _re.I) for p in HARD_PHRASE)]
    return missing, extra


if __name__ == "__main__":
    if "--sh" in sys.argv:
        shell()
    elif "--brief" in sys.argv:
        print(brief())
    elif "--check" in sys.argv:
        miss, extra = display_drift()
        pmiss, pextra = phrase_drift()
        if miss or extra or pmiss or pextra:
            print("DRIFT: unlisted=%s stale=%s phrase_unlisted=%s phrase_stale=%s"
                  % (miss, extra, pmiss, pextra))
            sys.exit(1)
        print("hard list and display list agree")
    else:
        miss, extra = display_drift()
        pmiss, pextra = phrase_drift()
        miss, extra = miss + pmiss, extra + pextra
        print("hard=%d hard_phrase=%d british_misc=%d%s"
              % (len(HARD), len(HARD_PHRASE), len(BRITISH_MISC),
                 "" if not (miss or extra) else "  DRIFT: run --check"))
