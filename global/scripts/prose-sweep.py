#!/usr/bin/env python3
'Worklist for the prose half of the plain-language rule.\n\nplain-language-sweep.py covers spelling and the HARD jargon list. This covers\nwhat that one does not: the SOFT word list (padding, hedges, intensifiers), the\nsentence-length ceiling, and passages repeated across files, which is the DRY\nhalf. Read-only; prints file:line so a fixer edits flagged lines instead of\nreading whole files.\n\nExclusions match plain-language-sweep.py, plus two the rule names explicitly:\ndocs/maintainability/ and docs/cleanup/ are ralph loop ledgers, append-only\nhistory written by the loops through MCP (ralph-ledger-in-store.md), so an old\nentry is not rewritten.\n\nThe counts are a worklist, not a target. Many padding hits are not\nfiller: in the probe and CI docs a reported-vs-measured contrast is the subject.\nLong-sentence hits skew the same way -- a markdown bullet\nrun gets joined into one apparent run-on, and dense technical prose carrying a\ncommand plus its caveat is one idea, not two. Judge every line before cutting.'
import os, re, sys, json, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task  

BASE = os.environ.get('AGENT_CONTEXT_STORE') or os.path.expanduser('~/.agent-context')
SKIP = ('/references/', '/vendor/', '/node_modules/', '/archive/',
        '/' + hp.CLAUDE_DIRNAME + '/worktrees/', '/' + hp.AGENTS_DIRNAME + '/worktrees/', '/docs/maintainability/', '/docs/cleanup/',
        '/__pycache__/', '/.git/')

import importlib.util as _ilu
_words_path = os.path.join(BASE, 'global', 'scripts', 'plain-language-words.py')
_spec = _ilu.spec_from_file_location('pl_words', _words_path)
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load spec for pl_words from %s" % _words_path)
PL = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(PL)



SOFT = re.compile(PL.phrase_alt(PL.HARD_PHRASE), re.I)
EM_DASH = PL.EM_DASH

SURFACE_VERB = re.compile(r'\b(surface|surfaces|surfaced|surfacing)\s+(the|a|an|it|that|this)\b', re.I)

FENCE = re.compile(r'^\s*```')
CODE = re.compile(r'`[^`]*`')
WIKILINK = re.compile(r'\[\[[^\]]*\]\]')
URL = re.compile(r'https?://\S+')
SENT = re.compile(r'[^.!?]+[.!?]')


def prose_line(ln):
    s = ln.strip()
    return bool(s) and not s.startswith(('|', '#', '>', '---', '==='))


def vendored_skill(p):
    "Same test as plain-language-sweep.py's _is_vendored_skill, which gives the reason."
    parts = p.split(os.sep)
    if 'skills' not in parts:
        return False
    i = parts.index('skills')
    return i + 1 < len(parts) and os.path.isdir(
        os.path.join(os.sep.join(parts[:i + 2]), 'references'))


def files(scope='.'):
    for root, dirs, fns in os.walk(os.path.join(BASE, scope)):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', '.venv', 'node_modules')]
        for fn in fns:
            if not fn.endswith(('.md', '.sh', '.py')) or fn.endswith('.meta.toml'):
                continue
            p = os.path.join(root, fn)
            
            
            if (any(v in p for v in SKIP) or vendored_skill(p)
                    or 'plain-language' in p or 'prose-sweep' in p):
                continue
            yield p


def scan(scope='.'):
    soft, longs = [], []
    passages = collections.defaultdict(list)
    for p in files(scope):
        try:
            lines = open(p, encoding='utf-8').read().split('\n')
        except Exception:
            continue
        rel = os.path.relpath(p, BASE)
        infence = False
        for i, ln in enumerate(lines, 1):
            if FENCE.match(ln):
                infence = not infence
                continue
            if infence or not prose_line(ln):
                continue
            masked = CODE.sub(lambda m: '\x00' * len(m.group(0)), ln)
            masked = WIKILINK.sub(lambda m: '\x00' * len(m.group(0)), masked)
            masked = URL.sub(lambda m: '\x00' * len(m.group(0)), masked)
            for m in SOFT.finditer(masked):
                soft.append((rel, i, m.group(0), ln.strip()[:110]))
            for m in SURFACE_VERB.finditer(masked):
                soft.append((rel, i, 'surface (verb)', ln.strip()[:110]))
            for s in SENT.finditer(masked.replace('\x00', '')):
                t = re.sub(r'[^a-z0-9 ]', '', ' '.join(s.group(0).split()).strip().lower())
                if len(t.split()) >= 12:
                    passages[t].append((rel, i))
        
        
        infence, buf, start = False, [], None
        for i, ln in enumerate(lines, 1):
            if FENCE.match(ln):
                infence = not infence
                buf, start = [], None
                continue
            if infence or not prose_line(ln):
                buf, start = [], None
                continue
            if start is None:
                start = i
            buf.append(ln.strip())
            if re.search(r'[.!?]\s*$', ln.strip()):
                text = URL.sub('x', CODE.sub('x', ' '.join(buf)))
                for s in SENT.finditer(text):
                    n = len(s.group(0).split())
                    if n > 34:
                        longs.append((rel, start, n, s.group(0).strip()[:130]))
                buf, start = [], None
    dupes = {k: v for k, v in passages.items() if len({f for f, _ in v}) >= 2}
    return soft, longs, dupes


def _usage_error():
    "--help or an unrecognized flag. Same contract as plain-language-sweep.py's _usage_error."
    args = sys.argv[1:]
    if any(a in ('-h', '--help') for a in args):
        print(__doc__)
        return 0
    unknown = [a for a in args if a.startswith('-')]
    if unknown:
        print('prose-sweep: unknown flag %s' % unknown[0], file=sys.stderr)
        return 2
    return None


def main():
    
    
    args = sys.argv[1:]
    as_json = 'json' in args
    scope = next((a for a in args if a != 'json'), '.')
    soft, longs, dupes = scan(scope)
    if as_json:
        print(json.dumps({
            'soft': [{'file': f, 'line': l, 'word': w, 'text': t} for f, l, w, t in soft],
            'long': [{'file': f, 'line': l, 'words': n, 'text': t} for f, l, n, t in longs],
            'dupes': [{'text': k, 'places': v} for k, v in dupes.items()],
        }, indent=1))
        return 0
    print(f"BLOCKED PHRASES: {len(soft)} hits in {len({r[0] for r in soft})} files")
    for w, c in collections.Counter(r[2].lower() for r in soft).most_common(12):
        print(f"  {c:4}  {w}")
    print()
    for f, c in collections.Counter(r[0] for r in soft).most_common(20):
        print(f"  {c:3}  {f}")
    print(f"\nSENTENCES OVER 34 WORDS: {len(longs)} in {len({r[0] for r in longs})} files")
    for f, c in collections.Counter(r[0] for r in longs).most_common(20):
        print(f"  {c:3}  {f}")
    print(f"\nREPEATED PASSAGES (12+ words, 2+ files): {len(dupes)}")
    for k, v in sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:20]:
        print(f"  {len(v)}x  {k[:88]}")
        for f, l in v[:6]:
            print(f"        {f}:{l}")
    return 0


if __name__ == '__main__':
    _early = _usage_error()
    if _early is not None:
        sys.exit(_early)
    store_task.main_or_forward('prose-sweep', main, store=BASE)
