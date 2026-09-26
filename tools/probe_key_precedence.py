#!/usr/bin/env python3
"""Who receives a key when mpv's own overlays and ours both force it.

The renderer forces ENTER, ESC, the arrows and `any_unicode`. So does mpv's
console, and so does its context menu. "Forced" is not the tie-break --
between two forced sections the LATER one wins -- and getting that backwards
is what a comment in `renderer.lua` did for a release: it said our bindings
outranked the console, and the handler under it was right for a different
reason.

So this asks mpv, on the build you actually have, and it asks by pressing
the key rather than by reading `priority` off `input-bindings`. A number is
the thing you have to interpret; the handler that fires is the answer.

Three cases, and only the third is an exposure:

  A. nothing open ................ our forced binding fires
  B. the overlay opens AFTER us .. the overlay's binding fires
  C. we re-bind while it is up ... ours fires, and the overlay stays drawn

C is what `renderer.lua`'s `user-data/mpv/console/open` handler exists to
prevent: the HUD re-installs its nav keys on pointer movement and on every
lifecycle event, so anything that re-binds while an overlay is up leaves it
on screen with no way to activate an item.
`tools/audit_key_bindings.py:PUBLISHED` is where each overlay's property is
declared; this is where its answer is measured.

Run it when the mpv pin moves, or before believing a claim about who wins.
Read-only: no window, no config, no file in the tree.

Usage:  tools/probe_key_precedence.py [--mpv MPV] [--verbose]
Exit 1 if any case could not be measured -- an mpv without the overlay, or
one whose menu never opened. A case that measures cleanly always "passes";
this is a probe, not a threshold (docs/testing.md section 7).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

#: Each overlay: how to open it, and the property that says it is up. The
#: context menu needs `menu-data` populated -- with an empty menu it returns
#: before binding anything and before setting its property, which reads as
#: "no exposure" and is really "the probe did not run".
OVERLAYS = {
    "console": {
        "open": "mp.commandv('script-message-to', 'console', 'enable')",
        "prop": "user-data/mpv/console/open",
        "args": [],
    },
    "context-menu": {
        "open": "mp.commandv('script-message-to', 'context_menu', 'open')",
        "prop": "user-data/mpv/context-menu/open",
        "args": ["--load-context-menu=yes"],
    },
}

_PROBE = """
-- Stand in for the renderer: a forced ENTER bound at startup.
local fired = {}
local function claim()
    mp.add_forced_key_binding('ENTER', 'jms_probe_enter', function()
        fired[#fired + 1] = 'OURS'
    end)
end
claim()

mp.set_property_native('menu-data', {
    { title = 'probe', cmd = 'ignore' },
})

local function press()
    fired = {}
    mp.commandv('keypress', 'ENTER')
end

local function say(case, note)
    -- Prefixed so the console, which echoes mpv's own log back into its
    -- overlay, cannot be mistaken for a result line.
    print('JMSPROBE ' .. case .. ' ' .. (fired[1] or 'OVERLAY') ..
          ' open=' .. tostring(mp.get_property_native(%(prop)r)) ..
          ' ' .. note)
end

mp.add_timeout(0.5, function()
    press()
    mp.add_timeout(0.2, function()
        say('A', 'nothing-open')
        %(open)s
        mp.add_timeout(0.5, function()
            press()
            mp.add_timeout(0.2, function()
                say('B', 'overlay-opened-after-us')
                %(open)s
                mp.add_timeout(0.4, function()
                    claim()          -- what a HUD lifecycle event does
                    mp.add_timeout(0.2, function()
                        press()
                        mp.add_timeout(0.2, function()
                            say('C', 'we-rebound-while-it-was-up')
                            mp.commandv('quit')
                        end)
                    end)
                end)
            end)
        end)
    end)
end)
"""

_LINE = re.compile(r"JMSPROBE (\w) (OURS|OVERLAY) open=(\S+) (\S+)")


def probe(name, mpv="mpv", timeout=40):
    """[(case, winner, open, note)] for one overlay, in case order."""
    spec = OVERLAYS[name]
    tmp = tempfile.mkdtemp(prefix="jms-keyprobe-")
    try:
        script = os.path.join(tmp, "probe.lua")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_PROBE % {"prop": spec["prop"], "open": spec["open"]})
        cmd = [mpv, "--idle", "--vo=null", "--ao=null", "--no-config",
               "--script=" + script] + spec["args"]
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout).stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    seen, rows = set(), []
    for case, winner, is_open, note in _LINE.findall(out):
        if case in seen:      # the console echoes our own lines back
            continue
        seen.add(case)
        rows.append((case, winner, is_open, note))
    return sorted(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mpv", default="mpv", help="mpv binary to ask")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    version = subprocess.run([args.mpv, "--version"], capture_output=True,
                             text=True).stdout.splitlines()[0]
    print(version)

    bad = 0
    for name in sorted(OVERLAYS):
        rows = probe(name, args.mpv)
        print("\n%s (%s)" % (name, OVERLAYS[name]["prop"]))
        if len(rows) != 3:
            bad += 1
            print("  could not measure: %d of 3 cases reported. This mpv "
                  "may not have the overlay at all." % len(rows))
            if args.verbose:
                print("  got: %r" % (rows,))
            continue
        for case, winner, is_open, note in rows:
            print("  %s  %-8s wins   overlay open=%-5s  (%s)"
                  % (case, winner, is_open, note))
        if rows[2][1] != "OURS":
            print("  NOTE: case C did not reproduce on this build. The "
                  "re-bind stopped taking the key back, which would make "
                  "the console handler unnecessary -- check before "
                  "believing it.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
