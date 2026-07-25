"""Every late-bound call must resolve to something that exists.

The browser reaches its collaborators through names Python does not check
until the call actually happens. Rename or move a method and nothing fails —
not the import, not the type checker the project doesn't have, not the test
suite, not even a coverage report, because **a lambda body only counts as
covered when it runs**. It fails the first time a user presses that button.

That is the single sharpest hazard in the decomposition described in
``docs/ARCHITECTURE_TARGET.md``, because moving methods is precisely what it
consists of. So the names are checked statically, here, before anything is
moved.

Four surfaces, all of them late-bound:

1. **Lambda receivers.** ``self._act(lambda pm: pm.toggle_pause())`` and
   ``self._safe(lambda c: c.check_updates())`` — the parameter is duck-typed
   and only bound when the callback fires. 70 of these.
2. **``self.controller.X``** — the same boundary reached without a lambda.
   60 of these.
3. **``ROUTES`` tables**, which name a loader and a renderer as *strings*
   that ``_load_route`` / ``_render_route`` resolve with ``getattr``. A view
   whose renderer was renamed raises only when you navigate to it.
4. **``_start_daemon("_slot_name", …)``**, which does ``getattr(self, attr)``
   and ``setattr`` on a slot named by string. A slot never initialised in
   ``__init__`` raises ``AttributeError`` on the first poll.

None of this replaces the behavioural tests: it proves the *name* exists, not
that calling it does the right thing. But a wrong name is the failure mode
refactoring actually produces, and it is the one nothing else here catches.
"""

import ast
import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import gateway as gw_mod  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER = os.path.join(REPO, "jellyfin_mpv_shim", "mpvtk_browser")


def _modules():
    """Every module under mpvtk_browser/, subpackages included.

    This was a flat os.listdir. When the refactor moved code into pages/,
    components/ and gateway/, those became invisible to every scan in this
    file -- 32 lambda receivers among them -- while the tests kept passing.
    The file whose subject is "moving methods breaks late-bound names" was
    itself broken by moving methods.
    """
    for root, _dirs, files in os.walk(BROWSER):
        if "__pycache__" in root:
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, BROWSER).replace(os.sep, "/")
            with open(path, encoding="utf-8") as fh:
                yield rel, ast.parse(fh.read(), filename=path)


def _instance_attrs(cls_source_path, class_name):
    """Names a class assigns to ``self`` anywhere in its body.

    ``dir(cls)`` sees methods and class attributes but not instance state, so
    a legitimate reference like ``pm._player`` would look unresolved without
    this.
    """
    with open(cls_source_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=cls_source_path)
    # class_name=None means every class in the file — the gateway is a
    # package of mixins, so naming one class would read a twelfth of it.
    classes = [n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef)
               and (class_name is None or n.name == class_name)]
    found = set()
    for cls in classes:
        for node in ast.walk(cls):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                    and isinstance(node.ctx, ast.Store)):
                found.add(node.attr)
    return found


def _controller_api():
    """Every name the gateway answers to.

    The gateway is a package of mixins now, so the instance-attribute half
    has to sweep all of them -- reading one file would quietly shrink the
    surface this whole file checks against."""
    import inspect

    api = set(dir(gw_mod.PlayerGateway))
    pkg = os.path.dirname(inspect.getfile(gw_mod))
    for fn in sorted(os.listdir(pkg)):
        if fn.endswith(".py"):
            api |= _instance_attrs(os.path.join(pkg, fn), None)
    return api


def _player_api():
    from jellyfin_mpv_shim.player import PlayerManager

    path = os.path.join(REPO, "jellyfin_mpv_shim", "player.py")
    return set(dir(PlayerManager)) | _instance_attrs(path, "PlayerManager")


def _jellyfin_api():
    """The apiclient's API surface, or None when it cannot be built."""
    try:
        from jellyfin_apiclient_python import JellyfinClient

        return set(dir(JellyfinClient(allow_multiple_clients=True).jellyfin))
    except Exception:
        return None


#: lambda parameter name -> what it is actually bound to at call time.
#: Derived from the helpers that invoke them: ``_act``/``run_action`` pass
#: the PlayerManager, ``_safe``/``_ctl``/``_client_call``/``_edit_call``/
#: ``_play_async`` pass the controller, ``_sync`` passes ``client.jellyfin``.
RECEIVERS = {
    "pm": "player",
    "c": "controller",
    "ctl": "controller",
    "jf": "jellyfin",
}


def _lambda_receiver_refs():
    """(module, line, param, attr) for every ``param.attr`` inside a lambda
    whose first parameter is a known receiver."""
    out = []
    for name, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Lambda):
                continue
            params = [a.arg for a in node.args.args]
            if not params or params[0] not in RECEIVERS:
                continue
            param = params[0]
            for inner in ast.walk(node.body):
                if (isinstance(inner, ast.Attribute)
                        and isinstance(inner.value, ast.Name)
                        and inner.value.id == param):
                    out.append((name, inner.lineno, param, inner.attr))
    return out


class TestLambdaReceiversResolve(unittest.TestCase):
    """``lambda c: c.foo()`` must name a real method.

    This is the user-visible failure mode: the lambda is stored as a
    callback, the button looks fine, and pressing it raises
    ``AttributeError`` inside a pool worker where nobody sees it.
    """

    def test_there_are_receivers_to_check(self):
        # If the walk stops matching, everything below passes vacuously.
        # A floor, not a target. It sat at 45 against an actual 50 while the
        # scan was a flat listdir; making it recursive found 82, which is
        # what it should have been seeing all along.
        self.assertGreater(len(_lambda_receiver_refs()), 70)

    def test_controller_lambdas_resolve(self):
        api = _controller_api()
        bad = ["%s:%d %s.%s" % ref for ref in _lambda_receiver_refs()
               if RECEIVERS[ref[2]] == "controller" and ref[3] not in api]
        self.assertEqual(
            bad, [],
            "These lambdas call methods that do not exist on "
            "PlayerGateway. They fail only when the callback runs:\n  "
            + "\n  ".join(bad))

    def test_player_lambdas_resolve(self):
        api = _player_api()
        bad = ["%s:%d %s.%s" % ref for ref in _lambda_receiver_refs()
               if RECEIVERS[ref[2]] == "player" and ref[3] not in api]
        self.assertEqual(
            bad, [],
            "These lambdas call methods that do not exist on PlayerManager. "
            "run_action defers them to the action thread, so the failure "
            "surfaces far from the press that caused it:\n  "
            + "\n  ".join(bad))

    def test_jellyfin_lambdas_resolve(self):
        api = _jellyfin_api()
        if api is None:
            self.skipTest("jellyfin-apiclient-python API not introspectable")
        bad = ["%s:%d %s.%s" % ref for ref in _lambda_receiver_refs()
               if RECEIVERS[ref[2]] == "jellyfin" and ref[3] not in api]
        self.assertEqual(
            bad, [],
            "These SyncPlay lambdas call apiclient methods that do not "
            "exist — most likely the dependency moved under us:\n  "
            + "\n  ".join(bad))


def _lambda_receiver_calls():
    """Like :func:`_lambda_receiver_refs` but only actual *calls*, carrying
    the argument counts so arity can be checked."""
    out = []
    for name, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Lambda):
                continue
            params = [a.arg for a in node.args.args]
            if not params or params[0] not in RECEIVERS:
                continue
            param = params[0]
            for inner in ast.walk(node.body):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and isinstance(inner.func.value, ast.Name)
                        and inner.func.value.id == param):
                    starred = any(isinstance(a, ast.Starred)
                                  for a in inner.args)
                    out.append({
                        "module": name,
                        "line": inner.lineno,
                        "param": param,
                        "attr": inner.func.attr,
                        "positional": len(inner.args),
                        "keywords": {k.arg for k in inner.keywords if k.arg},
                        "starred": starred or any(k.arg is None
                                                  for k in inner.keywords),
                    })
    return out


class TestLambdaCallsMatchSignatures(unittest.TestCase):
    """A late-bound call must also pass the right number of arguments.

    Renaming is not the only way a move breaks a callback. Changing a
    signature — adding a required parameter, reordering two — leaves every
    name resolving and still fails at call time, and the failure surfaces on
    a pool worker or the action thread rather than at the press.

    Checked only for the controller, whose methods are defined in this repo
    and whose signatures are therefore ours to keep in step. Calls that
    splat (``*args`` / ``**kwargs``) are skipped: their arity is not
    statically known.
    """

    def _controller_signatures(self):
        import inspect as _inspect

        sigs = {}
        for name in dir(gw_mod.PlayerGateway):
            attr = getattr(gw_mod.PlayerGateway, name, None)
            if not callable(attr):
                continue
            try:
                sigs[name] = _inspect.signature(attr)
            except (TypeError, ValueError):
                continue
        return sigs

    @staticmethod
    def _all_controller_calls():
        """Both shapes: ``lambda c: c.foo(...)`` and ``self.controller.foo(...)``.

        The direct form is exactly as late-bound as the lambda — ``controller``
        is assigned in ``__init__`` and is ``None`` in several tests — and it
        is where most of the multi-argument calls live, so checking only
        lambdas misses the arity changes that matter most. (Found the hard
        way: adding a parameter to ``set_watched``, whose only caller is
        ``self.controller.set_watched(...)`` in views.py, slipped straight
        through a lambda-only check.)
        """
        calls = [c for c in _lambda_receiver_calls()
                 if RECEIVERS[c["param"]] == "controller"]
        for name, tree in _modules():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "controller"):
                    continue
                calls.append({
                    "module": name,
                    "line": node.lineno,
                    "param": "self.controller",
                    "attr": node.func.attr,
                    "positional": len(node.args),
                    "keywords": {k.arg for k in node.keywords if k.arg},
                    "starred": any(isinstance(a, ast.Starred)
                                   for a in node.args)
                    or any(k.arg is None for k in node.keywords),
                })
        return calls

    def test_there_are_calls_to_check(self):
        self.assertGreater(len(self._all_controller_calls()), 40)

    def test_controller_calls_are_callable_as_written(self):
        import inspect as _inspect

        sigs = self._controller_signatures()
        bad = []
        for call in self._all_controller_calls():
            if call["starred"]:
                continue
            sig = sigs.get(call["attr"])
            if sig is None:
                continue        # existence is TestLambdaReceiversResolve's job
            # The lambda calls it on an instance, so `self` is already bound;
            # the unbound signature read from the class still carries it.
            params = [p for n, p in sig.parameters.items() if n != "self"]
            try:
                bound = _inspect.Signature(params).bind(
                    *["x"] * call["positional"],
                    **{k: "x" for k in call["keywords"]})
                bound.apply_defaults()
            except TypeError as exc:
                bad.append("%s:%d %s.%s(...) — %s"
                           % (call["module"], call["line"], call["param"],
                              call["attr"], exc))
        self.assertEqual(
            bad, [],
            "These late-bound calls do not match the method's signature. "
            "Every name resolves, so nothing fails until the callback "
            "runs:\n  " + "\n  ".join(bad))


class TestDirectControllerCallsResolve(unittest.TestCase):
    """``self.controller.X`` — the same boundary, reached without a lambda.

    Just as late-bound: ``controller`` is assigned in ``__init__`` and is
    ``None`` in several tests, so a bad name is only found by running the
    code path that uses it.
    """

    def _refs(self):
        out = []
        for name, tree in _modules():
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Attribute)
                        and node.value.attr == "controller"):
                    out.append((name, node.lineno, node.attr))
        return out

    def test_there_are_references_to_check(self):
        self.assertGreater(len(self._refs()), 40)

    def test_every_reference_resolves(self):
        api = _controller_api()
        bad = ["%s:%d controller.%s" % ref for ref in self._refs()
               if ref[2] not in api]
        self.assertEqual(
            bad, [],
            "These reach for controller methods that do not exist:\n  "
            + "\n  ".join(bad))


class TestRouteTablesResolve(unittest.TestCase):
    """``ROUTES`` names a loader and a renderer as strings.

    ``_load_route`` and ``_render_route`` resolve them with ``getattr``, so a
    renamed renderer raises only when someone navigates to that view — and
    the views with the least traffic are exactly the ones nobody navigates to
    while testing by hand.

    ``tests/test_mpvtk_browser_mixins.py`` already checks that no two mixins
    claim the same kind; this checks the other half, that what they claim
    actually exists.
    """

    def _tables(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        out = []
        for base in type.mro(MpvtkBrowser):
            for kind, pair in (base.__dict__.get("ROUTES") or {}).items():
                out.append((base.__name__, kind, pair))
        return out

    def test_there_are_routes_to_check(self):
        """Guards against the scan silently matching nothing.

        The threshold tracks what is left in the tables: step 6c is moving
        kinds out of ROUTES and into pages/, and the registry has its own
        contract test (tests/test_page_contract.py). Every kind still has to
        be served by exactly one of the two, which is what the total below
        checks."""
        from jellyfin_mpv_shim.mpvtk_browser.pages import PAGES

        self.assertGreater(len(self._tables()) + len(PAGES), 15)
        self.assertGreater(len(self._tables()), 0,
                           "no ROUTES left -- delete this class instead of "
                           "letting it pass on an empty scan")

    def test_every_loader_and_renderer_exists(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        bad = []
        for owner, kind, pair in self._tables():
            loader, renderer = pair
            for role, attr in (("loader", loader), ("renderer", renderer)):
                # A None loader is legal: some routes carry their own data.
                if attr is None:
                    continue
                if not hasattr(MpvtkBrowser, attr):
                    bad.append("%s route %r: %s %r does not exist"
                               % (owner, kind, role, attr))
        self.assertEqual(
            bad, [],
            "ROUTES names methods that do not exist. These raise on "
            "navigation, not at import:\n  " + "\n  ".join(bad))

    def test_every_route_has_a_renderer(self):
        # A loader is optional; a renderer is not — _render_route would
        # return None and build() would produce a hole in the scene.
        bad = ["%s route %r has no renderer" % (owner, kind)
               for owner, kind, pair in self._tables() if not pair[1]]
        self.assertEqual(bad, [], "\n  ".join(bad))


class TestDaemonSlotsExist(unittest.TestCase):
    """``_start_daemon("_np_thread", …)`` names its slot as a string.

    It does ``getattr(self, attr)`` to check whether a thread is already
    running and ``setattr`` to register one, so a slot that ``__init__``
    never creates raises ``AttributeError`` on the first call — inside a
    render path, on the loop thread.
    """

    def _slots(self):
        out = []
        for name, tree in _modules():
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "_start_daemon"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    out.append((name, node.lineno, node.args[0].value))
        return out

    def test_there_are_slots_to_check(self):
        self.assertGreater(len(self._slots()), 4)

    def test_every_slot_is_initialised(self):
        created = _instance_attrs(os.path.join(BROWSER, "app.py"),
                                  "MpvtkBrowser")
        bad = ["%s:%d %r" % ref for ref in self._slots()
               if ref[2] not in created]
        self.assertEqual(
            bad, [],
            "These daemon slots are never initialised on the browser, so "
            "_start_daemon's getattr raises the first time they are "
            "used:\n  " + "\n  ".join(bad))




class TestPageContextCallsResolve(unittest.TestCase):
    """``self.ctx.<field>.<method>()`` inside a Page.

    The surface step 6 created, and one this file did not cover until it had
    already let a bug through: ``ctx.nav`` was wired to the raw ``Navigator``
    rather than a navigate/go_back facade, so every page's ``ctx.nav.navigate``
    was an AttributeError waiting for a click -- the season switcher, "To
    Series", "Go to Series". Nothing raised, because they are all lambdas and
    no test clicked them.

    Resolution is dynamic on purpose: the context is built by the shell, so
    asking a real ``_page_context()`` what it actually holds is the only check
    that cannot drift from production wiring.
    """

    PAGES_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "jellyfin_mpv_shim", "mpvtk_browser", "pages")

    @classmethod
    def _ctx(cls):
        from tests.test_mpvtk_browser_shell import FakeSource
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        return MpvtkBrowser(app=None, source=FakeSource())._page_context()

    def _refs(self):
        """(file, line, field, attr) for every self.ctx.FIELD.ATTR."""
        out = []
        for name in sorted(os.listdir(self.PAGES_DIR)):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            path = os.path.join(self.PAGES_DIR, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                # self.ctx.FIELD.ATTR
                if not (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Attribute)
                        and isinstance(node.value.value, ast.Attribute)
                        and node.value.value.attr == "ctx"):
                    continue
                out.append((name, node.lineno, node.value.attr, node.attr))
        return out

    def test_there_are_references_to_check(self):
        self.assertGreater(len(self._refs()), 5)

    def test_every_reference_resolves(self):
        from jellyfin_mpv_shim.mpvtk_browser.repository import (
            LibrarySource, OfflineLibrarySource)

        ctx = self._ctx()
        bad = []
        for name, line, field, attr in self._refs():
            if field == "shell":
                continue          # counted and pinned by test_page_contract
            if field == "source":
                # Against the REAL sources, not the test fake: a page must
                # name something one of them actually provides. Either is
                # enough -- offline legitimately lacks some, and the callers
                # guard with getattr/try.
                if hasattr(LibrarySource, attr) or hasattr(
                        OfflineLibrarySource, attr):
                    continue
                bad.append("%s:%d ctx.source.%s" % (name, line, attr))
                continue
            target = getattr(ctx, field, None)
            if target is None:
                # `ctx.player` is None on a browser built without a
                # controller, and skipping it silently unchecked every
                # ctx.player.* reference -- the same shape as the
                # getattr-with-a-default bug this file exists to catch.
                # Resolve those against the real gateway type instead.
                if field == "player":
                    from jellyfin_mpv_shim.mpvtk_browser.gateway import (
                        PlayerGateway)
                    if not hasattr(PlayerGateway, attr):
                        bad.append("%s:%d ctx.player.%s" % (name, line, attr))
                    continue
                continue
            if not hasattr(target, attr):
                bad.append("%s:%d ctx.%s.%s" % (name, line, field, attr))
        self.assertEqual(
            bad, [],
            "These reach for context members that do not exist:\n  "
            + "\n  ".join(bad))

if __name__ == "__main__":
    unittest.main()
