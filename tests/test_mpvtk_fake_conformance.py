"""Do the test fakes still match the things they stand in for?

Almost every mpvtk browser test runs against `FakeSource` (for
`repository.LibrarySource`) and `FakeController` (for
`ui._PlayerController`). A fake that is MORE permissive than production
hides bugs rather than finding them, in two specific ways this project has
already been bitten by:

1. **A method the fake accepts and production does not have.**
   `FakeController.__getattr__` records any call and returns None, so a view
   calling `self.controller.some_new_thing()` passes here and raises
   `AttributeError` in front of a user.

2. **A parameter the fake swallows.** `FakeSource.get_library_items` took
   `**kw` and discarded `sort_by`/`sort_order`/`filters` entirely, so every
   filter, sort, unplayed toggle and letter-jump test asserted only on the
   browser's own scratch dict. If the view stopped passing `filters=` to the
   source, all of them stayed green and every filter in the app silently did
   nothing. That is exactly how `_load_person` shipped ignoring its sort.

These tests compare the surfaces mechanically so neither can drift
unnoticed. They are not a substitute for asserting on behaviour — they
guarantee that the fakes the behaviour tests use are honest.
"""

import inspect
import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    LibrarySource, OfflineLibrarySource)
from jellyfin_mpv_shim.mpvtk_browser.ui import _PlayerController  # noqa: E402

from tests.test_mpvtk_browser_shell import (  # noqa: E402
    FakeController, FakeSource)


def public_methods(obj):
    out = {}
    for name, member in inspect.getmembers(obj):
        if name.startswith("_"):
            continue
        if inspect.isfunction(member) or inspect.ismethod(member):
            out[name] = member
    return out


def accepts(fn, name):
    """Would ``fn`` accept a keyword argument called ``name``?"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if p.name == name and p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY):
            return True
    return False


class TestFakeSourceMatchesTheRealOne(unittest.TestCase):
    def test_every_fake_method_exists_on_the_real_source(self):
        """A fake method with no counterpart means the tests exercise a
        contract production does not have."""
        real = set(public_methods(LibrarySource))
        # The offline catalog is a legitimate alternative implementation;
        # anything it defines is a real part of the source contract too.
        real |= set(public_methods(OfflineLibrarySource))
        extra = sorted(set(public_methods(FakeSource)) - real)
        self.assertEqual(
            extra, [],
            "FakeSource defines methods LibrarySource does not: %s" % extra)

    def test_the_fake_accepts_every_parameter_production_declares(self):
        """The reverse of the usual drift: if production grows a parameter
        the fake cannot take, the tests break loudly, which is fine. The
        dangerous direction is the fake accepting a parameter and *ignoring*
        it — covered by test_mpvtk_browser_shell's recording fakes — but a
        fake that cannot even accept it means the view is never tested with
        it at all."""
        bad = []
        fakes = public_methods(FakeSource)
        for name, fake_fn in fakes.items():
            real_fn = getattr(LibrarySource, name, None)
            if real_fn is None:
                continue
            try:
                sig = inspect.signature(real_fn)
            except (TypeError, ValueError):
                continue
            for p in sig.parameters.values():
                if p.name in ("self", "args", "kwargs"):
                    continue
                if p.kind in (inspect.Parameter.VAR_KEYWORD,
                              inspect.Parameter.VAR_POSITIONAL):
                    continue
                if not accepts(fake_fn, p.name):
                    bad.append("%s(%s)" % (name, p.name))
        self.assertEqual(bad, [],
                         "FakeSource cannot accept: %s" % sorted(bad))

    def test_the_check_is_not_vacuous(self):
        self.assertGreater(len(public_methods(FakeSource)), 10)


class TestFakeControllerMatchesTheRealOne(unittest.TestCase):
    def test_every_explicitly_faked_method_exists_on_the_controller(self):
        real = set(public_methods(_PlayerController))
        extra = sorted(set(public_methods(FakeController)) - real)
        self.assertEqual(
            extra, [],
            "FakeController defines methods _PlayerController does not: %s"
            % extra)

    def test_the_check_is_not_vacuous(self):
        self.assertGreater(len(public_methods(FakeController)), 3)


class TestNothingCallsAControllerMethodThatDoesNotExist(unittest.TestCase):
    """FakeController's catch-all __getattr__ records ANY call and returns
    None. That is convenient — it means tests need not stub 28 methods — but
    it also means a view calling a method the real controller does not have
    passes here and raises AttributeError in front of a user.

    Read every `self.controller.<name>` in the browser package and check it
    against the real class.
    """

    def _controller_calls(self):
        import ast
        import os
        from jellyfin_mpv_shim import mpvtk_browser

        pkg = os.path.dirname(mpvtk_browser.__file__)
        names = set()
        for fn in os.listdir(pkg):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(pkg, fn)) as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                # self.controller.NAME(...)
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Attribute)
                        and node.value.attr == "controller"
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == "self"):
                    names.add(node.attr)
            # _client_call/_edit_call/_safe take `lambda c: c.NAME(...)`.
            # Scoped to the lambda's own parameter, not any name called `c` —
            # a bare `c.get(...)` on a dict is not a controller call.
            for node in ast.walk(tree):
                if not isinstance(node, ast.Lambda):
                    continue
                params = {a.arg for a in node.args.args}
                target = params & {"c", "ctl"}
                if not target:
                    continue
                for sub in ast.walk(node.body):
                    if (isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id in target):
                        names.add(sub.attr)
        return names

    def test_every_call_site_names_a_real_controller_method(self):
        real = set(public_methods(_PlayerController))
        called = self._controller_calls()
        missing = sorted(n for n in called if n not in real)
        self.assertEqual(
            missing, [],
            "the browser calls controller methods that do not exist: %s"
            % missing)

    def test_the_scan_found_the_call_sites(self):
        """A scan matching nothing would make the check above vacuous."""
        self.assertGreater(len(self._controller_calls()), 20)


if __name__ == "__main__":
    unittest.main()
