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

#: **The arrows are not migrated, and that is the same rule as the rest.**
#:
#: They were, and it was wrong twice over. An arrow is not one binding: it
#: seeks during playback *and* drives the OSD menu, and `input.conf` can
#: express the first and not the second. Migrating it therefore either
#: leaves the shim binding a key mpv now also binds (two seeks per press),
#: or clears the setting and takes the menu's navigation with it -- which is
#: what the first version did, permanently, for anyone who had changed a
#: seek distance.
#:
#: So this module keeps only the settings whose whole meaning mpv can carry.
#: The arrows keep their Python binding exactly when they differ from mpv
#: (see `PlayerManager._arrows_differ_from_mpv`) and are given back
#: untouched when they do not, which is the whole of #16's benefit for them
#: and needs no migration at all.
SEEKS = ()


def _cleared(value):
    """Whether the user turned this binding off.

    **[iw]**: "unless of course they set the config options to null, that
    means they were probably parking our nav interception away and eating
    the penalty." Re-binding that key during a migration would undo the
    exact thing #16 is for. ``"None"`` is included because that is what a
    cleared binding was written as before the settings became Optional.
    """
    return value in (None, "", "None")


def _changed(settings, name):
    """Whether ``name`` holds something other than the shim's own default.

    **Not ``__fields_set__``.** That says "this key was in the file", and
    ``save()`` writes all 186 of them — so after a single save every key is
    in the file and the whole config reads as deliberately chosen. The first
    version of this migrated `space cycle pause`, `f cycle fullscreen` and
    the four arrows into somebody's input.conf: every one of them mpv's own
    default, written back as an explicit binding for nothing. **[iw]**:
    "these are default bindings, we don't need to set them."

    Comparing to the class default is the honest question. It cannot tell a
    user who deliberately typed the default from one who never touched it —
    but for these settings those two want the same thing, because our
    defaults ARE mpv's for every key here.
    """
    return getattr(settings, name, None) != getattr(type(settings), name,
                                                    None)


def plan(settings):
    """``[(setting, key, command)]`` to write, for the bindings the user
    changed.

    Only ones that differ from our default, only ones that are not cleared,
    and only where mpv can express what the shim did.
    """
    out = []
    for name, command in FIXED:
        if not _changed(settings, name):
            continue
        value = getattr(settings, name, None)
        if not _cleared(value):
            out.append((name, value, command))
    return out


def render(entries):
    """The block to insert, marker included."""
    lines = [MARKER,
             "# Written once, when this client stopped binding these keys",
             "# itself. Edit or delete them freely -- nothing rewrites this."]
    lines += ["%s %s" % (key, command) for _n, key, command in entries]
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
    migrated = [name for name, _k, _c in entries]
    try:
        existing = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        if MARKER in existing:
            return []
        # Written to a sibling and renamed over, the way Settings.save does
        # and for the same reason -- this is the user's own mpv config, and
        # `open(path, "w")` truncates it *before* the write, so a full disk
        # or a kill in between leaves them with nothing rather than with
        # what they had. os.replace is atomic within a directory.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(insert_before_first_section(existing, render(entries)))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        # An unwritable config directory must not cost the user their
        # bindings: leave the settings alone and they keep working the old
        # way, through the Python binding.
        log.warning("Could not write %s; keeping the key settings.", path,
                    exc_info=True)
        return []
    # Cleared, so nothing binds them twice: the choice has moved from our
    # config into theirs, which is the whole point of the migration.
    # Cleared by NAME, not by value. Clearing "every setting whose value is
    # in the written set" nulls a setting that holds the same key string as
    # a migrated one -- `kb_pause = "right"` cleared `kb_menu_right`, whose
    # binding the plan had deliberately declined to migrate. That is the
    # "quietly dropped a feature" this module exists to avoid, arriving by
    # the back door.
    for name in migrated:
        setattr(settings, name, None)
    log.info("Migrated %d key binding(s) to %s", len(entries), path)
    return entries
