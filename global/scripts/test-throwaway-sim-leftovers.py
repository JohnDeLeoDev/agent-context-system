#!/usr/bin/env python3
'- `delete` re-lists devices and exits non-zero, naming #481, when the device survives\n    (whether simctl reported the failure or returned 0 and did nothing);\n  - a deleting sweep (`clean`, `sweep --delete`, the sweep inside `create`) prints a loud\n    warning naming #481, the survivor count and their UDIDs;\n  - `create` refuses, naming #481, when more than MAX_LEFTOVERS shut-down throwaways\n    already exist (REFUSE_OVER_LIMIT; it only warns when that is off).\n\nIt runs on Linux: the real script file from the store is copied into a fake checkout\n(<tmp>/.agents/scripts/) beside a stub ci/lib/simulators.py, and `xcrun` on PATH is a\nfake that keeps its device list in a JSON file. Stdlib only.'

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone




SCRIPT = os.environ.get("THROWAWAY_SIM_SCRIPT") or next(
    (p for p in map(os.path.expanduser, (
        "~/.agent-context/projects/example-project/scripts/throwaway-sim.py",
        "~/Developer/Personal/example-project/.agents/scripts/throwaway-sim.py"))
     if os.path.exists(p)),
    os.path.expanduser("~/.agent-context/projects/example-project/scripts/throwaway-sim.py"))
CACHE_TMP = os.path.expanduser("~/.cache/tmp")
os.makedirs(CACHE_TMP, exist_ok=True)

FAKE_XCRUN = r'''#!/usr/bin/env python3
import json, os, sys, uuid
from datetime import datetime, timezone
state_path = os.environ["FAKE_SIM_STATE"]
with open(state_path) as fh:
    state = json.load(fh)
devices = state["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-0"]
def save():
    with open(state_path, "w") as fh:
        json.dump(state, fh)
args = sys.argv[1:]
if args[:1] != ["simctl"]:
    sys.exit("fake xcrun: only simctl")
args = args[1:]
if args[:1] == ["list"]:
    print(json.dumps(state)); sys.exit(0)
if args[:1] == ["delete"]:
    mode = os.environ.get("FAKE_SIMCTL_DELETE", "ok")
    if mode == "fail":
        print('An error was encountered processing the command (domain=NSCocoaErrorDomain, '
              'code=513): "data" couldn\'t be moved because you don\'t have permission to '
              'access "Deleting-%s". Operation not permitted' % uuid.uuid4(), file=sys.stderr)
        sys.exit(1)
    if mode == "silent":
        sys.exit(0)
    state["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-0"] = [
        d for d in devices if d["udid"] != args[1]]
    save(); sys.exit(0)
if args[:1] == ["create"]:
    udid = str(uuid.uuid4()).upper()
    devices.append({"name": args[1], "udid": udid, "state": "Shutdown",
                    "lastBootedAt": datetime.now(timezone.utc).isoformat()})
    save(); print(udid); sys.exit(0)
if args[:1] == ["boot"]:
    for d in devices:
        if d["udid"] == args[1]:
            d["state"] = "Booted"
    save(); sys.exit(0)
sys.exit("fake xcrun: unsupported %r" % (args,))
'''




STUB_SIMULATORS = r'''
import json, os, subprocess, time
from datetime import datetime

def _simctl(*args):
    return subprocess.run(["xcrun", "simctl"] + list(args), capture_output=True, text=True)

def builder_name():
    return "ao-throwaway-%d" % os.getpid()

def _devices():
    data = json.loads(_simctl("list", "devices", "-j").stdout)
    return [d for ds in data["devices"].values() for d in ds]

def delete(udid):
    if _simctl("delete", udid).returncode != 0:
        raise RuntimeError("simctl delete %s failed" % udid)

def sweep(dry_run=False):
    for d in _devices():
        if not d["name"].startswith(("ao-throwaway", "AO-CI-")) or d["state"] != "Shutdown":
            continue
        when = datetime.fromisoformat(d["lastBootedAt"]).timestamp()
        if time.time() - when < 2 * 3600:
            continue
        if dry_run:
            print("would delete", d["udid"])
            continue
        try:
            delete(d["udid"])
        except RuntimeError:
            pass

def create(name):
    sweep()
    udid = _simctl("create", name, "iPhone", "iOS").stdout.strip()
    _simctl("boot", udid)
    return udid
'''


def device(name, state="Shutdown", hours_ago=5.0):
    stamp = datetime.fromtimestamp(time.time() - hours_ago * 3600, timezone.utc).isoformat()
    return {"name": name, "udid": str(uuid.uuid4()).upper(), "state": state,
            "lastBootedAt": stamp}


class ThrowawaySimLeftoverTests(unittest.TestCase):
    'ThrowawaySimLeftoverTests.'

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=CACHE_TMP)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        scripts = os.path.join(self.root, "main", ".agents", "scripts")
        lib = os.path.join(self.root, "main", "ci", "lib")
        bindir = os.path.join(self.root, "bin")
        for d in (scripts, lib, bindir):
            os.makedirs(d)
        self.script = os.path.join(scripts, "throwaway-sim.py")
        shutil.copy(SCRIPT, self.script)
        with open(os.path.join(lib, "simulators.py"), "w", encoding="utf-8") as fh:
            fh.write(STUB_SIMULATORS)
        xcrun = os.path.join(bindir, "xcrun")
        with open(xcrun, "w", encoding="utf-8") as fh:
            fh.write(FAKE_XCRUN)
        os.chmod(xcrun, os.stat(xcrun).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.state = os.path.join(self.root, "state.json")
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_SIM_STATE=self.state)
        self.env.pop("PYTHONPATH", None)
        self.set_devices([])

    def set_devices(self, devices):
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": devices}}, fh)

    def devices(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-0"]

    def run_sim(self, *args, delete_mode="ok"):
        env = dict(self.env, FAKE_SIMCTL_DELETE=delete_mode)
        return subprocess.run([sys.executable, self.script] + list(args), env=env,
                              capture_output=True, text=True, timeout=60)

    def assert_loud(self, proc, udids, total):
        err = proc.stderr
        self.assertIn("#481", err, err)
        self.assertIn("disk is filling", err, err)
        self.assertIn("%d ao-throwaway-* simulator(s) are STILL PRESENT" % len(udids), err, err)
        self.assertIn("%d ao-throwaway-* simulator(s) exist in all" % total, err, err)
        for udid in udids:
            self.assertIn(udid, err, err)

    

    def test_delete_that_fails_with_513_exits_nonzero_naming_481(self):
        dev = device("ao-throwaway-59201")
        self.set_devices([dev])
        proc = self.run_sim("delete", dev["udid"], delete_mode="fail")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_loud(proc, [dev["udid"]], 1)
        self.assertNotIn("deleted", proc.stdout)

    def test_delete_that_reports_success_but_leaves_the_device_exits_nonzero(self):
        dev = device("ao-throwaway-51238")
        self.set_devices([dev])
        proc = self.run_sim("delete", dev["udid"], delete_mode="silent")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_loud(proc, [dev["udid"]], 1)

    def test_delete_that_works_is_quiet_and_exits_zero(self):
        dev = device("ao-throwaway-14025")
        self.set_devices([dev])
        proc = self.run_sim("delete", dev["udid"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("deleted %s" % dev["udid"], proc.stdout)
        self.assertNotIn("#481", proc.stderr)
        self.assertEqual(self.devices(), [])

    

    def test_clean_whose_deletes_fail_warns_with_count_and_udids(self):
        stale = [device("ao-throwaway-%d" % n) for n in (28609, 51167)]
        fresh = device("ao-throwaway-99999", hours_ago=0.1)
        self.set_devices(stale + [fresh])
        proc = self.run_sim("clean", delete_mode="fail")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_loud(proc, [d["udid"] for d in stale], 3)
        
        self.assertNotIn(fresh["udid"], proc.stderr.split("STILL PRESENT")[1].split("\n")[0])

    def test_sweep_delete_whose_deletes_work_is_quiet(self):
        self.set_devices([device("ao-throwaway-1"), device("ao-throwaway-2", hours_ago=0.1)])
        proc = self.run_sim("sweep", "--delete")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("#481", proc.stderr)
        self.assertEqual([d["name"] for d in self.devices()], ["ao-throwaway-2"])

    def test_dry_run_sweep_does_not_warn(self):
        self.set_devices([device("ao-throwaway-1")])
        proc = self.run_sim("sweep", delete_mode="fail")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("#481", proc.stderr)

    

    def test_create_whose_sweep_fails_still_creates_but_warns(self):
        stale = [device("ao-throwaway-%d" % n) for n in (1, 2)]
        self.set_devices(stale)
        proc = self.run_sim("create", delete_mode="fail")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        new_udid = proc.stdout.strip().splitlines()[0]
        self.assertIn(new_udid, [d["udid"] for d in self.devices()])
        self.assert_loud(proc, [d["udid"] for d in stale], 3)

    def test_create_refuses_over_the_leftover_threshold(self):
        
        leftovers = [device("ao-throwaway-%d" % n) for n in range(6)]
        self.set_devices(leftovers)
        proc = self.run_sim("create", delete_mode="fail")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("#481", proc.stderr)
        self.assertIn("refusing to create: 6 shut-down ao-throwaway-*", proc.stderr)
        for d in leftovers:
            self.assertIn(d["udid"], proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(len(self.devices()), 6)

    def test_create_at_the_threshold_is_allowed(self):
        
        self.set_devices([device("ao-throwaway-%d" % n, hours_ago=0.1) for n in range(5)])
        proc = self.run_sim("create")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("#481", proc.stderr)
        self.assertEqual(len(self.devices()), 6)

    def test_booted_throwaways_do_not_count_toward_the_threshold(self):
        self.set_devices([device("ao-throwaway-%d" % n, state="Booted") for n in range(8)])
        proc = self.run_sim("create")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("#481", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
