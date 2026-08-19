# ---------------------------------------------------------------------------
# BEFORE EDITING THIS FILE, READ docs/mpv-backends.md.
#
# It carries the constraints that have no line to sit on -- the danger is in
# the line you are about to add, so no inline comment can warn you:
#
#   * mpv is NOT re-created between queue items, so any global option written
#     for one item is still set for the next, including a next item we
#     deliberately refuse to set it for;
#   * a bound method cannot be a libmpv ``property_observer`` (use _observe);
#   * an input section without ``allow-hide-cursor`` stops the mouse cursor
#     ever hiding again;
#   * the two backends raise different things and answer property reads at
#     wildly different cost.
# ---------------------------------------------------------------------------

from threading import Event, Thread
from typing import Optional

# How often the observer wait re-reads the property directly. Property-change
# events are the fast path; the poll is a safety net for delivery loss (seen in
# the field on the external-mpv JSON IPC transport), so it can be leisurely.
POLL_INTERVAL_SECS = 0.5


def observe(instance, name, handler):
    """Register ``handler`` for property ``name`` on either backend.

    Deliberately NOT python-mpv's ``property_observer`` decorator. That writes
    an ``unobserve_mpv_properties`` attribute onto the callback it is given,
    and a **bound method has no __dict__ to write it to** -- it raises
    AttributeError. jsonipc and the test fake both accept one, so a handler
    that stops being a plain closure breaks on exactly one backend, at
    runtime, and nowhere else.

    Backend picked by class capability, like ``wait_property`` below and for
    the same reason: libmpv's ``__getattr__`` turns an unknown *instance*
    attribute into a property read, so an instance-level hasattr would be
    both wrong and wasteful.

    Returns the token :func:`unobserve` needs (jsonipc hands back an observer
    id; libmpv identifies a registration by name and handler, so None).

    Here rather than on ``PlayerManager`` because importing ``player`` builds
    a real mpv as a side effect, and the one place that most needs to check
    this dispatch against a real backend is a test that must not.
    """
    if hasattr(type(instance), "bind_property_observer"):
        return instance.bind_property_observer(name, handler)
    instance.observe_property(name, handler)
    return None


def unobserve(instance, name, handler, token=None):
    """Undo :func:`observe`.

    Nothing in the app calls this -- mpv is torn down whole, handle and
    observers together. A caller that outlives its handle's observers has to,
    though: libmpv segfaults the interpreter on the way out otherwise.
    """
    if hasattr(type(instance), "bind_property_observer"):
        if token is not None:
            instance.unbind_property_observer(token)
    else:
        instance.unobserve_property(name, handler)


def wait_property(
    instance,
    name: str,
    cond=lambda x: True,
    timeout: Optional[int] = None,
    skip_initial: bool = False,
    abort: Optional[Event] = None,
    satisfied_by: Optional[Event] = None,
):
    """Block until MPV property ``name`` reports a value satisfying ``cond``.

    Works with both backends; the backend is picked by class capability, so
    this carries no global state and is testable with a fake ``instance``.

    ``skip_initial`` drops a value belonging to the *previous* file -- both
    backends deliver the property's current value the instant an observer
    registers, and with a prior file still loaded that value is the old one.

    The wait is **poll-assisted on its own daemon thread**, and that is not
    redundancy: the external backend's IPC pipeline has been seen in the field
    to drop property-change notifications, which turned a fine playback start
    into a hard "no duration" timeout that killed the session. **Do not
    simplify it away.** Its own thread because a jsonipc property read blocks
    for up to 120s, which would stretch ``timeout`` past being a hard bound.

    ``abort`` ends the wait as a failure, ``satisfied_by`` as a success -- the
    latter for a stream that will never report a duration at all. Both are
    observed by the poll thread, so they land within one poll interval.

    The residual race, and the full argument for each parameter:
    docs/mpv-backends.md section 9.
    """
    event = Event()
    # Set only by a genuine cond() match, so the abort and timeout paths both
    # report failure without needing to re-check the property afterwards.
    satisfied = False

    # Sample before registering the observer so the handler (which may fire on
    # the mpv event thread the moment we register) never races this write.
    skip = False
    stale_value = None
    if skip_initial:
        try:
            stale_value = getattr(instance, name)
            skip = cond(stale_value)
        except Exception:
            skip = False

    def handler(_name, value):
        nonlocal skip, satisfied
        if skip:
            skip = False
            # Only drop a re-delivery of the sampled stale value; a value
            # that already differs is fresh and must count.
            if value == stale_value:
                return
        if cond(value):
            satisfied = True
            event.set()

    # Discriminate on the class, not the instance: libmpv's __getattr__ turns
    # unknown instance attributes into IPC property gets, so an instance-level
    # hasattr would be both wrong and wasteful.
    use_ext_mpv = hasattr(type(instance), "bind_property_observer")

    if use_ext_mpv:
        observer_id = instance.bind_property_observer(name, handler)
    else:
        instance.observe_property(name, handler)

    # Poll fallback on a separate thread (see docstring); the main wait below
    # keeps the caller's timeout as a hard bound.
    stop_poll = Event()

    def poller():
        nonlocal satisfied
        while not stop_poll.wait(POLL_INTERVAL_SECS):
            # Checked before the read: once the caller has given up, a
            # property read on a wedged mpv could block for minutes.
            if abort is not None and abort.is_set():
                event.set()  # satisfied stays False -> wait_property returns False
                return
            # Checked after abort so a failed load stays a failure even if
            # both fire: mpv can report a file loaded and then immediately
            # fail it.
            if satisfied_by is not None and satisfied_by.is_set():
                satisfied = True
                event.set()
                return
            try:
                value = getattr(instance, name)
            except Exception:
                continue  # property unavailable / player busy; keep polling
            # A polled value equal to the stale sample may simply be the old
            # state still in place, so only the observer (which sees the
            # actual change sequence) may accept it.
            if cond(value) and not (skip_initial and value == stale_value):
                satisfied = True
                event.set()
                return

    poll_thread = Thread(target=poller, daemon=True,
                         name="wait-property-poll")
    poll_thread.start()

    # Event.wait(None) blocks indefinitely and returns True, so one wait
    # covers both the bounded and unbounded cases.
    event.wait(timeout=timeout)
    stop_poll.set()

    if use_ext_mpv:
        instance.unbind_property_observer(observer_id)
    else:
        instance.unobserve_property(name, handler)
    # Not event.is_set(): the abort path sets it to wake this thread without
    # the property ever having satisfied cond().
    return satisfied
