#!/usr/bin/env python3
'TEST_WT_FINISH_CORE and TEST_AO_WT_FINISH point the test at other copies of the two\nfiles (used to show it fails against the code before the fix).\n\nRun directly: python3 test-wt-finish-symlink-guard.py'

import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.environ.get("TEST_WT_FINISH_CORE") or os.path.join(HERE, "wt_finish_core.py")
AO_WT_FINISH = os.environ.get("TEST_AO_WT_FINISH") or os.path.join(
    HERE, "..", "..", "projects", "example-project", "scripts", "wt-finish-core.py")
CACHE_TMP = os.path.expanduser("~/.cache/tmp")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_framework(root, loop=None):
    'A stand-in libgit2.xcframework with a benign relative file link. loop="ancestor"\n    adds the #488 link (the framework\'s own name inside it, pointing at the framework);\n    loop="self" adds a link that points at its own path.'
    fw = os.path.join(root, "Kit", "Vendor", "libgit2.xcframework")
    os.makedirs(os.path.join(fw, "ios-arm64", "Headers"))
    with open(os.path.join(fw, "Info.plist"), "w", encoding="utf-8") as fh:
        fh.write("plist\n")
    with open(os.path.join(fw, "ios-arm64", "Headers", "git2.h"), "w", encoding="utf-8") as fh:
        fh.write("header\n")
    os.symlink("Headers/git2.h", os.path.join(fw, "ios-arm64", "git2.h"))
    bad = None
    if loop == "ancestor":
        bad = os.path.join(fw, "libgit2.xcframework")
        os.symlink(fw, bad)
    elif loop == "self":
        bad = os.path.join(fw, "ios-arm64", "loop")
        os.symlink(bad, bad)
    return fw, bad


class Fixture(unittest.TestCase):
    def setUp(self):
        os.makedirs(CACHE_TMP, exist_ok=True)
        self.base = tempfile.mkdtemp(dir=CACHE_TMP)
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.core = load(CORE, "wt_finish_core_488")

    def died(self, fn, *args):
        "The Died message fn raises, printed output swallowed; fails on anything else\n        (the old code's shutil.Error or RecursionError)."
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                fn(*args)
            except self.core.Died as exc:
                return exc.msg
        self.fail("expected the #488 refusal, got a return")


class CopyTreeTests(Fixture):
    def test_link_back_to_an_ancestor_is_refused_by_name_488(self):
        fw, bad = make_framework(self.base, loop="ancestor")
        self.assertEqual(self.core.symlink_cycles(fw), [bad])
        dst = os.path.join(self.base, "copy")
        msg = self.died(self.core.copy_tree, fw, dst)
        self.assertIn(bad, msg)
        self.assertIn("#488", msg)
        self.assertFalse(os.path.exists(dst), "a refused copy still wrote the destination")

    def test_link_to_itself_is_refused_by_name_488(self):
        fw, bad = make_framework(self.base, loop="self")
        self.assertEqual(self.core.symlink_cycles(fw), [bad])
        msg = self.died(self.core.copy_tree, fw, os.path.join(self.base, "copy"))
        self.assertIn(bad, msg)
        self.assertIn("#488", msg)

    def test_tree_without_a_loop_is_copied_with_links_kept_as_links(self):
        fw, _ = make_framework(self.base)
        os.symlink(os.path.join(self.base, "nowhere"), os.path.join(fw, "dangling"))
        self.assertEqual(self.core.symlink_cycles(fw), [])
        dst = os.path.join(self.base, "copy")
        self.core.copy_tree(fw, dst)
        link = os.path.join(dst, "ios-arm64", "git2.h")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), "Headers/git2.h")
        self.assertTrue(os.path.islink(os.path.join(dst, "dangling")))
        with open(os.path.join(dst, "Info.plist"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "plist\n")


class LandingGateTests(Fixture):
    'LandingGateTests.'

    def setUp(self):
        
        
        if not os.path.exists(AO_WT_FINISH):
            self.skipTest("no example-project wt-finish-core.py at %s; set TEST_AO_WT_FINISH" % AO_WT_FINISH)
        super().setUp()
        saved = {k: os.environ.get(k) for k in ("WT_FINISH_CORE", "DEVELOPER_DIR",
                                                "WT_FINISH_DERIVED_DATA")}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        os.environ["WT_FINISH_CORE"] = os.path.abspath(CORE)
        os.environ["DEVELOPER_DIR"] = os.path.join(self.base, "toolchain")
        os.environ["WT_FINISH_DERIVED_DATA"] = os.path.join(self.base, "dd")
        os.makedirs(os.environ["DEVELOPER_DIR"])
        self.ao = load(AO_WT_FINISH, "ao_wt_finish_488")
        self.core = self.ao.core
        self.main = os.path.join(self.base, "main")
        self.wt = os.path.join(self.base, "wt")
        os.makedirs(os.path.join(self.wt, "scripts"))
        generate = os.path.join(self.wt, "scripts", "generate.sh")
        with open(generate, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(generate, 0o755)

    def landing_gate(self):
        ctx = self.core.Ctx(self.ao.PROJECT, self.ao, {"build": True}, "")
        ctx.worktree, ctx.main, ctx.changed_paths = self.wt, self.main, ["App/View.swift"]
        gate = next(g for g in self.ao.gates(ctx) if g.name == "landing")
        return lambda: gate.run(ctx)

    def test_gate_refuses_a_looping_link_in_the_main_checkout_488(self):
        _, bad = make_framework(self.main, loop="ancestor")
        msg = self.died(self.landing_gate())
        self.assertIn(bad, msg)
        self.assertIn("#488", msg)
        self.assertFalse(os.path.exists(os.path.join(self.wt, "Kit", "Vendor", "libgit2.xcframework")))

    def test_gate_copies_a_clean_framework_and_goes_on_to_the_build(self):
        make_framework(self.main)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd = self.landing_gate()()
        self.assertIsInstance(cmd, self.core.Cmd, out.getvalue())
        self.assertEqual(cmd.argv[0], "xcodebuild")
        copied = os.path.join(self.wt, "Kit", "Vendor", "libgit2.xcframework", "ios-arm64", "git2.h")
        self.assertTrue(os.path.islink(copied))
        self.assertEqual(os.readlink(copied), "Headers/git2.h")


if __name__ == "__main__":
    unittest.main(verbosity=2)
