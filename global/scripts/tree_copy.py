'Copy a store directory into a projection as `rsync -rlt [--delete]` does, with or without rsync.'
import fnmatch
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp  


STORE_ONLY = ("*.meta.toml", "*.meta.json", ".DS_Store", "__pycache__")


def rsync_excludes(excludes=STORE_ONLY):
    return ["--exclude=" + pattern for pattern in excludes]


def _excluded(name, excludes):
    return any(fnmatch.fnmatch(name, pattern) for pattern in excludes)


def _remove_extra(src, dst, excludes):
    for name in os.listdir(dst):
        if _excluded(name, excludes):
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(d) and not hp.is_link(d):
            if os.path.isdir(s):
                _remove_extra(s, d, excludes)
            else:
                shutil.rmtree(d)
        elif not os.path.lexists(s):
            if os.name == "nt" and hp.is_link(d) and os.path.isdir(d):
                os.rmdir(d)  
            else:
                os.remove(d)


def copy_tree(src, dst, delete=False, excludes=STORE_ONLY):
    "Copy src's contents into dst. (ok, error text)."
    if shutil.which("rsync"):
        argv = (["rsync", "-rlt"] + (["--delete"] if delete else []) + rsync_excludes(excludes)
                + [src + "/", dst + "/"])
        done = subprocess.run(argv, capture_output=True, text=True)
        return done.returncode == 0, done.stderr.strip()
    try:
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*excludes))
        if delete:
            _remove_extra(src, dst, excludes)
    except OSError as exc:
        return False, str(exc)
    return True, ""
