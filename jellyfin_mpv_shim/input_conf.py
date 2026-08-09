"""Writing the user's own key choices into mpv's ``input.conf`` (#16).

The shim used to express "which key pauses" as a config setting of its own
and then bind that key in Python. #16 gives those keys back to mpv — but a
user who *changed* one made a real choice, and dropping the binding without
carrying it across would silently undo it.

So a one-time migration: the settings that mpv can express become real mpv
bindings, in the shim's own config directory, and the settings are cleared
so nothing binds them twice.

**Two things are load-bearing.**

*Where it writes.* mpv's ``input.conf`` has ``[section]`` headers and
**everything after one belongs to it until the next**, so appending to the
end of a file that has any section puts the new bindings *inside* it, where
they apply conditionally or never. The file would look right and the keys
would silently not work. It writes before the first ``[``, and says so in
the file. ``mpv_options.hwdec_pinned_by_config`` reads the same rule from
the other side.

*What it declines to write.* Only a setting whose meaning mpv can express.
``use_web_seek`` (jellyfin-web's variable seek) and ``skip_intro_on_seek``
have no mpv equivalent at all, so where either is on the arrows keep their
Python binding and are not migrated — a migration that quietly dropped a
feature would be worse than none.
"""

import logging
import os

log = logging.getLogger("input_conf")

#: The marker line. Present means the migration has run against this file,
#: which is the check that keeps it from writing twice even if the config
#: version is lost.
MARKER = "# --- migrated from jellyfin-mpv-shim key settings ---"

#: ``setting -> the mpv command it means``. Only these: the rest of the
#: ``kb_*`` settings name a *shim* action (mark watched, next in the
#: Jellyfin queue, open our menu) that mpv has no opinion about and cannot
#: be handed anything.
FIXED = (("kb_pause", "cycle pause"),
         ("kb_fullscreen", "cycle fullscreen"))

#: ``setting -> (seek setting, exactness setting)``. The amount is the
#: user's, so `kb_menu_up` with `seek_up = 30` migrates to `seek 30`.
SEEKS = (("kb_menu_up", "seek_up", "seek_v_exact"),
         ("kb_menu_down", "seek_down", "seek_v_exact"),
         ("kb_menu_right", "seek_right", "seek_h_exact"),
         ("kb_menu_left", "seek_left", "seek_h_exact"))


def _cleared(value):
    """Whether the user turned this binding off.

    **[iw]**: "unless of course they set the config options to null, that
    means they were probably parking our nav interception away and eating
    the penalty." Re-binding that key during a migration would undo the
    exact thing #16 is for. ``"None"`` is included because that is what a
    cleared binding was written as before the settings became Optional.
    """
    return value in (None, "", "None")


def plan(settings):
    """``[(key, command)]`` to write, for the bindings the user changed.

    Only settings they actually set (``__fields_set__``), only ones that
    are not cleared, and only where mpv can express what the shim did.
    """
    touched = getattr(settings, "__fields_set__", ())
    out = []
    for name, command in FIXED:
        if name not in touched:
            continue
        value = getattr(settings, name, None)
        if not _cleared(value):
            out.append((value, command))
    # The arrows are only expressible when nothing shim-specific rides on
    # them. Asked once for the group, not per key: a user with web seek on
    # has it on for all four.
    if not (getattr(settings, "use_web_seek", False)
            or getattr(settings, "skip_intro_on_seek", False)):
        for name, amount_key, exact_key in SEEKS:
            if name not in touched:
                continue
            value = getattr(settings, name, None)
            if _cleared(value):
                continue
            amount = getattr(settings, amount_key, 0)
            command = "seek %d%s" % (
                amount, " exact" if getattr(settings, exact_key, False)
                else "")
            out.append((value, command))
    return out


def render(entries):
    """The block to insert, marker included."""
    lines = [MARKER,
             "# Written once, when this client stopped binding these keys",
             "# itself. Edit or delete them freely -- nothing rewrites this."]
    lines += ["%s %s" % (key, command) for key, command in entries]
    return "\n".join(lines) + "\n\n"


def insert_before_first_section(existing, block):
    """``existing`` with ``block`` inserted **above the first ``[``**.

    Not appended. Everything after a section header belongs to that section
    until the next one, so appending to a file that has any section puts
    these bindings inside it -- written, looking right, and never firing.
    """
    lines = existing.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return "".join(lines[:i]) + block + "".join(lines[i:])
    if existing and not existing.endswith("\n"):
        existing += "\n"
    return existing + block


def migrate(settings, path):
    """Write the plan into ``path`` and clear the settings it carried.

    Returns the entries written (empty if there was nothing to do), so the
    caller can decide whether the config needs saving.
    """
    entries = plan(settings)
    if not entries:
        return []
    try:
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        if MARKER in existing:
            return []
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(insert_before_first_section(existing, render(entries)))
    except OSError:
        # An unwritable config directory must not cost the user their
        # bindings: leave the settings alone and they keep working the old
        # way, through the Python binding.
        log.warning("Could not write %s; keeping the key settings.", path,
                    exc_info=True)
        return []
    # Cleared, so nothing binds them twice: the choice has moved from our
    # config into theirs, which is the whole point of the migration.
    for name, _c in FIXED:
        if name in getattr(settings, "__fields_set__", ()):
            setattr(settings, name, None)
    for name, _a, _e in SEEKS:
        if any(key == getattr(settings, name, None) for key, _cmd in entries):
            setattr(settings, name, None)
    log.info("Migrated %d key binding(s) to %s", len(entries), path)
    return entries
