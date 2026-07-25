import logging
from collections import deque
import re

bad_patterns = (
    (re.compile("api_key=[a-f0-9]*"), "api_key=REDACTED"),
    (
        re.compile("'X-MediaBrowser-Token': '[a-f0-9]*'"),
        "'X-MediaBrowser-Token': 'REDACTED'",
    ),
    (re.compile("'AccessToken': '[a-f0-9]*'"), "'AccessToken': 'REDACTED'"),
)

# The patterns above all need the KEY next to the value, which works on a
# repr but not on a value on its own. A single mapping argument
# (``log.info("headers %s", headers)``) is exactly that case: the logging
# module unwraps it -- it becomes the %-formatting mapping rather than a
# one-tuple -- so the formatter sees the values individually and a bare
# token matches nothing. Redact those by key instead.
sensitive_keys = ("x-mediabrowser-token", "accesstoken", "api_key",
                  "password", "authorization")

sanitize_logs = False
root_logger = logging.getLogger("")
root_logger.level = logging.INFO
level_mapping = {
    "CRITICAL": 50,
    "FATAL": 50,
    "ERROR": 40,
    "WARN": 30,
    "WARNING": 30,
    "INFO": 20,
    "DEBUG": 10,
    "NOTSET": 0,
}


def _redact(text):
    for pattern, replacement in bad_patterns:
        text = pattern.sub(replacement, text)
    return text


def sanitize(message):
    """Redact secrets from one log argument, **keeping its type**.

    Type preservation is not cosmetic: the formatter maps this over
    ``record.args`` before ``msg % args`` runs, so an argument that comes
    back as a string makes every ``%d`` in the format string raise
    ``TypeError: %d format: a real number is required, not str`` — and the
    whole record is lost, replaced by a logging-internal traceback.

    Two bugs did that. ``type(message) in (int, float)`` misses ``bool``,
    which is an ``int`` subclass and the natural thing to log as ``%d``; and
    ``message is not str`` compared the *value* against the *type* object, so
    it was true for everything and stringified every argument.

    Non-strings are still redacted, and that is deliberate rather than
    accidental: one of the patterns matches a header dict's repr, so logging
    a headers dict as an argument has to be caught. But the string is only
    kept when redaction actually removed something — in which case not
    leaking the token beats keeping a ``%d`` working.
    """
    if isinstance(message, (int, float)):    # bool included, by design
        return message
    if isinstance(message, str):
        return _redact(message)
    text = str(message)
    redacted = _redact(text)
    return redacted if redacted != text else message


class CustomFormatter(logging.Formatter):
    def __init__(self, force_sanitize=False):
        self.force_sanitize = force_sanitize
        super(CustomFormatter, self).__init__(
            fmt="%(asctime)s [%(levelname)8s] %(name)s: %(message)s"
        )

    def format(self, record):
        if sanitize_logs or self.force_sanitize:
            record.msg = sanitize(record.msg)
            if type(record.args) is dict:
                sanitized = {}
                for key, value in record.args.items():
                    if str(key).lower() in sensitive_keys:
                        sanitized[key] = "REDACTED"
                    else:
                        sanitized[key] = sanitize(value)
                record.args = sanitized
            elif type(record.args) is tuple:
                record.args = tuple(sanitize(value) for value in record.args)
            else:
                return "(Log message could not be processed for sanitization.)"
        return logging.Formatter.format(self, record)


def enable_sanitization():
    global sanitize_logs
    sanitize_logs = True


def configure_log(destination, level: str = "info"):
    lvl = level_mapping[level.upper()]
    # Records below the logger's level never reach handlers, so widen the root
    # logger if a handler asks for something more permissive.
    if lvl < root_logger.level:
        root_logger.level = lvl

    handler = logging.StreamHandler(destination)
    handler.setFormatter(CustomFormatter())
    handler.setLevel(lvl)
    root_logger.addHandler(handler)


def configure_log_file(destination: str, level: str = "info"):
    lvl = level_mapping[level.upper()]
    if lvl < root_logger.level:
        root_logger.level = lvl

    handler = logging.FileHandler(destination, mode="w")
    # Never allow logging API keys to a file.
    handler.setFormatter(CustomFormatter(True))
    handler.setLevel(lvl)
    root_logger.addHandler(handler)


class RingLogHandler(logging.Handler):
    """Keeps the last N formatted log lines in memory for in-app log views.

    The browser runs in *this* process and reads the ring directly, so the
    log viewer needs no IPC and no separate copy of the buffer."""

    def __init__(self, capacity=2000):
        super().__init__()
        self.lines = deque([], capacity)

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass


ring_handler = RingLogHandler()
ring_handler.setFormatter(CustomFormatter())
root_logger.addHandler(ring_handler)


def recent_log_lines():
    return list(ring_handler.lines)
