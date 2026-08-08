"""The reader's exception base, in a module every layer can import.

One class, its own file, because of a cycle: ``archive`` is where an epub
becomes readable and is where `EpubError` naturally lived, but ``archive``
*imports* ``xmlish`` to read the package document — so the markup reader
could not raise the same error without importing its own caller.

That matters more than tidiness. Half a dozen places catch `EpubError` to
mean "this document is unreadable, skip it and carry on": a missing spine
item, an entry that blew its size cap, a stylesheet that will not load. A
parse that runs out of time is exactly that answer, so it has to be
catchable by exactly those handlers rather than by a second `except` that
somebody has to remember to add.
"""


class EpubError(Exception):
    """This file, or something in it, cannot be read — with a reason."""
