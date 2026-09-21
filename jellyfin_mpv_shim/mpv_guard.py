"""A property write mpv refuses must not become a Python attribute.

Both backends absorb a refused write instead of reporting it, and what they
leave behind wins every later read of that property for the life of the
process:

* python-mpv's ``MPV.__setattr__`` catches ``AttributeError`` -- which is what
  a property this build does not have (``MPV_ERROR_PROPERTY_NOT_FOUND``) and
  one that is unavailable right now (``PropertyUnavailableError``) both raise
  -- and falls through to ``object.__setattr__``;
* python-mpv-jsonipc's sends ``set_property`` only for a name in the
  ``property-list`` it read at connect, and does ``object.__setattr__`` for
  anything else. Its half of the defect is the absent-property one only: a
  runtime property that exists but has no value is in that list, so the write
  goes out and errors instead.

``__getattr__`` is only consulted when normal lookup fails, so after that
neither backend ever reads that property from mpv again -- and a later
*successful* write does not clear it either (measured, python-mpv 1.0.8).

**Why this is not 84 patches.** The shim writes 84 property names through this
object and reads 14 of them back, and *which* of those a refusal reaches is a
property of the mpv the user has rather than of this tree. So the live sites
cannot be enumerated by reading them, which is the definition of a rule that
belongs in one place. #761 and #765 are the shipped instance (the resume
position, fixed at its site as well); ``set_osd_settings`` is the second,
where a ``try/except`` written for exactly this absence cannot fire because
the write it defends does not raise.

What the subclass does, and nothing else:

* a write mpv refuses **leaves no attribute behind**;
* it is **recorded and logged, and execution continues** -- in production and
  under test alike, identically. It never raises: 14 of those 84 writes have
  further statements after them inside the same ``try``, worst of all the
  picture-view/playback handoff at ``player_window.py:522`` with six, so a
  raise would make a test run take a path production never takes;
* reads, commands, observers, key bindings, ``event_callback``, ``terminate``
  and ``handle`` are untouched.

The **assertion** is the suite's, not this module's:
``tests/integration/_harness.py``'s ``watch_refused_writes`` is registered for
every integration and e2e case without one opting in, and
``ALLOWED_REFUSED_WRITES`` beside it is the executable form of "this absence
is a decision somebody made".
"""
import logging
import threading
from collections import deque, namedtuple

log = logging.getLogger("mpv_guard")

#: How many refusals are kept. The count is exact; the records are a tail,
#: because a refused write on a per-item path would otherwise grow without
#: bound in a long session.
HISTORY = 16

#: What was refused. The value is kept for a reader with a debugger and is
#: deliberately **never logged or formatted into a message**:
#: ``http_header_fields`` carries this server's ``Authorization`` header, and
#: a refusal of it is exactly the case where somebody would want the log
#: (docs/auth-headers.md).
RefusedWrite = namedtuple("RefusedWrite", "name value error")

_classes = {}
_classes_lock = threading.Lock()


def _write_libmpv(player, name, value):
    """python-mpv's own write, without its fallback.

    ``_set_property`` and the underscore-to-dash spelling are exactly what
    ``MPV.__setattr__`` does; only the ``except AttributeError`` around it is
    left out.

    Reaching into the library is the deliberate half. The alternative is to
    let it swallow the failure and then infer one from ``__dict__``, which
    cannot tell a refusal from a stand-in that stores its properties as
    ordinary attributes -- and ``FakeMPV`` is exactly that. Both names are
    pinned by ``tests/test_mpv_guard.py`` against the installed library, so
    the day either goes away is a failure here rather than a silent stop.
    """
    player._set_property(name.replace("_", "-"), value)


def _write_jsonipc(player, name, value):
    """python-mpv-jsonipc's own write, without its fallback.

    Its ``__setattr__`` asks whether the name is in the property list mpv
    reported at connect and quietly makes a Python attribute when it is not,
    so that absence check *is* the refusal.
    """
    if name not in player.properties:
        raise AttributeError(name)
    player.command("set_property", name.replace("_", "-"), value)


def _writer_for(base):
    """The write primitive for a backend class, or None if it brings one.

    The jsonipc test is ``bind_property_observer``, which is the same class
    capability ``mpv_events`` dispatches on -- one discriminator for the
    backend, rather than a second one that can disagree with it.
    """
    if hasattr(base, "_jms_write"):
        return None                       # a stand-in that models a refusal
    if hasattr(base, "bind_property_observer"):
        return _write_jsonipc
    if hasattr(base, "_set_property"):
        return _write_libmpv
    raise TypeError(
        "%s is neither backend and models no write of its own" % (base,))


def guarded(base):
    """The guarded subclass of ``base``: one class per base, built once.

    Cached because ``_init_mpv`` runs again on every mpv re-creation (an
    idle-quit then a cast, ``set_browse_window``, ``force_window``), and a
    fresh class per instance would leak one each time -- and would put the
    recorded refusals somewhere the next instance cannot see.
    """
    if not isinstance(base, type):
        # Not subclassable, so there is nothing to guard. Two unit modules
        # patch `mpv.MPV` with a Mock whose `side_effect` is a factory, to
        # drive the option-retry loop in `_construct_mpv` without a real
        # window; requiring a class there would mean a stand-in shaped by
        # this module rather than by the retry it tests. The real backends
        # are classes, and `tests/test_mpv_guard.py` asserts that of the
        # installed libraries so this branch cannot become the production
        # path in silence.
        return base
    with _classes_lock:
        cls = _classes.get(base)
        if cls is None:
            cls = _make(base)
            _classes[base] = cls
        return cls


def _make(base):
    writer = _writer_for(base)
    # On the class rather than on instances, because mpv is torn down and
    # re-created inside a single test and a counter on the instance goes away
    # with the instance whose failures it recorded.
    refusals = deque(maxlen=HISTORY)
    count = [0]
    record_lock = threading.Lock()

    class GuardedMPV(base):
        """``base`` with one difference: a refused write leaves nothing."""

        _jms_armed = False
        _jms_passthrough = frozenset()
        #: Read by the suite's hook through the instance; shared by every
        #: instance of this class, which is what makes it survive a
        #: re-creation mid-test.
        _jms_refusals = refusals

        def __setattr__(self, name, value):
            if (not self._jms_armed
                    or name.startswith("_")
                    or name == "handle"
                    or name in self._jms_passthrough):
                # Not a property write, or not ours yet: the backend's own
                # semantics, unchanged. Everything either library assigns to
                # itself travels this way -- python-mpv's `osd`/`raw`/`lazy`
                # proxies, jsonipc's `observer_id` counter -- which is what
                # `arm` snapshots.
                super().__setattr__(name, value)
                return
            try:
                self._jms_write(name, value)
            except AttributeError as error:
                # Exactly what the two backends catch, and nothing more: a
                # bad *value* is a `TypeError` and a dead core is a
                # `SystemError` (`mpv.ShutdownError`), neither of which is a
                # refusal, and both still reach the caller.
                self._jms_record(name, value, error)

        if writer is not None:
            def _jms_write(self, name, value):
                writer(self, name, value)

        def _jms_record(self, name, value, error):
            with record_lock:
                count[0] += 1
                refusals.append(RefusedWrite(name, value, error))
            # The name and the KIND of error, never the value and never the
            # exception itself: libmpv puts the value in its args
            # (`(msg, -10, (handle, b'http-header-fields', b'Authorization:
            # MediaBrowser Token="..."'))`), so logging the error is logging
            # the token. See RefusedWrite.
            detail = type(error).__name__
            code = next((a for a in error.args if isinstance(a, int)), None)
            if code is not None:
                detail = "%s %d" % (detail, code)
            log.warning("mpv would not accept the property %r (%s). Nothing "
                        "was written and nothing was left behind.",
                        name, detail)

        @property
        def failed_writes(self):
            """How many writes this mpv refused. Exact, unlike the tail."""
            return count[0]

        @property
        def last_failed_write(self):
            return refusals[-1] if refusals else None

    GuardedMPV.__name__ = "Guarded" + base.__name__
    GuardedMPV.__qualname__ = GuardedMPV.__name__
    return GuardedMPV


def arm(player, passthrough=None):
    """Start guarding this player's property writes.

    **After construction has returned**, because both libraries assign their
    own bookkeeping through ``__setattr__`` on the way up and some of it is a
    plain Python attribute: python-mpv's ``osd``, ``raw``, ``lazy``,
    ``strict``, ``file_local``, ``overlays``, ``overlay_ids`` and
    ``mpv_version_tuple``; jsonipc's ``properties``, ``observer_id`` and
    ``keybind_id`` -- and jsonipc keeps assigning the last two, once per
    observer and per key binding, for the life of the object. So the names
    already on the object when it is handed over are the library's, and a
    later write to one of them is not a property write at all.

    ``passthrough`` overrides that snapshot, and a stand-in that stores its
    properties as ordinary attributes has to pass ``()``: for ``FakeMPV`` the
    snapshot would be every property it models, which is precisely the set a
    test injects an absence into.
    """
    if not hasattr(type(player), "_jms_armed"):
        return                      # unguarded -- see `guarded`
    if passthrough is None:
        passthrough = player.__dict__
    player._jms_passthrough = frozenset(passthrough)
    player._jms_armed = True


def refusals(player):
    """Every refusal this player's class has recorded, oldest first."""
    return tuple(getattr(player, "_jms_refusals", ()))


def refusals_since(player, mark):
    """The refusals recorded since ``mark``, a count from ``refused_count``.

    The record is shared by every instance of the class and therefore by
    every test in the process, so "what was refused" is only ever a question
    about a window. Asking it any other way is how one case fails for the
    refusal of the case before it. Only the last ``HISTORY`` are kept, so
    this is what is still *known* about that window; the count is the exact
    answer to whether there were any.
    """
    new = refused_count(player) - mark
    if new <= 0:
        return ()
    return refusals(player)[-new:]


def refused_count(player):
    """How many writes were refused, which is exact where `refusals` is a tail.

    Zero for anything unguarded, so a caller does not have to ask first: a
    stand-in nobody wrapped has no failures to report rather than an error.
    """
    return getattr(player, "failed_writes", 0)
