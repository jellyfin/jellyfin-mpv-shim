"""Which keys currently *mean* something we need to intercept.

#16. The shim used to bind `space`, `f` and the four arrows unconditionally,
which meant swallowing mpv's own defaults — and, worse, swallowing whatever
the user had put on those keys in their own `input.conf`. That is the
complaint behind PR #547, and the answer is not a config parser.

**Ask mpv, do not read the config.** The ``input-bindings`` property is the
*resolved* set: mpv has already applied sections, profiles, priorities and
`ignore`, so there is no precedence model of ours to drift from theirs. It
costs one property read.

What that buys, and why this is better than hard-coding keys:

* it **follows a remapped key**. Somebody who moved pause to `p` gets
  SyncPlay-aware pause on `p`, which a fixed ``kb_pause`` never gave them;
* it **preserves meaning**, because a claim re-issues the command that was
  already bound rather than substituting one of ours. We intercept only
  where we genuinely need to, and even there the key still does what their
  config says;
* it distinguishes **mpv's default from the user's choice** — that is
  ``is_weak``, which mpv sets on the builtin bindings and not on anything
  from an ``input.conf``. The one-time migration needs exactly that.

Pure logic, no mpv: the caller reads the property and installs the section.
"""

import logging
import shlex

log = logging.getLogger("keysweep")

#: The three things we ever need to know about. Deliberately not a general
#: mechanism -- each of these is claimed by a named feature with a reason.
PAUSE = "pause"
SEEK = "seek"
FULLSCREEN = "fullscreen"

#: ``property -> semantic`` for the commands that toggle or set one.
_PROPERTY_SEMANTIC = {"pause": PAUSE, "fullscreen": FULLSCREEN}

#: Commands that manipulate a property by name. ``no-osd`` and friends are
#: prefixes rather than commands, and are stripped before this is consulted.
_PROPERTY_VERBS = ("cycle", "set", "cycle-values", "add", "multiply",
                   "toggle")

#: Prefixes mpv allows in front of a command. Stripped, not matched: a
#: binding written `no-osd cycle pause` means the same thing to us.
_PREFIXES = ("osd-auto", "no-osd", "osd-bar", "osd-msg", "osd-msg-bar",
             "raw", "expand-properties", "repeatable", "nonrepeatable",
             "async", "sync")


def _tokens(cmd):
    """``cmd`` split into words with the prefixes dropped, or None if it
    cannot be read. shlex, because a command can carry quoted arguments and
    a naive split would classify `cycle-values sub-ass-override "force"`
    off its quotes."""
    try:
        parts = shlex.split(str(cmd or ""))
    except ValueError:
        # An unbalanced quote in somebody's input.conf. Unreadable is not
        # the same as uninteresting, but it is the same as "leave it alone".
        return None
    while parts and parts[0] in _PREFIXES:
        parts = parts[1:]
    return parts or None


def classify(cmd):
    """The semantic ``cmd`` has, or None.

    **Under-claiming is the safe direction**, so anything not recognised
    exactly is left alone: a key we fail to claim costs a SyncPlay report
    or a remembered fullscreen, while a key we claim wrongly is one we have
    stolen from the user for no reason -- which is the whole thing #16 is
    removing.
    """
    parts = _tokens(cmd)
    if not parts:
        return None
    verb = parts[0]
    if verb == "seek":
        return SEEK
    if verb in _PROPERTY_VERBS and len(parts) >= 2:
        return _PROPERTY_SEMANTIC.get(parts[1])
    return None


#: mpv's seek flags that mean "not relative to where we are". A claim
#: re-issues the user's intent through the shim's own seek, which is
#: relative -- so an absolute one is left alone rather than mistranslated.
_ABSOLUTE_SEEK = ("absolute", "absolute-percent", "absolute+keyframes",
                  "relative-percent")


def action(cmd):
    """``(semantic, arg)`` for a command a claim can carry out, or None.

    The *intent*, not just the category, because a claim substitutes the
    shim's own SyncPlay-aware operation for the binding and the two have to
    mean the same thing. ``PLAYONLY`` is bound to ``set pause no``:
    answering it with a toggle would pause a playing file from the key
    whose entire job is to not do that.

    ``arg`` is ``None`` for a toggle, a bool for a set, and
    ``(seconds, exact)`` for a seek.
    """
    parts = _tokens(cmd)
    if not parts:
        return None
    verb = parts[0]
    if verb == "seek":
        if len(parts) < 2:
            return None
        flags = parts[2:]
        if any(f in _ABSOLUTE_SEEK for f in flags):
            # Not translatable into the shim's relative seek. Left alone.
            return None
        try:
            amount = float(parts[1])
        except ValueError:
            return None
        return SEEK, (amount, "exact" in flags)
    if verb in ("cycle", "toggle") and len(parts) >= 2:
        semantic = _PROPERTY_SEMANTIC.get(parts[1])
        return (semantic, None) if semantic else None
    if verb == "set" and len(parts) >= 3:
        semantic = _PROPERTY_SEMANTIC.get(parts[1])
        if not semantic:
            return None
        value = parts[2].lower()
        if value in ("yes", "true", "1", "on"):
            return semantic, True
        if value in ("no", "false", "0", "off"):
            return semantic, False
        return None
    return None


def winning(bindings):
    """``{key: entry}`` -- the binding that actually fires for each key.

    ``input-bindings`` lists every binding including shadowed ones, so a key
    the user rebound appears twice. mpv prefers a **non-weak** binding (one
    from an input.conf or a script) over a weak one (its own builtin), then
    a higher priority. Ties go to the later entry, which is mpv's own
    last-defined-wins.
    """
    best = {}
    for entry in bindings or []:
        key = entry.get("key")
        if not key:
            continue
        current = best.get(key)
        if current is None or _rank(entry) >= _rank(current):
            best[key] = entry
    return best


def _rank(entry):
    return (0 if entry.get("is_weak") else 1, entry.get("priority") or 0)


def _is_pointer(key):
    """Whether ``key`` is mouse or wheel input.

    **Excluded from every claim.** The pointer belongs to the renderer:
    `mpvtk_mouse` owns the buttons while the HUD or the library is up, and
    #1's whole subject was getting that ownership right -- a second
    claimant on MBTN_* would be fighting it, and mpv's own
    `MBTN_LEFT_DBL cycle fullscreen` is exactly the binding #1 arranged to
    fall *through* to.

    It does leave a gap for the pointer's own meanings -- see
    :func:`pointer_keys`, which suppresses the ones a SyncPlay group cannot
    tolerate rather than claiming them.
    """
    return key.startswith("MBTN_") or key.startswith("WHEEL_")


def sweep(bindings, wanted):
    """``[(key, semantic, cmd)]`` for every key that currently means one of
    ``wanted``, sorted for a stable section.

    ``wanted`` is a set of the semantics above.
    """
    out = []
    for key, entry in winning(bindings).items():
        if _is_pointer(key):
            continue
        parsed = action(entry.get("cmd"))
        if parsed is not None and parsed[0] in wanted:
            out.append((key, parsed[0], parsed[1]))
    return sorted(out, key=lambda c: (c[1], c[0]))


def is_mpv_default(bindings, key):
    """Whether ``key``'s live binding is mpv's own rather than the user's.

    The one-time migration's question, and the reason it can be answered at
    all: writing a shim binding into someone's input.conf on top of a
    binding they chose would be the same rudeness in a new place.
    """
    entry = winning(bindings).get(key)
    return bool(entry and entry.get("is_weak"))


def pointer_keys(bindings, wanted):
    """Pointer keys that currently mean one of ``wanted``.

    These are excluded from a *claim* (see ``_is_pointer``) because the
    renderer owns the pointer -- but a SyncPlay group still cannot have
    them. mpv's own ``WHEEL_LEFT``/``WHEEL_RIGHT`` are ``seek -10``/``seek
    10``, and a seek nobody reports is a desync the group then corrects.

    **[iw]**: "should just disable wheel seek during syncplay." Which is the
    right trade rather than the lazy one: routing them would mean a message
    per notch for a gesture that delivers dozens, to reach an operation the
    group is going to refuse anyway.
    """
    out = []
    for key, entry in winning(bindings).items():
        if not _is_pointer(key):
            continue
        parsed = action(entry.get("cmd"))
        if parsed is not None and parsed[0] in wanted:
            out.append(key)
    return sorted(out)


def section_lines(claims, message, suppress=()):
    """The body of a ``define-section``: one line per claimed key, routing
    to ``script-message <message> <semantic> <key>``.

    The **key travels in the message** so the handler can re-issue what was
    bound to it, which is what makes a claim preserve meaning instead of
    substituting our own verb.
    """
    lines = ["%s script-message %s %s %s" % (key, message, semantic,
                                             shlex.quote(key))
             for key, semantic, _arg in claims]
    # `ignore` rather than a message: these are suppressed for the duration
    # of the claim, not handled. mpv drops the key and nothing else in the
    # chain sees it.
    lines += ["%s ignore" % key for key in suppress]
    return "\n".join(lines)
