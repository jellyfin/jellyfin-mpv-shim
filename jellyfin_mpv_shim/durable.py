"""Write a config file so that a crash leaves it old or new, and so that
another program holding it open on Windows does not cost the write.

Imports nothing of the app: `conf.py` uses this, and it loads first.
"""

import logging
import os
import shutil

log = logging.getLogger("durable")

#: The copy kept while a file is written in place; ours alone, never
#: `.bak`, which may be the user's (or users.json's own rolling spare). One
#: that exists marks a write that did not finish, so it is removed by every
#: write that does, and by a loader that read its file cleanly.
RECOVERY_SUFFIX = ".tmprec"


def discard_recovery(path):
    """Remove a leftover recovery copy of ``path``, if there is one."""
    try:
        os.remove(path + RECOVERY_SUFFIX)
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("Could not remove %s", path + RECOVERY_SUFFIX,
                  exc_info=True)


def fsync_dir(directory):
    """Flush the directory entry itself, so a rename survives a power cut.

    `os.replace` is atomic with respect to other *readers*; it says nothing
    about whether the new name has reached the disk. Without this, a crash
    can leave the old name, the new name, or -- on ext4 with the default
    `data=ordered` -- a correctly renamed file of zero length.

    Best effort on purpose: Windows cannot open a directory as a file and
    raises here, and a filesystem that refuses the descriptor must not stop
    the app saving. The file's own fsync is the part that matters most and it
    has already happened by the time this runs.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write(path, text, encoding, mode=None):
    if mode is None:
        fh = open(path, "w", encoding=encoding)
    else:
        # The mode applies only when os.open creates the file.
        fh = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                               mode), "w", encoding=encoding)
    with fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def replace_file(path, text, encoding="utf-8", mode=None):
    """Write ``text`` to ``path``: to a temp file, flushed, then renamed over.
    Raises on failure, having changed nothing on disk; the caller logs.

    **Windows refuses the rename while another program has ``path`` open**
    (WinError 5) -- an antivirus scan, an indexer, a backup tool, or the e2e
    suite polling users.json -- but it still lets the file be written. So
    then: copy what is there to ``path.tmprec``, write over ``path`` in
    place, and read it back. A crash part-way leaves ``path`` torn and
    ``.tmprec`` whole, which is what the loaders recover from. Once the write
    reads back true the copy is removed.

    ``mode`` creates the temp file and the ``.tmprec`` with those permission
    bits instead of the umask's (users.json holds every server token).
    """
    tmp = path + ".tmp"
    try:
        _write(tmp, text, encoding, mode)
        try:
            os.replace(tmp, path)
        except PermissionError:
            if not os.path.isfile(path):
                raise
            _write_in_place(path, text, encoding, mode)
        else:
            discard_recovery(path)
        fsync_dir(os.path.dirname(path) or ".")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _write_in_place(path, text, encoding, mode):
    bak = path + RECOVERY_SUFFIX
    if os.path.exists(bak):
        # The last good copy of a write that never finished: ``path`` may be
        # the torn half of it, and copying that over this one would leave
        # nothing to recover from if this write is cut short too.
        if mode is not None:
            os.chmod(bak, mode)
    else:
        fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o666 if mode is None else mode)
        with open(path, "rb") as src, os.fdopen(fd, "wb") as fh:
            shutil.copyfileobj(src, fh)
            fh.flush()
            os.fsync(fh.fileno())
    _write(path, text, encoding)
    with open(path, encoding=encoding) as fh:
        if fh.read() != text:
            raise OSError("%s did not read back as written; the previous "
                          "copy is %s" % (path, bak))
    log.warning("Another program had %s open, so it was written in place.",
                path)
    discard_recovery(path)
