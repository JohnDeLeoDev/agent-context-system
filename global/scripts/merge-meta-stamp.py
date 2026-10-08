#!/usr/bin/env python3
'Merge driver for entity metadata sidecars -- `merge=acmetastamp`.\n\nWHY. Every store entity is two files: the body, and a `<name>.meta.toml` sidecar\ncarrying uuid/type/description/updated_at. When two machines edit one entity, the\nBODIES usually merge fine -- git is good at that, and where they genuinely\ndisagree the conflict SHOULD surface. The sidecar cannot merge at all: both sides\nlegitimately bumped `updated_at`, git sees one line differing with no common\nresolution, and the whole fleet\'s sync stops on a timestamp.\n\nTHE RULE IS DELIBERATELY NARROWER THAN merge-doc-newer\'s. That driver is scoped to\ngenerated per-project docs, and its own .gitattributes comment gives the reason:\n"a conflict in a global instruction, memory or hook is two people disagreeing about\na rule and MUST surface to a human". A sidecar is not exempt from that -- a\n`description` is prose someone wrote, and last-writer-wins would silently drop one.\nSo this driver resolves ONE case and refuses everything else:\n\n  * The two sides parse as TOML-ish key/value, AND\n  * every key is present on both sides with an identical value, EXCEPT\n  * `updated_at`, which differs and is orderable.\n\nThen, and only then, take the newer stamp. If any other key differs -- description,\nlanguage, scope, name -- that is a real disagreement and this driver EXITS NON-ZERO\nso the conflict lands in front of a person, exactly as it does today. An empty side\nis damage, not an edit, and is also refused: unlike a generated doc, a truncated\nsidecar orphans its body, and that deserves a human rather than a silent repair.\n\nA driver that guesses is worse than one that stops, because the guess is silent.\n\nArgs, per gitattributes(5): %O ancestor  %A ours (RESULT written here)  %B theirs\n                            %P pathname'
import sys


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def parse(text):
    '`key = value` lines into a dict, preserving nothing else.\n\n    Deliberately dumb: no TOML library is guaranteed on every node in this fleet\n    (the Synology hosts run a 3.8 system python, so `tomllib` is not available),\n    and the sidecars this driver sees are flat key/value emitted by one writer.\n    A line this cannot parse makes the whole side unparseable, which routes to\n    the refuse path rather than to a partial comparison.'
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            return None
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in out:
            return None
        out[key] = value.strip()
    return out or None


def main(argv):
    if len(argv) < 4:
        return 1
    ours_path, theirs_path = argv[2], argv[3]
    path = argv[4] if len(argv) > 4 else ours_path

    ours, theirs = read(ours_path), read(theirs_path)
    if ours is None or theirs is None:
        return 1

    def refuse(why):
        sys.stderr.write("merge-meta-stamp: %s -- %s; leaving the conflict for a "
                         "human\n" % (path, why))
        return 1

    
    
    
    if not ours.strip() or not theirs.strip():
        return refuse("one side is empty")

    o, t = parse(ours), parse(theirs)
    if o is None or t is None:
        return refuse("a side is not flat key = value")

    if set(o) != set(t):
        return refuse("the two sides carry different keys (%s)"
                      % ", ".join(sorted(set(o) ^ set(t))))

    differing = sorted(k for k in o if o[k] != t[k])
    if differing != ["updated_at"]:
        return refuse("they differ in more than updated_at (%s)"
                      % ", ".join(differing) if differing
                      else "they do not differ at all")

    o_ts, t_ts = o["updated_at"], t["updated_at"]
    
    
    winner_text = theirs if t_ts > o_ts else ours

    try:
        with open(ours_path, "w", encoding="utf-8") as fh:
            fh.write(winner_text)
    except OSError as exc:
        sys.stderr.write("merge-meta-stamp: %s -- write failed: %s\n" % (path, exc))
        return 1

    sys.stderr.write("merge-meta-stamp: %s -- resolved to the newer updated_at "
                     "(%s)\n" % (path, max(o_ts, t_ts)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
