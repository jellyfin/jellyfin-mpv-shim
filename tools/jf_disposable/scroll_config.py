"""Settings of one scroll_probe run, parsed and checked before anything is started.

Stdlib only, so scroll_manual.sh --dry-run and the unit tests can use it without the
app's dependencies. Two knobs vary between runs:

  JH_TICK_MS       the renderer's frame-cadence floor, forwarded to the app as JMS_TICK_MS
                   (honoured by the app only with JMS_FRAME_DIAG, which the probe always sets).
                   Unset: the app's own 30 ms, and the app is given no override at all.
  JH_SCROLL_DELAY  seconds between wheel notches the probe sends (default 0.03).

Both are checked here with the same rule the renderer applies (a plain decimal), so a typo
fails the run before it starts instead of being ignored by the app and mislabelling a result.
"""
import json
import os
import re
import sys

TICK_MIN_MS, TICK_MAX_MS = 5.0, 100.0          # the renderer's own accepted range
DELAY_MIN_S, DELAY_MAX_S = 0.001, 0.5
DEFAULT_DELAY_S = 0.03
_DECIMAL = re.compile(r"^\d+\.?\d*$")

# name, tick floor (ms), wheel interval (ms): the 2 x 2 of the experiment.
MATRIX = (
    ("t30_w30", 30.0, 30.0),
    ("t16.7_w30", 16.7, 30.0),
    ("t30_w8", 30.0, 8.0),
    ("t16.7_w8", 16.7, 8.0),
)


class ConfigError(ValueError):
    pass


def _decimal(text, what):
    text = "" if text is None else str(text)
    if not _DECIMAL.match(text):
        raise ConfigError("%s=%r is not a plain decimal number" % (what, text))
    return float(text)


def parse_tick_ms(text):
    """None when unset or empty; else milliseconds in [5, 100]."""
    if text is None or text == "":
        return None
    ms = _decimal(text, "JH_TICK_MS")
    if not (TICK_MIN_MS <= ms <= TICK_MAX_MS):
        raise ConfigError("JH_TICK_MS=%s outside %g..%g ms" % (text, TICK_MIN_MS, TICK_MAX_MS))
    return ms


def parse_delay_s(text):
    if text is None or text == "":
        return DEFAULT_DELAY_S
    s = _decimal(text, "JH_SCROLL_DELAY")
    if not (DELAY_MIN_S <= s <= DELAY_MAX_S):
        raise ConfigError("JH_SCROLL_DELAY=%s outside %g..%g s" % (text, DELAY_MIN_S, DELAY_MAX_S))
    return s


def parse_label(text):
    if not text:
        return None
    if not re.match(r"^[A-Za-z0-9._-]{1,48}$", text) or text.startswith("."):
        raise ConfigError("JH_RUN_LABEL=%r: use 1-48 of letters, digits, . _ -" % text)
    return text


def from_env(env=None):
    """The run's settings as a dict, or ConfigError.

    An inherited JMS_TICK_MS with no JH_TICK_MS is refused: the app would pick it up from the
    environment and the run would claim to be the default.
    """
    env = os.environ if env is None else env
    tick = parse_tick_ms(env.get("JH_TICK_MS"))
    if tick is None and env.get("JMS_TICK_MS") not in (None, ""):
        raise ConfigError("JMS_TICK_MS is set in the environment but JH_TICK_MS is not; unset it "
                          "or pass the value as JH_TICK_MS so the run records it")
    delay = parse_delay_s(env.get("JH_SCROLL_DELAY"))
    label = parse_label(env.get("JH_RUN_LABEL"))
    return {"tick_ms": tick, "wheel_delay_s": delay, "label": label}


def app_env(cfg):
    """Environment additions for the measured app: the diagnostics' override, if any."""
    return {"JMS_TICK_MS": ("%g" % cfg["tick_ms"])} if cfg["tick_ms"] is not None else {}


def main(argv):
    """`--check`: print the settings from the environment and exit 0, or say why not (64)."""
    try:
        print(json.dumps(from_env(), sort_keys=True))
    except ConfigError as e:
        print("scroll config: %s" % e, file=sys.stderr)
        return 64
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
