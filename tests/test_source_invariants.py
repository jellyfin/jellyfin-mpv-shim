"""Whole-tree source invariants — mechanical checks that no reviewer reliably
performs and no behavioural test can fail on.

These are deliberately cheap AST walks over the package rather than a linter
config: the repo has no linter, and each rule here exists because the defect
it catches is invisible in a diff.
"""

import ast
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "jellyfin_mpv_shim")
BROWSER = os.path.join(PKG, "mpvtk_browser")

# Generated / vendored trees that are not ours to hold to these rules.
SKIP_DIRS = {"messages", "default_shader_pack", "__pycache__"}


def _sources():
    for root, dirs, files in os.walk(PKG):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(root, name)
                yield os.path.relpath(path, REPO), path


def _parsed():
    for rel, path in _sources():
        with open(path, encoding="utf-8") as fh:
            yield rel, ast.parse(fh.read(), filename=path)


def browser_modules():
    # os.walk, not listdir: pages/, components/ and gateway/ are
    # subpackages, and a flat scan silently stopped covering them when
    # the refactor moved code there -- it kept passing while checking a
    # shrinking fraction of the source.
    for root, _dirs, files in os.walk(BROWSER):
        if "__pycache__" in root:
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, BROWSER)
            yield rel.replace(os.sep, "/"), path


class TestNoOrphanedDocstrings(unittest.TestCase):
    """A bare string expression anywhere but position 0 of a body.

    This is what an inserted guard clause does to a docstring: the string
    stops being ``__doc__`` and becomes a no-op statement, so the
    documentation is silently lost while the diff looks like a pure addition.
    It happened to ``MpvtkBrowser.enter_browse`` — the headless redirect was
    added above the docstring, and nothing anywhere noticed.

    Cheap to check, impossible to spot in review, and it also catches the
    rarer case of a comment written as a string literal.
    """

    def test_no_string_statement_follows_the_first_statement(self):
        offenders = []
        for rel, tree in _parsed():
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if not isinstance(body, list):
                    continue
                for index, stmt in enumerate(body):
                    if index == 0:
                        continue          # a real docstring
                    if (isinstance(stmt, ast.Expr)
                            and isinstance(stmt.value, ast.Constant)
                            and isinstance(stmt.value.value, str)):
                        offenders.append("%s:%d (in %s)" % (
                            rel, stmt.lineno,
                            getattr(node, "name", type(node).__name__)))
        self.assertEqual(
            offenders, [],
            "String literals used as statements — almost always a docstring "
            "orphaned by code inserted above it, which silently drops "
            "__doc__:\n  " + "\n  ".join(offenders))


class TestComponentsAreLeaves(unittest.TestCase):
    """``mpvtk_browser/components/`` is the bottom of the UI stack.

    A component takes data plus render resources plus callbacks, and returns
    a widget tree. It must not know about the app shell, the route dict, the
    data source or navigation — those are precisely the couplings that made
    the browser a 360-method object, and the only thing keeping them out is
    a rule someone has to remember.

    So the rule is a test. See docs/ARCHITECTURE_TARGET.md §1.4 for the
    distinction being enforced: a component may need ``art`` and callbacks,
    but never ``nav``, ``source`` or ``route``.
    """

    #: Sibling modules a component may never import.
    FORBIDDEN_IMPORTS = {"app", "views", "settings", "auth", "dialogs",
                         "music", "queue_edit", "cast", "ui", "repository"}

    #: Names that give away shell coupling if a component references them.
    FORBIDDEN_NAMES = {"nav_stack", "navigate", "go_back", "run_async",
                       "_route_async", "_bump_epoch", "_load_route"}

    @staticmethod
    def _component_sources():
        base = os.path.join(PKG, "mpvtk_browser", "components")
        if not os.path.isdir(base):
            return []
        out = []
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                path = os.path.join(base, name)
                out.append(("mpvtk_browser/components/" + name, path))
        return out

    def test_the_package_exists(self):
        self.assertTrue(
            self._component_sources(),
            "mpvtk_browser/components/ has no modules yet — this is step 1 "
            "of docs/ARCHITECTURE_TARGET.md §3 and the invariant it "
            "establishes.")

    def test_components_do_not_import_the_shell(self):
        offenders = []
        for rel, path in self._component_sources():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    # Relative sibling import: `from .app import X` has
                    # module="app"; `from ..conf import settings` is fine.
                    if node.level == 1 and node.module:
                        names.append(node.module.split(".")[0])
                elif isinstance(node, ast.Import):
                    names += [a.name.split(".")[-1] for a in node.names]
                for name in names:
                    if name in self.FORBIDDEN_IMPORTS:
                        offenders.append("%s:%d imports %s"
                                         % (rel, node.lineno, name))
        self.assertEqual(
            offenders, [],
            "Components must not depend on the shell or the data layer:\n  "
            + "\n  ".join(offenders))

    def test_components_do_not_reference_shell_state(self):
        offenders = []
        for rel, path in self._component_sources():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and node.attr in self.FORBIDDEN_NAMES):
                    offenders.append("%s:%d touches .%s"
                                     % (rel, node.lineno, node.attr))
                elif (isinstance(node, ast.Name)
                        and node.id in self.FORBIDDEN_NAMES):
                    offenders.append("%s:%d references %s"
                                     % (rel, node.lineno, node.id))
        self.assertEqual(
            offenders, [],
            "Components must not reach for navigation or the async runner; "
            "take a callback instead:\n  " + "\n  ".join(offenders))


class TestOneOwnerForSharedMachinery(unittest.TestCase):
    """Certain state must have exactly one owning module.

    ``app.py``'s docstring already asserts this for the epoch — "``_epoch``
    and ``_lock`` live *only* here" — but nothing enforced it, and a claim in
    prose is exactly the kind of thing a decomposition erodes one mixin at a
    time. Now the claim is checked.

    The counted thing is *ownership*, not use: mixins legitimately READ the
    epoch (``ep = self._epoch``) on the loop thread and hand it to
    ``run_async``. What must not spread is the machinery — the lock, the
    pool, and the code that advances the counter.
    """

    BROWSER = os.path.join(PKG, "mpvtk_browser")

    _browser_modules = staticmethod(browser_modules)


    def _modules_defining(self, predicate):
        found = set()
        for name, path in self._browser_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            if predicate(tree, name):
                found.add(name)
        return found

    @staticmethod
    def _assigns(tree, attr):
        """Does this module ASSIGN self.<attr> anywhere?"""
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if (isinstance(target, ast.Attribute)
                        and target.attr == attr
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    return True
        return False

    def test_the_epoch_counter_has_one_writer(self):
        writers = self._modules_defining(
            lambda tree, _n: self._assigns(tree, "_epoch"))
        self.assertLessEqual(
            writers, {"async_runner.py", "app.py"},
            "Only the async runner may advance the epoch; every other module "
            "reads it and passes it to run_async. Writers found: %s"
            % sorted(writers))

    def test_the_route_stack_is_mutated_in_one_place(self):
        """The headless lockdown is only as good as the stack's privacy.

        ``_default_route``'s docstring used to carry this as a warning —
        "every direct ``nav_stack`` assignment must come through here" —
        because a successful connect once put a headless box on the library:
        ``set_source`` reset the stack itself, so the refusal never ran.
        Prose does not survive a decomposition; this does.

        Reading the stack is fine and common (two mixins check its depth to
        decide whether to draw a back button). What is forbidden is mutating
        it in place from outside the Navigator.
        """
        offenders = []
        for name, path in self._browser_modules():
            if name == "navigator.py":
                continue
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                # nav_stack.append(...) / .pop() / .insert(...) / .clear()
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("append", "pop", "insert",
                                               "clear", "remove", "extend")
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "nav_stack"):
                    offenders.append("%s:%d nav_stack.%s(...)"
                                     % (name, node.lineno, node.func.attr))
        self.assertEqual(
            offenders, [],
            "The route stack must only be mutated by the Navigator, which "
            "is where the headless lockdown lives:\n  "
            + "\n  ".join(offenders))

    def test_the_headless_route_policy_has_one_definition(self):
        """One set of allowed routes, in the module that enforces it.

        Two copies would drift, and the copy that drifted would be the one
        some door happened to consult.
        """
        definers = set()
        for name, path in self._browser_modules():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
                    continue
                for target in node.targets:
                    label = getattr(target, "id", getattr(target, "attr", ""))
                    if label == "HEADLESS_ROUTES":
                        definers.add(name)
        self.assertEqual(
            definers, {"navigator.py"},
            "HEADLESS_ROUTES must be defined once, in navigator.py "
            "(app.py may alias it). Found in: %s" % sorted(definers))

    def test_scroll_bookkeeping_has_one_owner(self):
        """The renderer is the authority on where a container is scrolled;
        ``ScrollState`` is the only thing that mirrors it.

        Ten of the eighteen unconverted routes need this, and a second copy
        would drift from the thing actually drawing — which is precisely the
        "windowed rows far past the end, view renders empty" bug
        ``ScrollState.reset``'s docstring records.
        """
        offenders = []
        for name, path in self._browser_modules():
            if name == "scroll_state.py":
                continue
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and node.attr in ("_scroll_off", "_scroll_rendered",
                                          "_live_offsets")):
                    offenders.append("%s:%d touches .%s"
                                     % (name, node.lineno, node.attr))
        self.assertEqual(
            offenders, [],
            "Scroll offsets belong to ScrollState; go through its API "
            "(offset / on_scroll / forget / reset):\n  "
            + "\n  ".join(offenders))

    def test_the_async_lock_has_one_owner(self):
        owners = self._modules_defining(
            lambda tree, _n: self._assigns(tree, "_lock"))
        # strips.py and thumbnails.py own their own, unrelated caches' locks.
        owners -= {"strips.py", "thumbnails.py"}
        self.assertLessEqual(
            owners, {"async_runner.py"},
            "The async lock belongs to the runner. Owners found: %s"
            % sorted(owners))


class TestThePlayerIsReachedThroughOneGateway(unittest.TestCase):
    """The browser talks to the player through ``PlayerGateway``, not by
    importing ``playerManager`` wherever it is convenient.

    Step 5 of ``docs/ARCHITECTURE_TARGET.md`` §3. Before it, ``ui.py`` held
    61 of the browser's 68 cross-package imports — which is what made it the
    boundary in practice — but two other modules had quietly grown their own
    direct line to a singleton. Each of those is a place the eventual page
    objects could not be constructed without dragging ``player.py`` in.

    Deliberately narrow. This forbids reaching for the *live singletons*; it
    says nothing about importing constants or data-layer types, which are
    shape dependencies rather than service ones:

    * ``config.py`` imports ``AUDIO_PASSTHROUGH_CODECS`` — a tuple.
    * ``downloads.py`` imports status/origin constants from ``sync.db``.
    * ``repository.py`` imports ``SyncDB`` because the offline catalog *is*
      the data source it wraps. That is the data layer, not a service call.
    """

    BROWSER = os.path.join(PKG, "mpvtk_browser")

    #: singleton name -> modules allowed to import it directly.
    #:
    #: ``ui.py`` is allowed for three of them because what remains in it after
    #: the extraction is ``UserInterface``, the **composition root**: it
    #: builds the browser and wires callbacks *onto* playerManager
    #: (``on_mpv_gone``, ``on_playstate``, …), attaches the renderer to the
    #: live mpv handle, and registers the event handler. Routing that through
    #: the gateway would mean ten pass-through setters whose only job is to
    #: assign an attribute — more indirection, not less coupling. A
    #: composition root touching what it composes is the normal exception.
    #:
    #: What the rule is actually protecting is the *page and view* code, which
    #: must be constructible without ``player.py``.
    #: Paths are relative to mpvtk_browser/ so the gateway package's modules
    #: are named individually. That is deliberate and it is stricter than
    #: what it replaced: "player_gateway.py" was one blanket pass covering
    #: every service. Naming the domain makes each pairing a claim -- the
    #: queue domain may reach playerManager, the users domain may not -- and
    #: a new one has to be added here on purpose.
    #:
    #: Derived by measuring, not by guessing: I wrote a plausible-looking
    #: table first and five entries were wrong.
    SINGLETONS = {
        "playerManager": {"gateway/diagnostics.py", "gateway/hud.py",
                          "gateway/playback.py", "gateway/queue.py",
                          "gateway/syncplay.py", "gateway/transport.py",
                          "gateway/base.py", "ui.py"},
        "clientManager": {"gateway/deps.py", "ui.py"},
        "userManager": {"gateway/lock.py", "gateway/playback.py",
                        "gateway/servers.py", "gateway/users.py", "ui.py"},
        "syncManager": {"config.py", "gateway/downloads.py",
                        "gateway/playback.py", "gateway/servers.py",
                        "gateway/userdata.py"},
        "eventHandler": {"ui.py"},
    }

    def test_no_view_module_reaches_a_singleton(self):
        """The half that matters most, stated separately so it cannot be
        weakened by adding a name to the allowlist above: nothing that draws
        a screen may import a live service."""
        # Derived, not listed. This was 14 bare basenames, which could never
        # match a page (they are "pages/home.py" now) -- so every page, every
        # component and the seven new extractions were covered by neither
        # this test nor a SINGLETONS entry. "View code" is now defined as
        # everything under mpvtk_browser/ that is not allow-listed above,
        # which is the property the docstring actually claims.
        allowed = set()
        for names in self.SINGLETONS.values():
            allowed |= names
        views = {name for name, _p in browser_modules()
                 if name not in allowed}
        offenders = []
        for singleton in self.SINGLETONS:
            for name in sorted(self._imports_of(singleton) & views):
                offenders.append("%s imports %s" % (name, singleton))
        self.assertEqual(
            offenders, [],
            "View code must reach services through the gateway it is given, "
            "or a page cannot be built without player.py:\n  "
            + "\n  ".join(offenders))

    def _imports_of(self, singleton):
        """Every module under mpvtk_browser/ that imports ``singleton``.

        Walks subpackages. It used to list only the top level, which meant
        pages/ and gateway/ could have imported anything they liked and this
        whole class would have kept passing -- the invariant would have gone
        blind exactly as the code moved into them."""
        found = set()
        for root, _dirs, files in os.walk(self.BROWSER):
            if "__pycache__" in root:
                continue
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, self.BROWSER).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=path)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom):
                        continue
                    if any(a.name == singleton for a in node.names):
                        found.add(rel)
        return found

    def test_each_singleton_has_a_single_importer(self):
        offenders = []
        for singleton, allowed in self.SINGLETONS.items():
            actual = self._imports_of(singleton)
            extra = actual - allowed
            for name in sorted(extra):
                offenders.append("%s imports %s directly" % (name, singleton))
        self.assertEqual(
            offenders, [],
            "The browser reaches live services through PlayerGateway. A "
            "direct import is a dependency the page objects cannot be "
            "constructed without:\n  " + "\n  ".join(offenders))

    def test_the_gateway_exists(self):
        """It is a package now — one module per domain behind a composed
        facade. See mpvtk_browser/gateway/__init__.py."""
        pkg = os.path.join(self.BROWSER, "gateway")
        self.assertTrue(
            os.path.isdir(pkg),
            "mpvtk_browser/gateway/ does not exist — this is step 5 of "
            "docs/ARCHITECTURE_TARGET.md §3.")
        self.assertTrue(os.path.exists(os.path.join(pkg, "__init__.py")))


class TestNoTopLevelMutableClassState(unittest.TestCase):
    """A mutable class attribute is shared by every instance.

    The browser's mixins declare ``ROUTES`` dicts as class attributes, which
    is correct and deliberate (``_routes()`` merges them read-only). A
    mutable class attribute that is NOT one of those is almost always an
    instance field that was written in the wrong place — and because the
    browser is a singleton in production, the bug never shows up until a
    second instance exists, which is to say in the tests, which is to say
    after the refactor that introduces one.
    """

    ALLOWED = {"ROUTES", "HEADLESS_ROUTES", "MODULES"}

    @staticmethod
    def _is_settings_schema(cls):
        """conf.Settings declares its whole schema as class attributes — that
        IS the SettingsBase contract (``__annotations__`` for the type, the
        class attribute for the default), so a ``list``-typed setting has
        nowhere else to live. Exempt rather than allow-list, or every future
        list/dict setting has to be added by name."""
        return any(isinstance(b, ast.Name) and b.id.endswith("SettingsBase")
                   or isinstance(b, ast.Attribute) and b.attr.endswith("SettingsBase")
                   for b in cls.bases)

    def test_mutable_class_attributes_are_declared_intentionally(self):
        offenders = []
        for rel, tree in _parsed():
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef):
                    continue
                if self._is_settings_schema(cls):
                    continue
                for stmt in cls.body:
                    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = (stmt.targets if isinstance(stmt, ast.Assign)
                               else [stmt.target])
                    value = stmt.value
                    if not isinstance(value, (ast.List, ast.Dict, ast.Set)):
                        continue
                    for target in targets:
                        if not isinstance(target, ast.Name):
                            continue
                        if target.id in self.ALLOWED:
                            continue
                        # An empty literal is the classic accident; a
                        # populated one is usually a deliberate table.
                        if not (value.elts if hasattr(value, "elts")
                                else value.keys):
                            offenders.append("%s:%d %s.%s" % (
                                rel, stmt.lineno, cls.name, target.id))
        self.assertEqual(
            offenders, [],
            "Empty mutable class attributes are shared across instances; "
            "set these in __init__ instead (or add the name to ALLOWED "
            "with a reason):\n  " + "\n  ".join(offenders))


class TestApiCallsMatchTheClient(unittest.TestCase):
    """Every keyword we hand the API client is one it declares.

    ``get_channel_listing`` passed ``enable_images=False`` -- a real Jellyfin
    query parameter, and one jellyfin-web sends -- to a client method with no
    such argument. Python raises TypeError at the call, so the Live TV channel
    page died on open, and nothing caught it until someone opened one: the
    call sits behind a network fetch on a screen with no unit test, and the
    kwarg reads as correct in the diff.

    A signature check is the only thing that finds this without a server.
    Calls whose target cannot be resolved statically (``getattr(api, name)``)
    and calls that splat a dict (``**kwargs``) are skipped -- the point is
    the literal keywords, which is where the mistake lives.
    """

    @staticmethod
    def _api_class():
        from jellyfin_apiclient_python.api import API

        return API

    @staticmethod
    def _accepts(fn, keyword):
        import inspect

        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):     # pragma: no cover - C callables
            return True
        for p in sig.parameters.values():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if p.name == keyword:
                return True
        return False

    def test_no_call_passes_a_keyword_the_client_does_not_have(self):
        api_cls = self._api_class()
        offenders = []
        for rel, tree in _parsed():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                # `api.method(...)` and `self.api.method(...)`: the object is
                # always spelled `api` where the client is called.
                if not isinstance(fn, ast.Attribute):
                    continue
                owner = fn.value
                name = (owner.id if isinstance(owner, ast.Name)
                        else owner.attr if isinstance(owner, ast.Attribute)
                        else None)
                if name != "api":
                    continue
                method = getattr(api_cls, fn.attr, None)
                if method is None:
                    offenders.append("%s:%d api.%s() does not exist"
                                     % (rel, node.lineno, fn.attr))
                    continue
                for kw in node.keywords:
                    if kw.arg is None:      # **splat, not a literal keyword
                        continue
                    if not self._accepts(method, kw.arg):
                        offenders.append(
                            "%s:%d api.%s(%s=...)"
                            % (rel, node.lineno, fn.attr, kw.arg))
        self.assertEqual(
            offenders, [],
            "These calls raise TypeError the moment they run:\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
