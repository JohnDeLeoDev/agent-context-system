#!/usr/bin/env python3
"Fold transcript token usage into the durable record.\n\nWIRED TO THREE EVENTS (see home-settings-sync MANAGED). The script is\nevent-agnostic on purpose -- it collects whatever has been appended to every\ntranscript since its last run, so the same few lines serve all three without\nknowing which fired it. Any extra args are forwarded to the collector, which is\nhow the Stop wiring passes its --min-interval.\n\n  Stop         -- fires at the end of EVERY assistant turn. This is what makes the\n                  numbers near-real-time, and it is wired with --min-interval 30: a\n                  turn whose predecessor collected less than 30s ago costs one\n                  stat() and exits. Without that guard a long session would fork a\n                  full transcript walk hundreds of times to insert a handful of\n                  rows each.\n  SessionEnd   -- the conversation is complete; fold in whatever the last Stop did\n                  not cover. No interval: this is the definitive pass.\n  SessionStart -- catches what the end-side never got. A session killed by a\n                  closed terminal, a crash, or kill -9 never reaches SessionEnd or\n                  a final Stop, so its tail would sit uncollected. Collecting at\n                  launch means the very next start records that spend.\n\nTogether these replace the daily timer this system originally shipped with: Stop\ncovers live sessions continuously, SessionStart covers anything a crash\ninterrupted, and there is no window a timer would have caught that these miss.\n\nTranscripts are swept at cleanupPeriodDays (default 30) and nothing else on the\nmachine retains their `usage` blocks, so whatever is not collected before then is\nunrecoverable. That one-way deadline is why there are three triggers.\n\nDETACHED ON PURPOSE. A warm incremental run is ~0.3s, but the FIRST run on a\nmachine walks every transcript (~18s over 836 files here). Neither belongs on the\npath between the user pressing enter and getting their turn back. The collector\nalso takes a single-writer lock, so the Stop/SessionEnd/SessionStart volley that a\n/clear fires collapses to one run rather than three.\n\nNever blocks and never reports: a telemetry hook that can fail a turn is worse\nthan no telemetry (same rule as usage.py's fail-silent entry points)."
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp


def main():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    script = os.path.join(hp.scripts_dir(home), "token-usage-collect.py")
    if not os.path.isfile(script):
        return 0

    py = shutil.which("python3")
    if not py:
        return 0

    
    
    
    args = sys.argv[1:]
    try:
        subprocess.Popen(
            [py, script, "--quiet"] + args,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
