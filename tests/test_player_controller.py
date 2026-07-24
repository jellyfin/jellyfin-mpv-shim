"""``_PlayerController`` — the browser's boundary to everything else.

Step 4 of ``docs/ARCHITECTURE_TARGET.md`` §3, and a hard prerequisite for
steps 5+. Coverage put ``ui.py`` at **41.6%**, the lowest of any module the
refactor touches, and this class is most of it: 99 methods, and the ones
with the least coverage are ``_connect``, ``on_mpv_recreated``,
``login_servers`` and ``switch_user`` — startup, teardown and recovery,
which is precisely what hand-testing does not reach.

The inversion matters. ``app.py`` — the god object being decomposed — sits
at 88%. The *seam it is being decomposed across* was the untested part. That
is the wrong way round, and this file is the correction.

**The shape of the class is what makes it testable in bulk.** Almost every
method is: lazily import a singleton, delegate one call, catch ``Exception``,
return a documented fallback. The lazy import is not an accident — it keeps
``mpvtk_browser`` importable without ``player.py`` — and it is also the seam
that lets a test substitute the singleton with no patching machinery.

So the coverage here is deliberately two-layered:

* :class:`TestEveryGuardedMethodSwallows` sweeps the whole class and proves
  the *contract* holds uniformly — a failing collaborator never propagates
  out of a guarded method. That is the property step 5's ``PlayerGateway``
  must preserve, and it is checked for all 40 guarded methods rather than
  the handful someone thought to write out.
* The rest are behavioural, for the methods that do real work rather than
  delegate: the offline watched queue, the refusal semantics, the
  three-way return of ``switch_user``.
"""

import inspect
import sys
import types
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import ui as ui_mod  # noqa: E402

CTL = ui_mod._PlayerController


class Boom(Exception):
    """What a broken collaborator raises."""


class _BrokenClients(dict):
    """``clientManager.clients``: present, but every client in it is broken.

    ``.get()`` answers for any uuid so the sweep reaches the code *past* the
    "no server" early return, which is where the try blocks are.
    """

    def get(self, *_a, **_k):
        return BrokenService()


class BrokenService:
    """Reachable, but every *operation* fails.

    The first version of this raised on attribute access too, and twelve
    methods "failed" the sweep as a result. That was the fake being wrong,
    not the code: ``syncManager.db`` and ``clientManager.clients`` are plain
    data attributes that cannot raise in production, and several methods
    read them *outside* their try block precisely because of that. Worse,
    raising a non-``AttributeError`` from ``__getattr__`` defeats
    ``getattr(obj, "db", None)``, which is the idiom four of them use.

    So the realistic model — and the one that actually exercises the guards
    rather than short-circuiting before them — is: attribute traversal
    succeeds, calls fail. That models mpv gone, the server unreachable, the
    catalog closed, the user store corrupt.
    """

    #: Attributes that must answer with real containers, because callers
    #: treat them as data rather than as API surface.
    _CONTAINERS = {
        "clients": _BrokenClients,
        "credentials": list,
    }

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        factory = self._CONTAINERS.get(name)
        if factory is not None:
            return factory()
        # Everything else is traversable and callable — so `db.list()` gets
        # as far as the call before failing, inside the try that expects it.
        return BrokenService()

    def __call__(self, *a, **k):
        raise Boom("call")

    def __iter__(self):
        # A list-shaped attribute (mpv's chapter_list, a track list) reads as
        # empty rather than exploding: `for ch in playerManager._player
        # .chapter_list or []` sits OUTSIDE its try, correctly, because in
        # production that attribute is a list or None and cannot raise.
        return iter(())


def _dummy_args(fn):
    """Plausible arguments for a method we only intend to fail.

    Values barely matter — the collaborator fails on first call — so this
    mostly needs to satisfy arity. The one distinction worth making is a
    parameter that is obviously a callback: handing it a string produces a
    ``TypeError`` from the method's own body, which would look like a
    contract failure while actually being this helper's fault.
    """
    sig = inspect.signature(fn)
    args = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is not param.empty:
            continue
        if name in ("fn", "cb", "callback", "func") or name.startswith("on_"):
            args.append(lambda *a, **k: None)
        else:
            args.append("x")
    return args


#: Methods the sweep must not call, because they reach OUTSIDE the process
#: and no injected stand-in intercepts them. Learned the hard way: an
#: earlier version of this sweep called ``open_config_folder``, which does
#: ``subprocess.Popen(["xdg-open", path])`` — and duly opened a file manager
#: window on the developer's desktop mid-test-run.
#:
#: Each still deserves a test; they just need one that stubs the specific
#: outside call, which is a different exercise from this sweep.
SIDE_EFFECTING = {
    "open_config_folder",   # spawns a file manager (xdg-open / open / startfile)
    "open_url",             # spawns a browser (webbrowser.open)
    "copy_text",            # writes into the real config dir, drives the clipboard
}


def _controller_methods():
    """(guarded, side_effecting_names) parsed from the source.

    Read from the AST rather than listed by hand, so a method added without
    a test is swept automatically — the same discipline
    ``test_mpvtk_ui_wiring.py`` uses for the callback wiring.
    """
    import ast

    tree = ast.parse(inspect.getsource(ui_mod))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "_PlayerController")
    guarded, defined, rethrowing = [], set(), []
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        defined.add(fn.name)
        if fn.name in SIDE_EFFECTING:
            continue
        if not any(isinstance(n, ast.Try) for n in ast.walk(fn)):
            continue
        if _rethrows(fn):
            # Catches to LOG and then re-raises. That is a different
            # contract -- the caller is expected to see the failure -- so it
            # does not belong in the swallow sweep.
            rethrowing.append(fn.name)
        else:
            guarded.append(fn.name)
    return guarded, defined, rethrowing


def _rethrows(fn):
    """Does every ``except`` in this function end by re-raising?"""
    import ast

    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try)
                for h in n.handlers]
    if not handlers:
        return False
    return all(any(isinstance(s, ast.Raise) for s in ast.walk(h))
               for h in handlers)


def _guarded_methods():
    return _controller_methods()[0]


class _Sandbox:
    """Swap every collaborator for a raising stand-in, and put them back.

    playerManager / userManager / syncManager are imported lazily inside the
    methods, so they are patched on their own modules; clientManager is a
    module global of ui.py.
    """

    TARGETS = (
        ("jellyfin_mpv_shim.player", "playerManager"),
        ("jellyfin_mpv_shim.users", "userManager"),
        ("jellyfin_mpv_shim.sync.manager", "syncManager"),
        ("jellyfin_mpv_shim.clients", "clientManager"),
        ("jellyfin_mpv_shim.event_handler", "eventHandler"),
    )

    def __enter__(self):
        import importlib

        self._saved = []
        for mod_name, attr in self.TARGETS:
            module = importlib.import_module(mod_name)
            self._saved.append((module, attr, getattr(module, attr, None)))
            setattr(module, attr, BrokenService())
        # ui.py bound clientManager at import time, so the module global has
        # to be replaced too or the sweep tests the real one.
        self._ui_saved = ui_mod.clientManager
        ui_mod.clientManager = BrokenService()
        return self

    def __exit__(self, *exc):
        for module, attr, original in self._saved:
            if original is not None:
                setattr(module, attr, original)
        ui_mod.clientManager = self._ui_saved
        return False


class TestEveryGuardedMethodSwallows(unittest.TestCase):
    """A guarded method must never propagate a collaborator's failure.

    Every one of these runs on the browser's loop thread or its worker pool.
    On the loop thread an escape kills the render loop; on a pool worker it
    kills the worker silently. Neither has a caller in a position to recover,
    which is why the class catches at all — this proves it actually does,
    everywhere, rather than in the places someone remembered.
    """

    def test_there_are_guarded_methods_to_sweep(self):
        # Guards the guard: if the AST walk stops finding anything (a rename,
        # a class restructure), the sweep below would pass by testing nothing
        # at all. A floor, not a target -- it should rise, never fall.
        self.assertGreaterEqual(len(_guarded_methods()), 35)

    def test_the_exclusion_list_is_not_stale(self):
        """Every name in SIDE_EFFECTING must still exist.

        A renamed or deleted method would otherwise leave a silent hole: the
        sweep would skip nothing, and nobody would notice that the reason
        for the exclusion had gone away.
        """
        _guarded, defined, _rethrow = _controller_methods()
        self.assertEqual(
            SIDE_EFFECTING - defined, set(),
            "SIDE_EFFECTING names methods that no longer exist; remove them "
            "so the sweep covers what it should")

    def test_no_guarded_method_propagates(self):
        escaped = []
        with _Sandbox():
            for name in _guarded_methods():
                fn = getattr(CTL, name, None)
                if fn is None or not callable(fn):
                    continue
                ctl = CTL()
                bound = getattr(ctl, name)
                try:
                    bound(*_dummy_args(fn))
                except Boom:
                    escaped.append("%s: the collaborator's error escaped"
                                   % name)
                except (TypeError, AttributeError) as exc:
                    # Not the contract under test, but a real signal: the
                    # sweep could not call it, so it is NOT being covered.
                    escaped.append("%s: could not be exercised (%s: %s)"
                                   % (name, type(exc).__name__, exc))
                except Exception as exc:            # noqa: BLE001
                    escaped.append("%s: raised %r instead of returning a "
                                   "fallback" % (name, exc))
        self.assertEqual(
            escaped, [],
            "Guarded methods must turn a broken collaborator into their "
            "documented fallback:\n  " + "\n  ".join(escaped))


class TestOfflineWatchedQueue(unittest.TestCase):
    """``set_watched`` with no server writes to the sync catalog.

    Returning silently left the UI showing an optimistic tick that reverted
    on the next reload and never reached the server.
    """

    class FakeDB:
        def __init__(self, complete=(), rows=()):
            self._complete = set(complete)
            self._rows = list(rows)
            self.playstate = []
            self.userdata = []

        def is_complete(self, item_id):
            return item_id in self._complete

        def list(self, status=None):
            return self._rows

        def upsert_playstate(self, server, item_id, played=False):
            self.playstate.append((server, item_id, played))

        def update_userdata(self, item_id, played=False):
            self.userdata.append((item_id, played))

    def _with_db(self, db):
        import jellyfin_mpv_shim.sync.manager as manager_mod

        original = manager_mod.syncManager
        fake = types.SimpleNamespace(db=db)
        manager_mod.syncManager = fake
        self.addCleanup(setattr, manager_mod, "syncManager", original)

    def _offline(self):
        original = ui_mod.clientManager
        ui_mod.clientManager = types.SimpleNamespace(clients={})
        self.addCleanup(setattr, ui_mod, "clientManager", original)

    def test_a_downloaded_item_is_queued_and_marked(self):
        db = self.FakeDB(complete={"m1"})
        self._with_db(db)
        self._offline()
        self.assertTrue(CTL().set_watched("s1", "m1", True))
        self.assertEqual(db.playstate, [("s1", "m1", True)])
        # userdata too: the overlay and the watched-based delete read that,
        # not the pending queue, so without it the mark is invisible.
        self.assertEqual(db.userdata, [("m1", True)])

    def test_a_series_fans_out_to_its_downloaded_episodes(self):
        db = self.FakeDB(rows=[
            {"item_id": "e1", "server_uuid": "s1",
             "series_id": "sh1", "season_id": None},
            {"item_id": "e2", "server_uuid": None,
             "series_id": "sh1", "season_id": None},
            {"item_id": "other", "server_uuid": "s1",
             "series_id": "sh2", "season_id": None},
        ])
        self._with_db(db)
        self._offline()
        self.assertTrue(CTL().set_watched("s1", "sh1", True))
        self.assertEqual([i for _s, i, _p in db.playstate], ["e1", "e2"])
        # A row with no server of its own inherits the one asked for.
        self.assertEqual([s for s, _i, _p in db.playstate], ["s1", "s1"])

    def test_unwatching_offline_is_refused_not_half_applied(self):
        """The pending queue is advance-only, so un-watching cannot be
        represented. Refusing is honest; queueing it would silently drop."""
        db = self.FakeDB(complete={"m1"})
        self._with_db(db)
        self._offline()
        self.assertFalse(CTL().set_watched("s1", "m1", False))
        self.assertEqual(db.playstate, [])

    def test_nothing_downloaded_matching_is_a_refusal(self):
        db = self.FakeDB()
        self._with_db(db)
        self._offline()
        self.assertFalse(CTL().set_watched("s1", "sh9", True))

    def test_an_empty_item_id_never_reaches_the_catalog(self):
        db = self.FakeDB(complete={""})
        self._with_db(db)
        self._offline()
        self.assertFalse(CTL().set_watched("s1", "", True))
        self.assertEqual(db.playstate, [])


class TestOnlineDelegation(unittest.TestCase):
    """With a server present the change goes straight to it, and a refusal
    is reported rather than swallowed into a fake success."""

    def _client(self, fail=False):
        calls = []

        class Jellyfin:
            def item_played(self, item_id, played):
                calls.append(("played", item_id, played))
                if fail:
                    raise Boom("nope")

            def favorite(self, item_id, value):
                calls.append(("favorite", item_id, value))
                if fail:
                    raise Boom("nope")

        client = types.SimpleNamespace(jellyfin=Jellyfin())
        original = ui_mod.clientManager
        ui_mod.clientManager = types.SimpleNamespace(clients={"s1": client})
        self.addCleanup(setattr, ui_mod, "clientManager", original)
        return calls

    def test_watched_reaches_the_server(self):
        calls = self._client()
        self.assertTrue(CTL().set_watched("s1", "m1", True))
        self.assertEqual(calls, [("played", "m1", True)])

    def test_a_rejected_watched_change_reports_failure(self):
        self._client(fail=True)
        self.assertFalse(CTL().set_watched("s1", "m1", True))

    def test_favorite_reaches_the_server(self):
        calls = self._client()
        self.assertTrue(CTL().set_favorite("s1", "m1", True))
        self.assertEqual(calls, [("favorite", "m1", True)])

    def test_favorite_offline_is_a_refusal(self):
        """Favorites have no offline queue, so this must be a refusal rather
        than a silent no-op — the caller rolls its optimistic heart back."""
        original = ui_mod.clientManager
        ui_mod.clientManager = types.SimpleNamespace(clients={})
        self.addCleanup(setattr, ui_mod, "clientManager", original)
        self.assertFalse(CTL().set_favorite("s1", "m1", True))


class TestSwitchUserReturnsThreeDistinctThings(unittest.TestCase):
    """``switch_user`` returns a source, ``False`` for a bad PIN, or ``None``
    when the switch worked but there is nothing to browse.

    The last two being distinct is the point: reporting an unreachable
    server as a bad PIN is what made a correct PIN look wrong.
    """

    def _users(self, exists=True, locked=False, pin_ok=True):
        import jellyfin_mpv_shim.users as users_mod

        fake = types.SimpleNamespace(
            get=lambda uid: {"id": uid} if exists else None,
            is_locked=lambda uid: locked,
            verify_pin=lambda uid, pin: pin_ok,
        )
        original = users_mod.userManager
        users_mod.userManager = fake
        self.addCleanup(setattr, users_mod, "userManager", original)

    def _clients(self):
        original = ui_mod.clientManager
        ui_mod.clientManager = types.SimpleNamespace(
            switch_user=lambda uid: None, clients={}, credentials=[])
        self.addCleanup(setattr, ui_mod, "clientManager", original)

    def test_an_unknown_user_is_refused(self):
        self._users(exists=False)
        self._clients()
        self.assertIs(CTL().switch_user("u9"), False)

    def test_a_wrong_pin_is_refused(self):
        self._users(locked=True, pin_ok=False)
        self._clients()
        self.assertIs(CTL().switch_user("u1", pin="0000"), False)

    def test_a_correct_pin_proceeds_past_the_gate(self):
        self._users(locked=True, pin_ok=True)
        self._clients()
        ctl = CTL()
        ctl.rebuild_source = lambda: "SOURCE"
        self.assertEqual(ctl.switch_user("u1", pin="1234"), "SOURCE")

    def test_a_switch_with_nothing_to_browse_is_not_a_pin_failure(self):
        self._users()
        self._clients()
        ctl = CTL()
        ctl.rebuild_source = lambda: None
        ctl.offline_source = lambda: None
        result = ctl.switch_user("u1")
        self.assertIsNone(result)
        self.assertIsNot(result, False, "an empty switch must not read as a "
                                        "rejected PIN")

    def test_it_falls_back_to_the_offline_catalog(self):
        self._users()
        self._clients()
        ctl = CTL()
        ctl.rebuild_source = lambda: None
        ctl.offline_source = lambda: "OFFLINE"
        self.assertEqual(ctl.switch_user("u1"), "OFFLINE")


class TestMethodsThatDeliberatelyRaise(unittest.TestCase):
    """Not everything swallows, and the exceptions are deliberate.

    ``add_user`` / ``rename_user`` let the failure through because catching
    made the field clear and nothing happen — the caller shows the message.
    Pinning it stops a well-meaning "add a try/except everywhere" pass from
    silently restoring that bug.
    """

    def _users_that_reject(self):
        import jellyfin_mpv_shim.users as users_mod

        def reject(*_a, **_k):
            raise ValueError("that name is taken")

        original = users_mod.userManager
        users_mod.userManager = types.SimpleNamespace(
            add_user=reject, rename_user=reject)
        self.addCleanup(setattr, users_mod, "userManager", original)

    def test_add_user_propagates(self):
        self._users_that_reject()
        with self.assertRaises(ValueError):
            CTL().add_user("dup")

    def test_rename_user_propagates(self):
        self._users_that_reject()
        with self.assertRaises(ValueError):
            CTL().rename_user("u1", "dup")

    def test_the_log_and_rethrow_set_is_what_we_think_it_is(self):
        """``_sync`` catches only to log, then re-raises: the SyncPlay
        actions built on it need the caller to see the failure.

        Pinned by name because the swallow sweep excludes anything that
        re-raises. If a method silently joined this set the sweep would stop
        covering it and nothing else would notice.
        """
        _guarded, _defined, rethrowing = _controller_methods()
        self.assertEqual(
            sorted(rethrowing), ["_sync"],
            "A method started (or stopped) re-raising. If that is intended, "
            "update this list; the swallow sweep skips these.")


if __name__ == "__main__":
    unittest.main()
