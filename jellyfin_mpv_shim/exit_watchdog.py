"""Make quitting actually quit, and say what stopped it if it didn't.

Two things can leave the app running with no window and no way to quit it,
and CPython waits for both: a shutdown step that never returns (which the
deadline in :func:`arm` catches) and a thread that outlives its ``stop()``
(which :func:`finish` reports). Both report stacks rather than names, because
the frame a thread is parked in is what identifies the call needing bounding.

The forced exit is safe because it runs only *after* the orderly shutdown;
what it skips is the interpreter's own wait. Everything here also tolerates
having nowhere to write — a Windows GUI build (pythonw, the frozen installer
build) has no console, so both streams are ``None`` — hence :func:`_write` /
:func:`_flush` / :func:`_dump_to` and never a stream touched directly.

The derivation, including the two-minute wedge this was built for, is in
docs/architecture.md section 4.
"""

import faulthandler
import logging
import os
import signal
import sys
import threading
import time
import traceback

log = logging.getLogger("exit_watchdog")

# Total time stragglers get to finish once the shutdown is otherwise done.
# A budget for all of them together, not each: most are a socket read about
# to time out, and waiting lets them end normally and keeps the log quiet.
GRACE_SECONDS = 3.0

# How long the whole orderly shutdown gets before we call one of its steps
# wedged. Generous, because a step legitimately waiting out a socket
# timeout must not be cut off and blamed for it.
SHUTDOWN_DEADLINE = 20.0

# One shutdown per process, so these are set once and never reset.
_watchdog = None
_disarm = threading.Event()


def _flush(stream):
    """Flush a stream that may not exist; a Windows GUI build has no console,
    so both of them are ``None`` and flushing raises AttributeError."""
    if stream is None:
        return
    try:
        stream.flush()
    except Exception:
        pass


def _write(stream, text):
    """Write to a stream that may not exist or may be closed."""
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except Exception:
        pass


def _dump_to(stream):
    """faulthandler-dump every thread to ``stream``, if it can take one.

    faulthandler writes through a file descriptor, so a stream is only
    usable if ``fileno()`` works — which rules out ``None``, and also the
    wrappers a frozen GUI build can leave in place of a real console.
    """
    if stream is None:
        return False
    try:
        if stream.fileno() < 0:
            return False
    except Exception:
        return False
    try:
        faulthandler.dump_traceback(file=stream, all_threads=True)
        _flush(stream)
        return True
    except Exception:
        return False


def enable_manual_dumps():
    """``kill -USR1 <pid>`` dumps every thread's stack to stderr.

    A hang is only diagnosable while it is hanging, at which point it is
    too late to add instrumentation. No-op where the signal does not
    exist (Windows).
    """
    if not hasattr(signal, "SIGUSR1"):
        return
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)
    except Exception:
        log.debug("could not register SIGUSR1 stack dumps", exc_info=True)


def _dump_all_threads(why):
    """Every thread's Python stack, to stderr and to the log file.

    ``faulthandler`` rather than the walk in :func:`_describe`: this runs
    while the rest of the process is blocked, and it covers the main
    thread — which, when a step is wedged, is the one parked in the call
    that names it.
    """
    banner = "\n===== %s =====\n" % why
    _write(sys.stderr, banner)
    dumped = _dump_to(sys.stderr)
    # The log file is what gets sent back in a bug report, and faulthandler
    # writes to a file descriptor rather than through logging. It is also
    # the ONLY destination on a Windows GUI build, where there is no
    # console and sys.stderr is None.
    for handler in logging.getLogger().handlers:
        stream = getattr(handler, "stream", None)
        if stream is None or stream in (sys.stderr, sys.stdout):
            continue
        _write(stream, banner)
        # Best effort per handler: one we cannot write to must not stop us
        # reporting to the ones we can.
        dumped = _dump_to(stream) or dumped
    if not dumped:
        # Nothing could take a faulthandler dump (no console, no file log).
        # The compact walk still goes through logging, so the reason for
        # the exit is not lost entirely.
        log.warning("%s\n%s", why, _describe(_survivors()))


def arm(deadline=None):
    """Start the shutdown deadline.

    Call at the *start* of the shutdown sequence, not the end: the failure
    this guards against is a step that never returns, and anything placed
    after such a step is unreachable by definition.
    """
    global _watchdog
    if _watchdog is not None:
        return
    seconds = SHUTDOWN_DEADLINE if deadline is None else deadline

    def watch():
        if _disarm.wait(seconds):
            return                      # shutdown finished; nothing to do
        _dump_all_threads(
            "shutdown did not finish within %.0fs - all thread stacks "
            "follow; the main thread shows which step is wedged" % seconds)
        logging.shutdown()
        _flush(sys.stderr)
        os._exit(1)

    _watchdog = threading.Thread(target=watch, name="exit-watchdog",
                                 daemon=True)
    _watchdog.start()


def _survivors():
    """Non-daemon threads still running, excluding the main thread (which
    is the caller). These are exactly the threads that would keep the
    interpreter alive after ``main`` returns; a daemon straggler costs
    nothing at exit and would only be noise here."""
    me = threading.current_thread()
    return [t for t in threading.enumerate()
            if t is not me and t is not threading.main_thread()
            and t.is_alive() and not t.daemon]


def _describe(threads):
    """Name each straggler and where it is parked, a few frames each.

    Deliberately briefer than :func:`_dump_all_threads`: a leaked thread
    at the end of an otherwise clean shutdown warrants a line in the log,
    not a full-process dump.
    """
    frames = sys._current_frames()
    lines = []
    for t in threads:
        lines.append("  %s (id=%s)" % (t.name, t.ident))
        frame = frames.get(t.ident)
        if frame is None:
            # _survivors() and _current_frames() are separate snapshots, so
            # a thread can finish in between. Not worth reporting as an
            # error; it simply stopped on its own.
            lines.append("      <no stack available>")
            continue
        for entry in traceback.format_stack(frame)[-4:]:
            for line in entry.rstrip().splitlines():
                lines.append("      " + line.strip())
    return "\n".join(lines)


def _await_stragglers(threads):
    """Give the whole set GRACE_SECONDS between them to end on their own."""
    deadline = time.monotonic() + GRACE_SECONDS
    for t in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(timeout=remaining)


def finish(status=0):
    """Wait briefly for stragglers, report any, then end the process.

    Call as the last statement of ``main``, after the orderly shutdown.
    """
    _disarm.set()               # the deadline no longer applies
    stuck = _survivors()
    if stuck:
        _await_stragglers(stuck)
        stuck = _survivors()

    if stuck:
        log.warning(
            "%d thread(s) did not stop during shutdown; exiting anyway. "
            "This is a leak — the stacks below show what they are parked "
            "on:\n%s", len(stuck), _describe(stuck))

    # os._exit skips atexit handlers, which is the point: one of them is
    # concurrent.futures' join of every pool worker. It also skips buffer
    # flushing, which is NOT something we can skip — the warning above is
    # the entire value of this function.
    logging.shutdown()
    _flush(sys.stdout)
    _flush(sys.stderr)
    os._exit(status)
