"""The reader's exception base, in a module every layer can import.

One class, its own file, because of a cycle: `EpubError` belongs in
``archive``, but ``archive`` *imports* ``xmlish`` to read the package
document — so the markup reader could not raise the same error without
importing its own caller. Do not merge this back into ``archive``.

Half a dozen places catch `EpubError` to mean "this document is unreadable,
skip it and carry on", so every layer's failures have to land on it rather
than on a second `except` somebody has to remember to add.
"""



class EpubError(Exception):
    """This file, or something in it, cannot be read — with a reason."""
