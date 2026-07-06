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
    sample is already the fresh value and looks stale, so we wait for a further
    change that never arrives. Fresh and stale are indistinguishable here (both
    satisfy ``cond``), so we prefer correctness for the common (stale) case and
    let the caller's ``timeout`` bound that rare case -- it then degrades
    exactly like any other property-wait timeout.
    """
    success = True
    event = Event()

    # Sample before registering the observer so the handler (which may fire on
    # the mpv event thread the moment we register) never races this write.
    skip = False
    if skip_initial:
        try:
            skip = cond(getattr(instance, name))
        except Exception:
            skip = False

    def handler(_name, value):
        nonlocal skip
        if skip:
            skip = False
            return
        if cond(value):
            event.set()

    # Discriminate on the class, not the instance: libmpv's __getattr__ turns
    # unknown instance attributes into IPC property gets, so an instance-level
    # hasattr would be both wrong and wasteful.
    use_ext_mpv = hasattr(type(instance), "bind_property_observer")

    if use_ext_mpv:
        observer_id = instance.bind_property_observer(name, handler)
        if timeout:
            success = event.wait(timeout=timeout)
        else:
            event.wait()
        instance.unbind_property_observer(observer_id)
    else:
        instance.observe_property(name, handler)
        if timeout:
            success = event.wait(timeout=timeout)
        else:
            event.wait()
        instance.unobserve_property(name, handler)
    return success
