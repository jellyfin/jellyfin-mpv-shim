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


def sweep(bindings, wanted):
    """``[(key, semantic, cmd)]`` for every key that currently means one of
    ``wanted``, sorted for a stable section.

    ``wanted`` is a set of the semantics above.
    """
    out = []
    for key, entry in winning(bindings).items():
        semantic = classify(entry.get("cmd"))
        if semantic in wanted:
            out.append((key, semantic, (entry.get("cmd") or "").strip()))
    return sorted(out)


def is_mpv_default(bindings, key):
    """Whether ``key``'s live binding is mpv's own rather than the user's.

    The one-time migration's question, and the reason it can be answered at
    all: writing a shim binding into someone's input.conf on top of a
    binding they chose would be the same rudeness in a new place.
    """
    entry = winning(bindings).get(key)
    return bool(entry and entry.get("is_weak"))


def section_lines(claims, message):
    """The body of a ``define-section``: one line per claimed key, routing
    to ``script-message <message> <semantic> <key>``.

    The **key travels in the message** so the handler can re-issue what was
    bound to it, which is what makes a claim preserve meaning instead of
    substituting our own verb.
    """
    return "\n".join(
        "%s script-message %s %s %s" % (key, message, semantic,
                                        shlex.quote(key))
        for key, semantic, _cmd in claims
    )
