from threading import Event
from typing import Optional


def wait_property(
    instance,
    name: str,
    cond=lambda x: True,
    timeout: Optional[int] = None,
    skip_initial: bool = False,
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
    """
    event = Event()

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
        nonlocal skip
        if skip:
            skip = False
            # Only drop a re-delivery of the sampled stale value; a value
            # that already differs is fresh and must count.
            if value == stale_value:
                return
        if cond(value):
            event.set()

    # Discriminate on the class, not the instance: libmpv's __getattr__ turns
    # unknown instance attributes into IPC property gets, so an instance-level
    # hasattr would be both wrong and wasteful.
    use_ext_mpv = hasattr(type(instance), "bind_property_observer")

    if use_ext_mpv:
        observer_id = instance.bind_property_observer(name, handler)
    else:
        instance.observe_property(name, handler)

    # Event.wait(None) blocks indefinitely and returns True, so one wait
    # covers both the bounded and unbounded cases.
    success = event.wait(timeout=timeout)

    if use_ext_mpv:
        instance.unbind_property_observer(observer_id)
    else:
        instance.unobserve_property(name, handler)
    return success
