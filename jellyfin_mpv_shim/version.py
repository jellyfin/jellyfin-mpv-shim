"""Version comparison for the update check.

The update checker used to ask ``CLIENT_VERSION != version``, which is string
inequality rather than a comparison: anyone running a build that was not
byte-identical to the newest stable tag was told an update was available,
forever. That is wrong in both directions once pre-releases exist -- someone
on ``3.0.0pre8`` was being offered ``2.10.0`` as an upgrade.

This implements the subset of PEP 440 the project's tags actually use
(``v2.10.0``, ``v3.0.0pre8``) plus the rest of the pre/post/dev grammar, so
the ordering matches what pip would do. ``packaging`` would do this for us but
is not a declared dependency, and a hundred lines here is cheaper than adding
one for a single comparison.

Deliberately *not* handled: local versions (``+local``) are parsed and then
ignored for ordering. Nothing publishes them, and PEP 440 orders them above
the same release without one, which is not a distinction the update notice
should ever act on.
"""

import re

# [N!]N(.N)*[{a|b|rc}N][.postN][.devN][+local]
_VERSION_RE = re.compile(
    r"""^\s*v?
    (?:(?P<epoch>\d+)!)?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>\d+)?)?
    (?:(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n>\d+)?)|(?:-(?P<post_i>\d+)))?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

# PEP 440's alternate spellings. "c" and "pre"/"preview" are release
# candidates; the project's own tags use the "pre" spelling.
_PRE_ALIASES = {"alpha": "a", "beta": "b", "c": "rc", "pre": "rc",
                "preview": "rc"}
_PRE_ORDER = {"a": 0, "b": 1, "rc": 2}

# Sorts below any real pre-release, for a dev release of the base version.
_PRE_DEV = (-1, 0)
# Sorts above them: no pre-release segment means final (or post-final).
_PRE_FINAL = (3, 0)


def parse(text):
    """Return an orderable key for ``text``, or None if it isn't a version.

    Comparing two keys with ``<`` gives PEP 440 ordering. Callers must treat
    None as "unknown" rather than as an extreme -- an unparseable version is
    not an old one.
    """
    if not text:
        return None
    match = _VERSION_RE.match(text)
    if match is None:
        return None
    parts = match.groupdict()

    epoch = int(parts["epoch"] or 0)

    # Trailing zeros are dropped so 1.0 and 1.0.0 compare equal, as PEP 440
    # requires. Comparison is then plain tuple order.
    release = [int(n) for n in parts["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()

    pre = None
    if parts["pre_l"]:
        letter = parts["pre_l"].lower()
        letter = _PRE_ALIASES.get(letter, letter)
        pre = (_PRE_ORDER[letter], int(parts["pre_n"] or 0))

    post = None
    if parts["post_l"]:
        post = int(parts["post_n"] or 0)
    elif parts["post_i"]:
        post = int(parts["post_i"])

    dev = None
    if parts["dev_l"]:
        dev = int(parts["dev_n"] or 0)

    if pre is None:
        # A dev release with no pre-release segment precedes everything at
        # this release number; anything else is final or later.
        pre_key = _PRE_DEV if (dev is not None and post is None) else _PRE_FINAL
    else:
        pre_key = pre

    return (
        epoch,
        tuple(release),
        pre_key,
        -1 if post is None else post,
        # No dev segment means the finished build, which comes after its own
        # dev builds -- hence a value above every real dev number.
        float("inf") if dev is None else dev,
    )


def is_newer(candidate, current) -> bool:
    """True if ``candidate`` is a genuine upgrade from ``current``.

    Falls back to string inequality when either side is unparseable: the
    failure this replaces was crying wolf, but silently never notifying again
    because a tag changed shape would be worse.
    """
    a, b = parse(candidate), parse(current)
    if a is None or b is None:
        return candidate != current
    return a > b
