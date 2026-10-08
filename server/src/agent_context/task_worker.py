"The warm runner for read-only store tasks (policy).\n\nA read-only task call (`Task.readonly`: test-lock `status` and `check`, the question\nlocked-test-drift-gate asks on every Stop and block-locked-test-edit on every store Edit)\nspent more time starting Python and importing than working. The daemon keeps one of these\nprocesses and sends it each such call. It holds the standard library loaded and forks one\nchild per call; the child takes the call's working directory, environment, argv and stdin\nand runs the script as `python script args` would, so each call is as isolated as a\nprocess of its own and always runs the script's current text.\n\nProtocol, over the process's stdin and stdout: a request and a reply are each a 4-byte\nbig-endian length and a JSON object. Request {script, args, cwd, env, stdin}; reply\n{exit, stdout, stderr} (text, decoded with replacement). End of input ends the process,\nso it exits with the daemon, including across the daemon's re-exec."
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import traceback




PRELOAD = ("argparse", "contextlib", "datetime", "fnmatch", "glob", "hashlib", "io",
           "json", "re", "shlex", "shutil", "subprocess", "tempfile", "time")


def _read_exact(stream, n: int) -> bytes | None:
    data = b""
    while len(data) < n:
        chunk = stream.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_message(stream) -> dict | None:
    head = _read_exact(stream, 4)
    if head is None:
        return None
    (length,) = struct.unpack(">I", head)
    body = _read_exact(stream, length)
    return None if body is None else json.loads(body)


def write_message(stream, doc: dict) -> None:
    body = json.dumps(doc).encode("utf-8")
    stream.write(struct.pack(">I", len(body)) + body)
    stream.flush()




_CODE: dict[str, tuple[tuple[int, int], object]] = {}


def compiled(script: str):
    st = os.stat(script)
    key = (st.st_mtime_ns, st.st_size)
    hit = _CODE.get(script)
    if hit is None or hit[0] != key:
        with open(script, "rb") as fh:
            hit = (key, compile(fh.read(), script, "exec"))
        _CODE[script] = hit
    return hit[1]


def _child(req: dict, code_obj, out_fd: int, err_fd: int, in_fd: int) -> None:
    'Runs in the forked child; never returns.'
    code = 1
    try:
        os.dup2(in_fd, 0)
        os.dup2(out_fd, 1)
        os.dup2(err_fd, 2)
        sys.stdin = open(0, encoding="utf-8", errors="replace", closefd=False)
        sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
        sys.stderr = open(2, "w", encoding="utf-8", closefd=False)
        os.environ.clear()
        os.environ.update(req["env"])
        os.chdir(req["cwd"])
        script = req["script"]
        sys.argv[:] = [script] + list(req["args"])
        sys.path.insert(0, os.path.dirname(script))
        try:
            exec(code_obj, {"__name__": "__main__", "__file__": script,
                            "__builtins__": __builtins__})
            code = 0
        except SystemExit as exc:
            if exc.code is None:
                code = 0
            elif isinstance(exc.code, int):
                code = exc.code
            else:
                sys.stderr.write("%s\n" % exc.code)
                code = 1
        except BaseException:  
            traceback.print_exc()
            code = 1
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  
                pass
    finally:
        os._exit(code & 255)


def run_one(req: dict) -> dict:
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, \
            tempfile.TemporaryFile() as inp:
        inp.write((req.get("stdin") or "").encode("utf-8"))
        inp.flush()
        inp.seek(0)
        code_obj = compiled(req["script"])
        pid = os.fork()
        if pid == 0:
            _child(req, code_obj, out.fileno(), err.fileno(), inp.fileno())
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        out.seek(0)
        err.seek(0)
        return {"exit": code, "stdout": out.read().decode("utf-8", "replace"),
                "stderr": err.read().decode("utf-8", "replace")}


def serve(inp, out) -> None:
    for name in PRELOAD:
        __import__(name)
    while True:
        req = read_message(inp)
        if req is None:
            return
        try:
            reply = run_one(req)
        except Exception as exc:  
            reply = {"error": "%s: %s" % (type(exc).__name__, exc)}
        write_message(out, reply)


if __name__ == "__main__":
    serve(sys.stdin.buffer, sys.stdout.buffer)
