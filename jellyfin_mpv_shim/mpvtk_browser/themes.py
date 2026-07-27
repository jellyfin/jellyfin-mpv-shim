"""Selectable UI themes for the mpvtk browser: the schema, and the loader.

A theme is a plain data bundle — the colour palette (see ``theme.py``), the mpv
browse ``background-color``, a glow toggle, and a handful of size and layout
choices. ``theme.apply(name)`` makes one active at startup; ``player`` /
``strips`` / ``tile_renderer`` / the renderer read the rest.

Themes are **JSON files**, resolved the way shader packs are: the ones shipped
in the package are the built-ins, and a file of the same name under the user's
config directory (``<config>/themes/<id>.json``) shadows the built-in entirely.
So a user can add themes, and can retheme a shipped one, without touching the
install.

:data:`DEFAULT` is the exception, and deliberately so. It lives here in Python
because it is three things at once:

* the **fallback** — whatever else fails to load, this always resolves;
* the **schema** — a theme file may only set keys that appear here, and its
  values are coerced to the types found here;
* the **base** — every theme is merged *over* this, so a theme file only states
  what it changes. ``{"name": "Red", "palette": {"ACCENT": "cc2222"}}`` is a
  complete, valid theme.

That last property is what makes the merge safe. There is no such thing as a
half-applied theme: an absent key is the default's value, never the previously
active theme's.
"""

import json
import logging
import os

log = logging.getLogger("mpvtk_browser.themes")

#: Directory name for both the shipped themes and the user's own.
THEME_DIR = "themes"

# --- the stock Jellyfin-ish dark look (unchanged upstream default) ----------
# This dict is also the schema: a theme file may set these keys and no others,
# and each value is coerced to the type here.
DEFAULT = {
    "name": "Default",
    "palette": {
        "WINDOW_BG": "15171a", "CARD_BG": "1e2024", "PANEL_BG": "26292f",
        "PLACEHOLDER_BG": "2a2d33", "BUTTON_BG": "2e3138",
        "BUTTON_ACTIVE": "3a3e46", "ENTRY_BG": "2a2d33", "BORDER": "3a3d42",
        "TEXT_FG": "e8e8e8", "SUBTLE_FG": "9aa0a6",
        # There is exactly ONE accent. Anything that reads as "the app's
        # colour" — primary buttons, selection, hover rings, progress, active
        # tabs — uses this family and nothing else; a second unrelated hue
        # makes the UI look assembled from parts. ACCENT_HOVER is the same
        # colour lightened, ACCENT_SOFT the same darkened for fills that sit
        # behind text. ACCENT_FG is what goes ON an accent fill — dark-on-
        # accent reads as disabled, so it is white in both shipped themes.
        "ACCENT": "00a4dc", "ACCENT_HOVER": "1cb6e8", "ACCENT_SOFT": "0a3a4d",
        "ACCENT_FG": "ffffff",
        "FAV_RED": "e0264b",
        "OK_GREEN": "7bd88f",     # "Connected" / "active" badges
        "WARN_AMBER": "e5c07b",
        # Semantic extras used by baked strip decorations.
        "PROGRESS_TRACK": "000000",   # ~78% alpha behind the resume bar
        "WATCHED_GREEN": "28a046",
    },
    "browse_bg": "#141414",
    "glow": False,       # blurred accent halo behind titles + on card selection
    "rounded": False,    # rounded cover cards + cover-crop (False = stock square/letterbox)
    "accent_buttons": False,  # accent-bordered top-bar buttons
    # Where a carousel's page buttons live. "header" is jellyfin-web's design
    # and the default: a flat pair in the section heading, clear of the
    # artwork, with a disabled state. "overlay" floats round translucent
    # bitmaps over the strip's edges — see tile_renderer.hscroll_row.
    "arrow_mode": "header",
    # Fill of the round carousel page arrow (see tile_renderer._arrow_bitmap).
    # Neutral dark grey rather than the palette's BUTTON_BG: the arrow floats
    # ON artwork, not in chrome, so it wants the same "player overlay
    # furniture" treatment as the HUD's Skip Intro chip (hud._SKIP_BG /
    # _SKIP_ALPHA) — a themed fill reads as a coloured chip stuck to a poster.
    "arrow_bg": "202020",
    "arrow_alpha": 180,
    "poster_scale": 1.0,  # tile-geometry multiplier
    "heading_size": 24,   # carousel section-title font size
    "tile_landscape": (240, 135),  # (w, h) of the library/landscape tile
    "tile_title_size": None,  # tile caption font; None = stock (scales w/ cover)
    "tile_sub_size": None,
}

#: Keys whose value is a bare ``"rrggbb"`` outside the palette.
_COLOUR_KEYS = ("arrow_bg",)

#: Keys with a closed set of legal values.
_ENUMS = {"arrow_mode": ("header", "overlay")}


# --- validation -------------------------------------------------------------

def _is_hex(value):
    h = str(value).lstrip("#")
    if len(h) != 6:
        return False
    try:
        int(h, 16)
    except ValueError:
        return False
    return True


def _coerce(key, value, default, where):
    """One value from a theme file, checked against the default it replaces.

    Returns ``(ok, value)``. A rejected value is *dropped*, not fatal: the
    default stands and the rest of the theme still applies. A theme file is
    user-editable, so one bad colour must not cost you the whole look — and a
    silent drop would be worse, hence the log line.
    """
    def bad(why):
        log.warning("theme %s: ignoring %s (%s)", where, key, why)
        return False, default

    if key in _ENUMS:
        if value not in _ENUMS[key]:
            return bad("expected one of %s" % (", ".join(_ENUMS[key]),))
        return True, value
    if key in _COLOUR_KEYS:
        if not _is_hex(value):
            return bad("expected a \"rrggbb\" colour")
        return True, str(value).lstrip("#")
    if key == "tile_landscape":
        try:
            w, h = value
            return True, (int(w), int(h))
        except (TypeError, ValueError):
            return bad("expected [width, height]")
    if key == "browse_bg":
        # mpv's background-color; "#rrggbb" here, unlike everything else.
        if not _is_hex(value):
            return bad("expected a \"#rrggbb\" colour")
        return True, "#" + str(value).lstrip("#")
    if default is None:
        # tile_title_size / tile_sub_size: null means "leave it alone".
        if value is None:
            return True, None
        try:
            return True, int(value)
        except (TypeError, ValueError):
            return bad("expected a number or null")
    if isinstance(default, bool):
        if not isinstance(value, bool):
            return bad("expected true or false")
        return True, value
    for kind in (int, float, str):
        if isinstance(default, kind):
            try:
                return True, kind(value)
            except (TypeError, ValueError):
                return bad("expected %s" % kind.__name__)
    return bad("unsupported value")


def resolve(data, where="<memory>"):
    """Merge one theme file's contents over :data:`DEFAULT` and return a theme.

    Unknown keys are dropped with a warning rather than accepted, which is the
    whole reason this exists: the old code copied a theme's palette straight
    into the ``theme`` module's globals, so a typo silently defined a *new*
    global while the real one kept its old value — invisible, and the theme
    just looked subtly wrong. There is nowhere to put a typo now.
    """
    out = dict(DEFAULT)
    out["palette"] = dict(DEFAULT["palette"])
    if not isinstance(data, dict):
        log.warning("theme %s: expected a JSON object, ignoring", where)
        return out
    for key, value in data.items():
        if key == "palette":
            if not isinstance(value, dict):
                log.warning("theme %s: palette is not an object", where)
                continue
            for ckey, colour in value.items():
                if ckey not in DEFAULT["palette"]:
                    log.warning("theme %s: unknown palette colour %s",
                                where, ckey)
                    continue
                if not _is_hex(colour):
                    log.warning("theme %s: palette %s is not \"rrggbb\"",
                                where, ckey)
                    continue
                out["palette"][ckey] = str(colour).lstrip("#")
            continue
        if key not in DEFAULT:
            log.warning("theme %s: unknown key %s", where, key)
            continue
        ok, coerced = _coerce(key, value, DEFAULT[key], where)
        if ok:
            out[key] = coerced
    return out


# --- loading ----------------------------------------------------------------

def theme_dirs():
    """``(builtin, user)`` theme directories, either possibly None.

    Imported lazily: this module is pulled in by ``theme``, which nearly every
    view imports, and neither of these is needed to answer "what is the default
    palette" — which is all that matters until a theme is actually applied.
    """
    builtin = user = None
    try:
        from ..utils import get_resource

        builtin = get_resource(THEME_DIR)
    except Exception:
        log.debug("no built-in theme directory", exc_info=True)
    try:
        from .. import conffile
        from ..constants import APP_NAME

        user = conffile.get_dir(APP_NAME, THEME_DIR)
    except Exception:
        log.debug("no user theme directory", exc_info=True)
    return builtin, user


def _scan(directory, into):
    if not directory or not os.path.isdir(directory):
        return
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        log.warning("could not read theme directory %s", directory,
                    exc_info=True)
        return
    for filename in names:
        if not filename.lower().endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # A theme the user is midway through editing is a JSON syntax
            # error, and losing every OTHER theme over it — including the one
            # currently selected — would be a poor trade.
            log.warning("could not load theme %s", path, exc_info=True)
            continue
        theme_id = os.path.splitext(filename)[0].lower()
        resolved = resolve(data, where=path)
        # A theme that never says what it is called is labelled with its
        # filename, so it is still recognisable in the settings dropdown
        # instead of being a second entry called "Default".
        if not (isinstance(data, dict) and data.get("name")):
            resolved["name"] = theme_id
        into[theme_id] = resolved


def load(force=False):
    """The theme registry: ``{id: theme}``, built-ins then user overrides.

    Cached — themes take effect at startup, so re-reading the directories per
    frame would buy nothing. ``force`` re-reads, for the settings screen and
    for tests.
    """
    global _cache
    if _cache is not None and not force:
        return _cache
    themes = {"default": resolve({}, where="<built-in>")}
    builtin, user = theme_dirs()
    # User second: same id wins, which is the shadowing rule.
    _scan(builtin, themes)
    _scan(user, themes)
    _cache = themes
    return themes


_cache = None


def get(name):
    """A theme by id, falling back to ``default`` for unknown/None."""
    themes = load()
    fallback = themes.get("default") or resolve({})
    return themes.get((name or "default").lower(), fallback)


def choices(force=False):
    """``(label, id)`` pairs for the settings dropdown, ``default`` first and
    the rest alphabetical — the list is user-extensible now, so it needs an
    order that does not depend on directory listing order.

    ``(label, value)``, matching ``config.LABELED_ENUMS``, because that is
    what the settings form consumes."""
    themes = load(force=force)
    rest = sorted((k for k in themes if k != "default"),
                  key=lambda k: themes[k].get("name", k).lower())
    return [(themes[k].get("name", k), k) for k in ["default"] + rest
            if k in themes]
