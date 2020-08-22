import logging
import re

bad_patterns = (
    (re.compile("api_key=[a-f0-9]*"), "api_key=REDACTED"),
    (
        re.compile("'X-MediaBrowser-Token': '[a-f0-9]*'"),
        "'X-MediaBrowser-Token': 'REDACTED'",
    ),
    (re.compile("'AccessToken': '[a-f0-9]*'"), "'AccessToken': 'REDACTED'"),
)

sanitize_logs = False
root_logger = logging.getLogger("")
root_logger.level = logging.DEBUG


def sanitize(message):
    if type(message) in (int, float):
        return message
    if message is not str and message is not bytes:
        message = str(message)
    for pattern, replacement in bad_patterns:
        message = pattern.sub(replacement, message)
    return message


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


def configure_log(destination):
    handler = logging.StreamHandler(destination)
    handler.setFormatter(CustomFormatter())
    root_logger.addHandler(handler)


def configure_log_file(destination: str):
    handler = logging.FileHandler(destination, mode="w")
    # Never allow logging API keys to a file.
    handler.setFormatter(CustomFormatter(True))
    root_logger.addHandler(handler)
