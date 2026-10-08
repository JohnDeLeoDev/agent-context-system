#!/usr/bin/env python3
'Report where the store still fails the plain-language rules.\n\nRead-only. Prints a worklist; applying is a separate pass through the MCP tools,\nbecause entity bodies are only writable that way.\n\nThe identifier test is deliberately narrow. Accepting any `/`, `_` or `.` within\none character of the match would drop every flagged word that ends a sentence:\n`not the behaviour.` and `never honoured.` would read as identifiers, and the\nscanner would under-report the number a sweep is checked against. A dot only joins\nan identifier when it sits between two word characters (`behaviour.yml`,\n`foo.behaviour`); a dot followed by a space or end-of-line is a full stop.\n\nObservations guarded: #277.'
import os, re, sys, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task  

STEMS = {
 'behaviour':'behavior','behavioural':'behavioral','behaviours':'behaviors',
 'colour':'color','colours':'colors','coloured':'colored','colouring':'coloring',
 'neighbour':'neighbor','neighbours':'neighbors','neighbouring':'neighboring',
 'honour':'honor','honours':'honors','honoured':'honored','honouring':'honoring',
 'favour':'favor','favours':'favors','favoured':'favored','favourite':'favorite',
 'labour':'labor','rumour':'rumor','humour':'humor','armour':'armor','flavour':'flavor',
 'odour':'odor','parlour':'parlor','saviour':'savior','valour':'valor','vapour':'vapor',
 'endeavour':'endeavor','demeanour':'demeanor','harbour':'harbor',
 'centre':'center','centres':'centers','centred':'centered','centring':'centering',
 'metre':'meter','metres':'meters','litre':'liter','fibre':'fiber','theatre':'theater',
 'calibre':'caliber','sombre':'somber','spectre':'specter','lustre':'luster','manoeuvre':'maneuver',
 'cancelled':'canceled','cancelling':'canceling','travelled':'traveled','travelling':'traveling',
 'traveller':'traveler','labelled':'labeled','labelling':'labeling','modelled':'modeled',
 'modelling':'modeling','fuelled':'fueled','signalled':'signaled','signalling':'signaling',
 'marvellous':'marvelous','defence':'defense','defences':'defenses','offence':'offense',
 'pretence':'pretense','licence':'license','practise':'practice','grey':'gray','greyed':'grayed',
 'greyscale':'grayscale','catalogue':'catalog','catalogues':'catalogs','dialogue':'dialog',
 'analogue':'analog','aluminium':'aluminum','maths':'math','programme':'program',
 'whilst':'while','amongst':'among','learnt':'learned','spelt':'spelled','enquiry':'inquiry',
 'judgement':'judgment','judgements':'judgments','acknowledgement':'acknowledgment',
 'ageing':'aging','sceptical':'skeptical','storey':'story',
 'analyse':'analyze','analysed':'analyzed','analyses':'analyzes','analysing':'analyzing',
 'analyser':'analyzer',
}


for s in ['normal','initial','serial','deserial','organ','synchron','author','custom','optim',
          'priorit','minim','maxim','summar','categor','standard','visual','sanit','token','final',
          'capital','item','apolog','real','emphas','special','general','material','parameter',
          'container','virtual','modular','stabil','symbol','central','neutral','equal','digit',
          'monet','legitim','familiar','human','popular','rational','memo','critic']:
    for a, b in (('ise','ize'),('ises','izes'),('ised','ized'),('ising','izing'),
                 ('isation','ization'),('isations','izations'),('isable','izable'),('iser','izer')):
        STEMS[s+a] = s+b

WORD = re.compile(r'\b(' + '|'.join(sorted(STEMS, key=len, reverse=True)) + r')\b', re.I)





STORE = os.environ.get('AGENT_CONTEXT_STORE') or os.path.expanduser('~/.agent-context')

import importlib.util as _ilu
_words_path = os.path.join(STORE, 'global', 'scripts', 'plain-language-words.py')
_spec = _ilu.spec_from_file_location('pl_words', _words_path)
if _spec is None or _spec.loader is None:
    raise ImportError("cannot load spec for pl_words from %s" % _words_path)
PL = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(PL)

JARGON = re.compile(PL.alt(PL.HARD + [r'utilis(e|es|ed|ing|ation)']), re.I)
EM_DASH = PL.EM_DASH
FENCE = re.compile(r'^\s*```')
CODE = re.compile(r'`[^`]*`')
WIKILINK = re.compile(r'\[\[[^\]]*\]\]')
SKIP_DIR = ('/references/', '/vendor/', '/node_modules/', '/archive/',
            '/' + hp.CLAUDE_DIRNAME + '/worktrees/', '/' + hp.AGENTS_DIRNAME + '/worktrees/')


def _is_vendored_skill(path):
    "A skill that ships its own `references/` corpus came from upstream.\n\n    Google's and Anthropic's skills arrive as a SKILL.md beside a reference\n    corpus, and the corpus is already excluded. Excluding the corpus while\n    still reporting its SKILL.md creates debt nobody can durably clear: an\n    edit there is reverted by the next re-sync and nothing says so. So the\n    whole skill directory is out of scope, on the same ground as /references/.\n    Keyed on the corpus rather than a list of names, which would rot."
    parts = path.split(os.sep)
    if 'skills' not in parts:
        return False
    i = parts.index('skills')
    if i + 1 >= len(parts):
        return False
    return os.path.isdir(os.path.join(os.sep.join(parts[:i + 2]), 'references'))


def _is_identifier_context(masked, start, end):
    'True when the match is part of a path or identifier, not prose.\n\n    `/` or `_` touching either side always means an identifier. A `.` counts\n    only when it joins two word characters -- `behaviour.yml`, `foo.behaviour`.\n    A trailing `.` before a space, a quote or end-of-line is a full stop and\n    is never read as an identifier.'
    
    
    
    before = masked[start - 1] if start > 0 else ''
    after = masked[end] if end < len(masked) else ''
    if before in ('/', '_') or after in ('/', '_'):
        return True
    if before == '.' and start >= 2 and (masked[start - 2].isalnum() or masked[start - 2] in '/_'):
        return True
    if after == '.' and end + 1 < len(masked) and masked[end + 1].isalnum():
        return True
    return False


def scan(rootdir):
    rows = []
    for root, dirs, files in os.walk(rootdir):
        
        
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', '.venv', 'node_modules')]
        for fn in files:
            if not fn.endswith(('.md', '.sh', '.py', '.toml')) or fn.endswith('.meta.toml'):
                continue
            p = os.path.join(root, fn)
            if (any(v in p for v in SKIP_DIR) or 'plain-language' in p
                    or _is_vendored_skill(p)):
                continue
            try:
                lines = open(p, encoding='utf-8').read().split('\n')
            except Exception:
                continue
            infence = False
            for i, ln in enumerate(lines, 1):
                if FENCE.match(ln):
                    infence = not infence
                    continue
                if infence:
                    continue
                masked = CODE.sub(lambda m: '\x00' * len(m.group(0)), ln)
                masked = WIKILINK.sub(lambda m: '\x00' * len(m.group(0)), masked)
                for m in WORD.finditer(masked):
                    if _is_identifier_context(masked, m.start(), m.end()):
                        continue            
                    rows.append((p, i, m.group(0), STEMS[m.group(0).lower()], ln.strip()[:100]))
                for m in JARGON.finditer(masked):
                    rows.append((p, i, m.group(0), '<rewrite>', ln.strip()[:100]))
    return rows


def _usage_error():
    '--help or an unrecognized flag: answered here, before any store decision, so\n    neither needs the daemon. Read-only, but the name matches the *-sweep convention\n    (invariant side-effecting-script-parses-its-flags), so a bad flag is refused, never\n    treated as a scope.'
    args = sys.argv[1:]
    if any(a in ('-h', '--help') for a in args):
        print(__doc__)
        return 0
    unknown = [a for a in args if a.startswith('-')]
    if unknown:
        print('plain-language-sweep: unknown flag %s' % unknown[0], file=sys.stderr)
        return 2
    return None


def main():
    base = STORE
    scope = sys.argv[1] if len(sys.argv) > 1 else '.'
    rows = scan(os.path.join(base, scope))
    print(f"{len(rows)} hits in {len(set(r[0] for r in rows))} files under {scope}\n")
    for w, c in collections.Counter(r[2].lower() for r in rows).most_common(15):
        print(f"  {c:4}  {w}")
    print()
    byfile = collections.Counter(r[0] for r in rows)
    for f, c in byfile.most_common(30):
        print(f"  {c:3}  {os.path.relpath(f, base)}")
    return 0


if __name__ == '__main__':
    _early = _usage_error()
    if _early is not None:
        sys.exit(_early)
    store_task.main_or_forward('plain-language-sweep', main, store=STORE)
