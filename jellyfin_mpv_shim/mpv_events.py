from threading import Event, Thread
from typing import Optional

# How often the observer wait re-reads the property directly. Property-change
# events are the fast path; the poll is a safety net for delivery loss (seen in
# the field on the external-mpv JSON IPC transport), so it can be leisurely.
POLL_INTERVAL_SECS = 0.5


def wait_property(
    instance,
    name: str,
    cond=lambda x: True,
    timeout: Optional[int] = None,
    skip_initial: bool = False,
    abort: Optional[Event] = None,
):
    """Block until MPV property ``name`` reports a value satisfying ``cond``.

    Works with both backends: libmpv (python-mpv, ``observe_property``) and
    external mpv (python-mpv-jsonipc, ``bind_property_observer``). The backend
    is picked by class capability so this helper carries no global state and is
    testable with a fake ``instance``.

    ``skip_initial`` guards against a stale value from a *previous* file. Both
    backends deliver one initial property-change notification carrying the
    property's CURRENT value the instant the observer registers. When a prior
    file is still loaded (cast-while-playing, or auto-advance with keep_open
    holding the finished file), that value belongs to the OLD file, so
    accepting it would act on the wrong item. With ``skip_initial`` we sample
    the property at registration: if it already satisfies ``cond`` it is a
    stale ready value, so we drop the first notification (mpv re-delivers that
    same value) and only accept a later change. If the property is not yet
    ready (``cond`` fails on the sample, e.g. ``duration`` is None between
    files) there is nothing stale to skip, so we accept the first qualifying
    notification -- this keeps the normal first-play path working even if the
    file loads before the observer is processed.

    Residual race: if the NEW file finishes loading before we sample, the
    sample is already the fresh value. The first notification is only dropped
    when it re-delivers the exact sampled value, so a fresh value that differs
    from the stale one is accepted; only a new value *equal* to the stale one
    (same-duration reload) is indistinguishable, and the caller's ``timeout``
    bounds that case -- it then degrades exactly like any other property-wait
    timeout.

    The wait is poll-assisted: besides the observer, the property is re-read
    every POLL_INTERVAL_SECS and a qualifying value accepted directly (unless
    it equals the sampled stale value, which is indistinguishable from the
    pre-change state). Observer events are the fast path; the poll rescues the
    wait when property-change delivery is lost — the external-mpv IPC pipeline
    (socket reader -> event queue -> handler) has been seen in the field to
    drop notifications, which previously turned an otherwise-fine playback
    start into a hard "no duration" timeout that killed the session.

    The poll runs on its own daemon thread: on the external backend a property
    read is a synchronous IPC command with a long internal timeout (120s in
    python-mpv-jsonipc), so polling on the waiting thread would let a wedged
    mpv stretch the caller's deadline by minutes. This way ``timeout`` stays a
    hard bound; a poller blocked on a wedged read just exits late, alone.

    ``abort`` is an Event the caller may set to give up before the timeout —
    used when mpv reports the file failed to load, where waiting out the full
    ``timeout`` for a duration that can never arrive just freezes the UI. It
    is observed by the poll thread, so it takes effect within one poll
    interval rather than instantly; that turns a 30s hang into a sub-second
    one, which is the point. An aborted wait returns False, exactly like a
    timed-out one.
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
