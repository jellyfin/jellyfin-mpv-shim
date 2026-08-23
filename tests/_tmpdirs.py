"""Temp directories that go away when the test process does.

A dozen call sites wanted "somewhere to put a file for this one assertion"
and reached for a bare ``tempfile.mkdtemp()``. Each left a ``tmpXXXXXXXX``
directory behind, every run, for ever -- and because the name carries no
prefix there was nothing to grep for and nothing to blame. They were found
by counting ``/tmp`` before and after a suite run, which is not a thing
anybody does by accident: the developer's ``/tmp`` had 5,660 entries and
their file manager had started crashing on it.

``atexit`` rather than ``addCleanup`` because most of the callers are helper
functions with no ``self`` to hang a cleanup on, and because the scope is
right either way: these hold nothing a later test wants, and the parallel
runner gives each module its own process.

**But atexit is not enough on its own**, and the reason is worth knowing:
``tools/run_tests_parallel.py``'s workers end with ``os._exit`` on purpose,
to skip an interpreter teardown that aborts when several libmpv instances go
down at once. Nothing registered with atexit runs there. So the runner calls
:func:`cleanup_all` explicitly just before it exits, and this module keeps
both paths -- direct ``unittest`` runs take the atexit one.

Not named ``test_*``, so discovery ignores it (same as ``_shell_harness``).
"""

import atexit
import shutil
import tempfile


#: Everything handed out, so a runner that hard-exits can still clean up.
_MADE = []


def cleanup_all():
    """Remove every directory handed out by :func:`tmpdir`.

    Idempotent, and safe to call from a process that is about to
    ``os._exit``. Called by the parallel runner, which cannot use atexit.
    """
    while _MADE:
        shutil.rmtree(_MADE.pop(), ignore_errors=True)


def tmpdir(prefix="jms-test-"):
    """A temp directory removed when this process exits.

    The prefix is deliberate and the default is deliberate too: anything
    left behind should say which suite made it, so the next person can find
    the owner from the name alone.
    """
    path = tempfile.mkdtemp(prefix=prefix)
    _MADE.append(path)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def tmpfile(name, prefix="jms-test-"):
    """A path inside a self-cleaning temp directory. The file need not
    exist; the caller is usually about to write it."""
    import os

    return os.path.join(tmpdir(prefix), name)
