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

#: ``(mpv key, distance setting, exactness setting)`` -- the seek DISTANCES,
#: written onto mpv's own arrow keys.
#:
#: This was removed once, because migrating an arrow also took the OSD
#: menu's navigation with it: one setting meant both "which key drives the
#: menu" and "which key seeks", and `input.conf` can carry the second and
#: not the first. Splitting those apart (`kb_menu_*` is the menu's, and only
#: the menu's) is what makes this coherent -- **[iw]**: "people probably
#: configured arrow keys to something else so that our seek bindings
#: weren't messing with the mpv defaults." The distance is expressible, the
#: menu is not, and now they are separate questions.
#:
#: Keyed on mpv's arrows rather than on any shim setting: after the split
#: there is no shim setting for "the seek key" at all. It is mpv's key,
#: carrying the user's distance.
SEEKS = (("up", "seek_up", "seek_v_exact"),
         ("down", "seek_down", "seek_v_exact"),
         ("right", "seek_right", "seek_h_exact"),
         ("left", "seek_left", "seek_h_exact"))

#: The seek settings' old defaults, kept here because the settings
#: themselves are **gone** from ``conf.Settings``.
#:
#: They were never in the settings UI or the README -- hand-edited config
#: only -- and once #16 gave the arrows back to mpv they could not work
#: anyway: a changed distance made `_seek_is_ours` claim the key, and the
#: claim then seeks by the amount in *mpv's* binding, not by the setting.
#: So they were dead the moment the migration landed. **[iw]**: "should
#: drop the dead settings post-migration."
#:
#: Dropped from the schema, kept readable here: an upgrading user's value
#: still has to be carried into their input.conf, and by the time
#: `_migrate` runs, `parse_obj` has already discarded every key the schema
#: no longer declares. So the migration reads the **raw config dict**, not
#: the parsed Settings -- which is what a migration should do anyway, since
#: it is the only code that has to know what the config used to look like.
LEGACY_SEEK_DEFAULTS = {"seek_up": 60, "seek_down": -60,
                        "seek_right": 5, "seek_left": -5,
                        "seek_v_exact": False, "seek_h_exact": False}


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


def _legacy(raw, name):
    """A dropped seek setting's value from the raw config, or its default."""
    return (raw or {}).get(name, LEGACY_SEEK_DEFAULTS[name])


def _legacy_changed(raw, name):
    """Whether the raw config holds something other than the old default."""
    return _legacy(raw, name) != LEGACY_SEEK_DEFAULTS[name]


def plan(settings, raw=None):
    """``[(setting, key, command)]`` to write, for the bindings the user
    changed.

    Only ones that differ from our default, only ones that are not cleared,
    and only where mpv can express what the shim did.

    ``raw`` is the config file's own dict. The seek distances are read from
    it rather than from ``settings`` because they are no longer part of the
    schema -- see LEGACY_SEEK_DEFAULTS. Absent, only the `kb_*` half runs,
    which is what a caller with no file to offer should get.
    """
    out = []
    for name, command in FIXED:
        if not _changed(settings, name):
            continue
        value = getattr(settings, name, None)
        if not _cleared(value):
            out.append((name, value, command))
    # The distances are only expressible when nothing shim-only rides on
    # the seek keys. That is `use_web_seek` alone: it replaces the distance
    # with jellyfin-web's variable one, which mpv cannot express, so those
    # users keep a *claim* on whatever seeks instead
    # (PlayerManager._seek_is_ours).
    #
    # `skip_intro_on_seek` is NOT a reason to decline: `_on_seeking`
    # observes every seek and applies it, mpv's own bindings included, so
    # it works perfectly well on a migrated distance.
    if getattr(settings, "use_web_seek", False):
        return out
    for key, amount_key, exact_key in SEEKS:
        if not (_legacy_changed(raw, amount_key)
                or _legacy_changed(raw, exact_key)):
            continue
        amount = _legacy(raw, amount_key)
        if _cleared(amount):
            continue
        out.append((amount_key, key,
                    "seek %d%s" % (amount,
                                   " exact" if _legacy(raw, exact_key)
                                   else "")))
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


def migrate(settings, path, raw=None):
    """Write the plan into ``path`` and clear the settings it carried.

    Returns the entries written (empty if there was nothing to do), so the
    caller can decide whether the config needs saving.
    """
    entries = plan(settings, raw)
    if not entries:
        return []
    if getattr(settings, "mpv_ext", False) and getattr(
            settings, "mpv_ext_no_ovr", False):
        # That pair means "use my own mpv config" -- `build_mpv_options`
        # stops passing `config_dir`, so external mpv reads the user's
        # directory and never loads the file this would write. Writing it
        # anyway put the bindings somewhere inert *and* cleared the
        # settings that had been holding them, which lost the choice
        # outright.
        #
        # **[iw]**: "that config basically says 'use my own mpv config, if
        # something breaks it's my problem'... added to deal with picky
        # users who don't want to copy their config files and are willing
        # to contort their standalone mpv configs around my app."
        #
        # So: do nothing, and say what was not done. Their input.conf is
        # theirs; the block goes to the log rather than into it, which is
        # the difference between a choice the user can act on and one that
        # disappeared.
        log.info("mpv_ext_no_ovr is set, so mpv reads your own config "
                 "directory and not this one -- not writing %s. To keep "
                 "these, add them to your own input.conf:\n%s",
                 path, "\n".join("%s %s" % (key, cmd)
                                  for _n, key, cmd in entries))
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
        if not name.startswith("kb_"):
            # A seek distance has no setting to clear. They left the schema
            # in config version 4 (see LEGACY_SEEK_DEFAULTS) and `plan` read
            # this one out of the raw config, so there is no attribute here
            # -- and nothing consults one either: `_seek_is_ours` used to
            # read exactly these, which is why they had to be reset, and it
            # no longer does.
            continue
        setattr(settings, name, None)
    log.info("Migrated %d key binding(s) to %s", len(entries), path)
    return entries
