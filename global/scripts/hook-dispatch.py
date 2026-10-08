#!/usr/bin/env python3
'hook-dispatch: run every store guard for one Claude Code hook event in one process.\n\nMATCHING follows the Claude Code hooks reference. "*", "" or no matcher selects every\ncall. A matcher made only of letters, digits, _, -, spaces, commas and | is a list of\nexact names split on | or ,. Anything else is an unanchored regex; one that does not\ncompile skips its group and is reported like a broken registry, so a typo cannot quietly\ndisable a guard. The value matched is the tool name, or SessionStart\'s source,\nSessionEnd\'s reason, Notification\'s notification_type, PreCompact\'s trigger or\nSubagentStop\'s agent_type; Stop and UserPromptSubmit ignore matchers. An identical\ncommand runs once per call.\n\nMERGING, because Claude Code documents none beyond running hooks in parallel:\n  - A refusal is exit 2, permissionDecision "deny" or decision "block". Any refusal makes\n    this exit 2 with every reason on stderr, in order, each prefixed "[<guard file>] ".\n    Claude Code routes an exit 2 per event exactly as it routed each guard\'s own, and\n    still reads JSON on stdout, so context and systemMessage travel there.\n  - Without a refusal: permissionDecision by the documented precedence defer > ask >\n    allow, updatedInput from the first guard that sets one, exit 0. On PostToolUse,\n    updatedToolOutput from the first guard that sets one: every guard read the original\n    result, so a second rewrite would replace the first, not build on it.\n  - additionalContext and systemMessage from every guard, joined in order. Plain stdout\n    is context on SessionStart and UserPromptSubmit, as it is for a hook of its own;\n    on other events it passes to stderr. continue:false wins, with every stopReason.\n\nISOLATION. An in-process guard gets the payload as its own stdin (text, with a .buffer),\nand fds 1 and 2 point at capture files of its own while it runs, so output from a child it\nstarts is its output, and a child it leaves running writes into a file no later guard\nreads. Its environment, working directory, sys.path, sys.argv and signal handlers are put\nback before the next guard. The payload is read as bytes and decoded with replacement, so\na malformed byte cannot stop the guards from running.\n\nUsage:\n  hook-dispatch.py <Event>                 guards from $HOOK_DISPATCH_REGISTRY, else\n                                           ~/.claude/hook-dispatch.json\n  hook-dispatch.py <Event> --guard <path>  one guard and no health records, the route\n                                           hook-test-run --via-dispatch takes'
import hashlib
import importlib.util
import io
import json
import marshal
import os
import re
import signal
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_paths as hp
DEFAULT_TIMEOUT = 600
REFUSED = 2
CONTEXT_EVENTS = frozenset(("SessionStart", "UserPromptSubmit"))
IGNORES_MATCHER = frozenset(("Stop", "UserPromptSubmit"))
MATCH_FIELD = {"SessionStart": "source", "SessionEnd": "reason",
               "Notification": "notification_type", "PreCompact": "trigger",
               "SubagentStart": "agent_type", "SubagentStop": "agent_type"}
DECISION_RANK = {"allow": 1, "ask": 2, "defer": 3}
CODE_CACHE_DAYS = 30
EXACT = re.compile(r"^[A-Za-z0-9_\- ,|]+$")

SHELL_TOOLS = {"PowerShell": "powershell"}
WINDOWS = os.name == "nt"

_PLAIN_ARG = re.compile(r"^[A-Za-z0-9_.,:=/@%+-]+$")


class GuardTimeout(BaseException):
    "Raised inside an in-process guard when its timeout passes. BaseException, so a\n    guard's own `except Exception` cannot swallow it."


def load_sibling(name, filename):
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod




def matches(matcher, value):
    'Does this matcher select `value`? Raises re.error for an invalid regex.'
    if matcher in (None, "", "*"):
        return True
    if not isinstance(matcher, str):
        raise re.error("matcher is not a string")
    if EXACT.match(matcher):
        return value in {part.strip() for part in re.split(r"[|,]", matcher) if part.strip()}
    return re.search(matcher, value) is not None


def as_shell_call(payload):
    '`payload` with a PowerShell call made a Bash call (POWERSHELL above), or unchanged.'
    shell = SHELL_TOOLS.get(payload.get("tool_name"))
    if shell is None:
        return payload
    tool_input = payload.get("tool_input")
    tool_input = dict(tool_input) if isinstance(tool_input, dict) else {}
    command = tool_input.get("command")
    if isinstance(command, str):
        scan = load_sibling("shell_command_scan", "shell-command-scan.py")
        tool_input["command"] = scan.powershell_as_posix(command)
    return dict(payload, tool_name="Bash", tool_input=tool_input, shell=shell,
                shell_command=command)


def select(groups, event, payload):
    '(entries whose group matcher fits this call, problems), in registry order, each\n    command once. A group whose matcher does not compile is skipped and named.'
    value = payload.get(MATCH_FIELD.get(event, "tool_name"))
    value = value if isinstance(value, str) else ""
    chosen, seen, problems = [], set(), []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        if event not in IGNORES_MATCHER:
            try:
                fits = matches(group.get("matcher"), value)
            except re.error as exc:
                problems.append("matcher %r is not a valid regex (%s); its group was skipped"
                                % (group.get("matcher"), exc))
                continue
            if not fits:
                continue
        for entry in group.get("hooks") or []:
            command = entry.get("command") if isinstance(entry, dict) else None
            if not command or command in seen:
                continue
            seen.add(command)
            chosen.append(entry)
    return chosen, problems


def split_command(command):
    '(interpreter words, the rest) of a hook command.'
    words = command.split()
    at = 0
    while at < len(words) - 1 and hp.is_interpreter(words[at]):
        at += 1
    return words[:at], words[at:]


def guard_name(command):
    _, rest = split_command(command)
    return os.path.basename(rest[0]) if rest else command[:40]


def in_process_path(command):
    '(the .py script to run in this process, its arguments), or None when a subprocess is\n    needed: a shell guard, one behind a non-Python interpreter, or an argument the shell\n    would change (quotes, expansion, operators).'
    interpreters, rest = split_command(command)
    if not rest or not rest[0].endswith(".py"):
        return None
    if any(not os.path.basename(word).lower().startswith("python") for word in interpreters):
        return None
    if not all(_PLAIN_ARG.match(word) for word in rest[1:]):
        return None
    return rest[0], rest[1:]




def code_cache_dir(home=None):
    return os.path.join(hp.cache_dir(home), "hook-bytecode")


def guard_code(path):
    'guard code.'
    with open(path, "rb") as fh:
        source = fh.read()
    key = hashlib.blake2b(importlib.util.MAGIC_NUMBER + os.fsencode(path) + b"\0" + source,
                          digest_size=16).hexdigest()
    cached = os.path.join(code_cache_dir(), key)
    try:
        with open(cached, "rb") as fh:
            code = marshal.loads(fh.read())
        if isinstance(code, types.CodeType):
            return code
    except (OSError, ValueError, EOFError, TypeError):
        pass
    code = compile(source, path, "exec", dont_inherit=True)
    try:
        os.makedirs(code_cache_dir(), exist_ok=True)
        prune_code_cache()
        partial = "%s.%d.tmp" % (cached, os.getpid())
        with open(partial, "wb") as fh:
            fh.write(marshal.dumps(code))
        os.replace(partial, cached)
    except (OSError, ValueError):
        pass
    return code


def prune_code_cache():
    'Drop entries written more than CODE_CACHE_DAYS ago. Runs only on a miss, so an edit\n    pays for it; an entry still in use is compiled once more and written fresh.'
    cutoff = time.time() - CODE_CACHE_DAYS * 86400
    for entry in os.scandir(code_cache_dir()):
        try:
            if entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            pass


def run_guard_code(path):
    'Run a guard as runpy.run_path(path, run_name="__main__") does, from cached code.'
    code = guard_code(path)
    module = types.ModuleType("__main__")
    module.__dict__.update(__file__=path, __cached__=None, __loader__=None, __package__="",
                           __spec__=None)
    saved_main = sys.modules.get("__main__")
    sys.modules["__main__"] = module
    try:
        exec(code, module.__dict__)
    finally:
        if saved_main is None:
            sys.modules.pop("__main__", None)
        else:
            sys.modules["__main__"] = saved_main




class Outcome:
    def __init__(self, name):
        self.name: str = name
        self.code: int = 0
        self.out: str = ""
        self.err: str = ""
        self.fault: "str | None" = None


class Health:
    'health-record, loaded only when a record has to be written or cleared.'

    def __init__(self, enabled):
        self.enabled = enabled
        self._mod = None

    @staticmethod
    def path(component):
        return os.path.join(hp.state_dir(), "health", component + ".json")

    def _record(self, component, *args):
        if self._mod is None:
            self._mod = load_sibling("health_record", "health-record.py")
        setattr(self._mod, "STATE", os.path.dirname(self.path(component)))
        self._mod.main(["health-record", component] + list(args))

    def fail(self, component, detail):
        if not self.enabled:
            return
        try:
            self._record(component, "--fail", detail)
        except Exception:  
            pass

    def clear(self, component):
        if not self.enabled or not os.path.exists(self.path(component)):
            return
        try:
            self._record(component, "--ok")
        except Exception:  
            pass


class Capture:
    'Two temp files that fds 1 and 2 point at while ONE in-process guard runs.'

    def __init__(self):
        self._files = None

    def files(self):
        if self._files is None:
            self._files = (self._unlinked(), self._unlinked())
        return self._files

    @staticmethod
    def _unlinked():
        "A read-write file with no name. memfd_create on Linux spares the 3 ms tempfile\n        import costs; other systems take tempfile's."
        if hasattr(os, "memfd_create"):
            return os.fdopen(os.memfd_create("hook-dispatch"), "w+b")
        import tempfile
        return tempfile.TemporaryFile()

    @staticmethod
    def take(fh):
        fh.seek(0)
        return fh.read().decode("utf-8", "replace")

    def close(self):
        for fh in self._files or ():
            try:
                fh.close()
            except OSError:
                pass


def run_subprocess(command, payload, timeout, outcome):
    import subprocess
    
    
    argv = command if WINDOWS else ["/bin/sh", "-c", command]
    try:
        proc = subprocess.Popen(argv, shell=WINDOWS, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=not WINDOWS)
    except OSError as exc:
        outcome.fault = "could not start: %s" % exc
        return
    try:
        out, err = proc.communicate(payload.encode("utf-8", "replace"), timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if WINDOWS:  
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        outcome.fault = "timed out after %gs" % timeout
        return
    outcome.code = proc.returncode
    outcome.out = out.decode("utf-8", "replace")
    outcome.err = err.decode("utf-8", "replace")


def _expire(signum, frame):
    raise GuardTimeout()



_SAVED_SIGNALS = tuple(getattr(signal, name) for name in ("SIGALRM", "SIGTERM", "SIGINT", "SIGPIPE")
                       if hasattr(signal, name))


class _Deadline:
    "Raise GuardTimeout in this thread once `seconds` pass. SIGALRM where it exists; Windows\n    has none, so there a timer thread interrupts the main thread through SIGINT, whose handler\n    is _expire for the guard's run. Either way a guard blocked inside one C call is stopped\n    when that call returns."

    def __init__(self, seconds):
        self.seconds = seconds
        self._timer = None

    def __enter__(self):
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, _expire)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        else:
            import _thread
            import threading
            signal.signal(signal.SIGINT, _expire)
            self._timer = threading.Timer(self.seconds, _thread.interrupt_main)
            self._timer.daemon = True
            self._timer.start()
        return self

    def __exit__(self, *exc):
        if self._timer is not None:
            self._timer.cancel()
        elif hasattr(signal, "SIGALRM"):
            signal.setitimer(signal.ITIMER_REAL, 0)
        return False


def run_in_process(path, args, payload, timeout, outcome, capture):
    saved_env = dict(os.environ)
    try:
        saved_cwd = os.getcwd()
    except OSError:
        saved_cwd = None
    saved_path, saved_argv = list(sys.path), list(sys.argv)
    saved_streams = (sys.stdin, sys.stdout, sys.stderr)
    saved_signals = {sig: signal.getsignal(sig) for sig in _SAVED_SIGNALS}
    for stream in saved_streams[1:]:
        try:
            stream.flush()
        except Exception:  
            pass
    out_file, err_file = capture.files()
    fd_out, fd_err = os.dup(1), os.dup(2)
    os.dup2(out_file.fileno(), 1)
    os.dup2(err_file.fileno(), 2)
    sys.stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8", "replace")), encoding="utf-8")
    sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
    sys.stderr = open(2, "w", encoding="utf-8", closefd=False)
    sys.argv[:] = [path] + list(args)
    try:
        with _Deadline(timeout):
            run_guard_code(path)
    except GuardTimeout:
        outcome.fault = "timed out after %gs" % timeout
    except SystemExit as exc:
        if exc.code is None:
            outcome.code = 0
        elif isinstance(exc.code, int):
            outcome.code = exc.code
        else:
            try:
                sys.stderr.write("%s\n" % (exc.code,))
            except Exception:  
                pass
            outcome.code = 1
    except BaseException as exc:  
        outcome.fault = "raised %s: %s" % (type(exc).__name__, exc)
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  
                pass
        os.dup2(fd_out, 1)
        os.dup2(fd_err, 2)
        os.close(fd_out)
        os.close(fd_err)
        sys.stdin, sys.stdout, sys.stderr = saved_streams
        for sig, handler in saved_signals.items():
            try:
                signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
            except (OSError, ValueError, TypeError):
                pass
        os.environ.clear()
        os.environ.update(saved_env)
        if saved_cwd:
            try:
                os.chdir(saved_cwd)
            except OSError:
                pass
        sys.path[:] = saved_path
        sys.argv[:] = saved_argv
    outcome.out = Capture.take(out_file)
    outcome.err = Capture.take(err_file)


def run_guard(entry, payload, health, event):
    command = entry["command"]
    outcome = Outcome(guard_name(command))
    try:
        timeout = float(entry.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = float(DEFAULT_TIMEOUT)
    script = in_process_path(command)
    if script:
        capture = Capture()
        try:
            run_in_process(script[0], script[1], payload, timeout, outcome, capture)
        finally:
            capture.close()
    else:
        run_subprocess(command, payload, timeout, outcome)
    if outcome.fault is None and outcome.code not in (0, REFUSED):
        outcome.fault = "exit %s" % outcome.code
    component = "hook-dispatch-" + outcome.name.rsplit(".", 1)[0]
    if outcome.fault:
        health.fail(component, "%s %s: %s" % (event, outcome.name, outcome.fault))
    else:
        health.clear(component)
    return outcome




def parse_output(text):
    '(JSON object, "") when stdout is one, else ({}, the plain text).'
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped, strict=False)
            if isinstance(doc, dict):
                return doc, ""
        except ValueError:
            pass
    return {}, text or ""


def merge(event, outcomes, notices, rewrites=True):
    '(exit code, stdout, stderr) for the whole event. `rewrites` False drops every\n    updatedInput (a translated PowerShell call).'
    reasons, contexts, messages, side = [], [], list(notices), []
    decision = decision_reason = updated = None
    rewritten = []          
    halt, halt_reasons = False, []
    for o in outcomes:
        if o.fault:
            messages.append("hook-dispatch: %s failed open on %s (%s)" % (o.name, event, o.fault))
            continue
        doc, plain = parse_output(o.out)
        specific = doc.get("hookSpecificOutput")
        specific = specific if isinstance(specific, dict) else {}
        verdict = str(specific.get("permissionDecision") or "").lower()
        refused = o.code == REFUSED
        reason = o.err.strip() if refused else ""
        if verdict == "deny":
            refused = True
            reason = reason or str(specific.get("permissionDecisionReason") or "").strip()
        if str(doc.get("decision") or "").lower() == "block":
            refused = True
            reason = reason or str(doc.get("reason") or "").strip()
        if refused:
            reasons.append("[%s] %s" % (o.name, reason or "refused without a reason"))
        else:
            if o.err:
                side.append(o.err)
            if verdict in DECISION_RANK and (decision is None
                                             or DECISION_RANK[verdict] > DECISION_RANK[decision]):
                decision, decision_reason = verdict, specific.get("permissionDecisionReason")
            if updated is None and isinstance(specific.get("updatedInput"), dict):
                updated = specific["updatedInput"]
            if event == "PostToolUse" and not rewritten and "updatedToolOutput" in specific:
                rewritten.append(specific["updatedToolOutput"])
        context = specific.get("additionalContext")
        if isinstance(context, str) and context.strip():
            contexts.append(context.strip())
        if plain.strip():
            if event in CONTEXT_EVENTS:
                contexts.append(plain.strip())
            else:
                side.append(plain)
        message = doc.get("systemMessage")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
        if doc.get("continue") is False:
            halt = True
            if doc.get("stopReason"):
                halt_reasons.append(str(doc["stopReason"]))

    specific = {}
    if not reasons:
        if decision:
            specific["permissionDecision"] = decision
            if decision_reason:
                specific["permissionDecisionReason"] = decision_reason
        if updated is not None and rewrites:
            specific["updatedInput"] = updated
        if rewritten:
            specific["updatedToolOutput"] = rewritten[0]
    if contexts:
        specific["additionalContext"] = "\n\n".join(contexts)
    result = {}
    if specific:
        result["hookSpecificOutput"] = dict({"hookEventName": event}, **specific)
    if messages:
        result["systemMessage"] = "\n\n".join(messages)
    if halt:
        result["continue"] = False
        if halt_reasons:
            result["stopReason"] = "\n\n".join(halt_reasons)
    out = json.dumps(result) if result else ""
    if reasons:
        return REFUSED, out, "\n\n".join(reasons)
    return 0, out, "".join(side)




def registry_guards(event):
    '(groups for the event, None) or (None, what is wrong with the registry).'
    path = os.environ.get("HOOK_DISPATCH_REGISTRY") or hp.hook_registry_file()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None, "registry %s is missing" % path
    except (OSError, ValueError) as exc:
        return None, "registry %s is unreadable (%s)" % (path, exc)
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    if not isinstance(hooks, dict):
        return None, "registry %s has no hooks map" % path
    if event not in hooks:
        return None, "registry %s lists no %s guards" % (path, event)
    return hooks[event], None


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print((__doc__ or "").strip())
        return 0 if args else 2
    event = args[0]
    guard = None
    if "--guard" in args:
        at = args.index("--guard")
        guard = args[at + 1] if at + 1 < len(args) else ""
    payload = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(payload)
    except ValueError:
        parsed = None
    parsed = parsed if isinstance(parsed, dict) else {}
    shell_call = as_shell_call(parsed)
    translated = shell_call is not parsed
    if translated:
        parsed, payload = shell_call, json.dumps(shell_call)

    health = Health(enabled=guard is None)
    notices = []
    if guard is not None:
        command = ("%s %s" % (sys.executable, guard) if guard.endswith(".py")
                   else "bash %s" % guard)
        entries = [{"command": command}]
    else:
        groups, problem = registry_guards(event)
        if problem:
            notices.append("hook-dispatch: %s; every %s guard was skipped (failed open)"
                           % (problem, event))
            health.fail("hook-dispatch", "%s: %s" % (event, problem))
            entries = []
        else:
            health.clear("hook-dispatch")
            entries, problems = select(groups, event, parsed)
            for bad in problems:
                notices.append("hook-dispatch: %s %s (failed open)" % (event, bad))
                health.fail("hook-dispatch", "%s: %s" % (event, bad))

    outcomes = [run_guard(entry, payload, health, event) for entry in entries]
    code, out, err = merge(event, outcomes, notices, rewrites=not translated)
    if out:
        sys.stdout.write(out + "\n")
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    return code


def run(argv):
    'main() under the crash policy: the exit code. The script entry calls it, and so does\n    each child hook-server.py forks for a hook-client call (policy).'
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException as exc:  
        detail = "hook-dispatch crashed and failed open: %s: %s" % (type(exc).__name__, exc)
        Health(enabled="--guard" not in argv).fail(
            "hook-dispatch", "%s: %s" % (argv[1] if len(argv) > 1 else "?", detail))
        try:
            sys.stdout.write(json.dumps({"systemMessage": detail}) + "\n")
        except Exception:  
            pass
        return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv))
