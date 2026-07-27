"""Translate jellyfin-web's themes into jellyfin-mpv-shim theme JSON.

Run against a jellyfin-web checkout's src/themes/ values, which are
transcribed into THEMES below. Writes into jellyfin_mpv_shim/themes/ and
prints a contrast report for each.

The two LIGHT themes were held back at first: their palettes were fine, but
mpvtk's widget defaults were a hardcoded dark palette that most of the
browser inherited, so they rendered near-white text on a near-white page.
They ship now that those defaults are design tokens the theme drives.

jf-web's palettes are CSS: many values are rgba() overlays that only resolve
against whatever they sit on. Our palette is opaque hex, so every translucent
value has to be composited against its actual backdrop rather than guessed at.
That is the whole reason this is a script and not hand-typed hex.
"""
import json
import os

OUT = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "jellyfin_mpv_shim", "themes")


def rgb(v):
    """'#rrggbb' | 'rgba(r,g,b,a)' | '#rgb' -> (r, g, b, a)."""
    v = v.strip()
    if v.startswith("rgba(") or v.startswith("rgb("):
        parts = v[v.index("(") + 1:v.rindex(")")].split(",")
        nums = [float(p) for p in parts]
        if len(nums) == 3:
            nums.append(1.0)
        return (nums[0], nums[1], nums[2], nums[3])
    h = v.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)


def over(fg, bg):
    """Composite fg over bg; returns 'rrggbb'."""
    fr, fg_, fb, fa = rgb(fg)
    br, bg_, bb, _ = rgb(bg)
    return "%02x%02x%02x" % tuple(
        int(round(f * fa + b * (1 - fa)))
        for f, b in ((fr, br), (fg_, bg_), (fb, bb)))


def mix(a, b, t):
    ar, ag, ab, _ = rgb(a)
    br, bg_, bb, _ = rgb(b)
    return "%02x%02x%02x" % tuple(
        int(round(x + (y - x) * t))
        for x, y in ((ar, br), (ag, bg_), (ab, bb)))


def luminance(c):
    r, g, b, _ = rgb(c)
    def lin(x):
        x /= 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# jf-web's base palette (_base/_palette.scss)
PRIMARY = "#00a4dc"
PRIMARY_LIGHT = "#33b6e3"
PRIMARY_DARK = "#00729a"

# Each entry is jf-web's own values, taken from src/themes/<id>/theme.scss.
# Anything the theme does not override falls back to the base palette, exactly
# as the SCSS `@use ... with (...)` does.
THEMES = {
    "jf-blueradiance": dict(
        label="Blue Radiance (Jellyfin)",
        dark=True,
        bg="#033361", paper="#011432",
        text="#eee", text2="rgba(255, 255, 255, 0.9)",
        divider="rgba(255, 255, 255, 0.12)",
        btn="rgba(0, 0, 0, 0.5)", btn_hover="rgba(0, 0, 0, 0.7)",
        accent=PRIMARY, accent_light=PRIMARY_LIGHT, accent_dark=PRIMARY_DARK,
    ),
    "jf-wmc": dict(
        label="Windows Media Center (Jellyfin)",
        dark=True,
        bg="#0f3562", paper="#0c2450",
        text="#eee", text2="rgba(255, 255, 255, 0.9)",
        divider="rgba(255, 255, 255, 0.12)",
        btn="#082845", btn_hover="#143451",
        accent=PRIMARY, accent_light=PRIMARY_LIGHT, accent_dark=PRIMARY_DARK,
    ),
    "jf-purplehaze": dict(
        label="Purple Haze (Jellyfin)",
        dark=True,
        # background-default is inherited from the base (#101010) by the MUI
        # scheme, but the legacy sheet paints html/dialogs #230c33 and the page
        # backdrop #030322. The backdrop is what our window actually is.
        bg="#030322", paper="#000420",
        panel="#230c33",       # .dialog / .nowPlayingPlaylist
        text="rgba(248, 248, 254, 0.973)",
        text2="rgba(255, 255, 255, 0.5)",
        divider="rgba(255, 255, 255, 0.14)",
        btn="rgba(0, 0, 0, 0.5)", btn_hover="rgba(0, 0, 0, 0.7)",
        # Its own teal, not the Jellyfin blue: .button-link, .progressring,
        # .selectionCommandsPanel are all #48c3c8, hover/played is #0ce8d6.
        accent="#48c3c8", accent_light="#0ce8d6", accent_dark="#1f6d70",
        rounded=True,          # .cardContent { border-radius: 0.8em }
        watched="#0ce8d6",     # .playedIndicator
        fav="#cc3333",         # .playstatebutton-icon-played
    ),
    # The two light themes take $primary-DARK as their on-page accent. Our
    # ACCENT is one colour doing two jobs -- a fill behind white text, and a
    # line drawn ON the page (hover rings, focus borders, progress) -- and
    # Jellyfin blue cannot do the second job on a near-white background: it
    # lands at 2.5:1. jf-web has $primary-dark for exactly this, and MUI's
    # light mode uses it the same way, so this is its token used for its
    # purpose rather than an invention.
    "jf-light": dict(
        label="Light (Jellyfin)",
        dark=False,
        bg="#f2f2f2", paper="#e8e8e8",
        text="#000", text2="rgba(0, 0, 0, 0.87)",
        divider="rgba(0, 0, 0, 0.14)",
        btn="#d8d8d8", btn_hover="#cccccc",
        accent=PRIMARY_DARK, accent_light=PRIMARY, accent_dark=PRIMARY_DARK,
    ),
    "jf-appletv": dict(
        label="Apple TV (Jellyfin)",
        dark=False,
        bg="#d5e9f2", paper="#eaeaea",
        text="rgba(0, 0, 0, 0.87)", text2="rgba(0, 0, 0, 0.87)",
        divider="rgba(0, 0, 0, 0.158)",
        btn="rgba(0, 0, 0, 0.14)", btn_hover="rgba(0, 0, 0, 0.24)",
        entry="rgba(255, 255, 255, 0.9)",     # $filledInput-bg
        panel="#bcbcbc",                       # $appBar-defaultBg
        accent=PRIMARY_DARK, accent_light=PRIMARY, accent_dark=PRIMARY_DARK,
    ),
}


def separate(colour, backdrop, dark, floor=1.30):
    """Nudge ``colour`` away from ``backdrop`` until it is at least ``floor``.

    DELIBERATE DEVIATION. Several jf-web themes give buttons and panels a flat
    rgba(0,0,0,0.5), which reads fine there because a full-bleed backdrop
    IMAGE sits behind it — Purple Haze's bg.jpg most of all. Our browser paints
    a flat window colour, so the same overlay lands within 1% of the page and
    the control disappears. Pushed apart in whichever direction the theme is
    already going.
    """
    target = "#ffffff" if dark else "#000000"
    out = colour
    for _ in range(24):
        if contrast(out, backdrop) >= floor:
            break
        out = mix(out, target, 0.06)
    return out


def build(spec):
    bg, paper, dark = spec["bg"], spec["paper"], spec["dark"]
    # Text and dividers are translucent in CSS; they only mean anything once
    # composited against the surface they are drawn on.
    text = over(spec["text"], bg)
    text2 = over(spec["text2"], bg)
    palette = {
        "WINDOW_BG": over(bg, "#000000"),
        "CARD_BG": over(paper, bg),
        # The chrome surface: jf-web's AppBar, or the paper colour nudged
        # away from the window so panels read as raised.
        "PANEL_BG": over(spec.get("panel", paper), bg)
        if spec.get("panel") else mix(over(paper, bg),
                                      "#ffffff" if not dark else "#ffffff",
                                      0.06 if dark else 0.0),
        # An empty card slot: recessed relative to a filled one.
        "PLACEHOLDER_BG": mix(over(paper, bg),
                              "#000000" if dark else "#000000",
                              0.25 if dark else 0.10),
        # The button must be findable against the page...
        "BUTTON_BG": separate(over(spec["btn"], bg), bg, dark),
        # ...and its hover must differ from the BUTTON, which is the
        # relationship that carries the feedback. Separating it from the page
        # instead let one theme's hover overshoot to 2.5:1 against its own
        # button while another sat at 1.17:1.
        "BUTTON_ACTIVE": separate(over(spec["btn_hover"], bg),
                                  separate(over(spec["btn"], bg), bg, dark),
                                  dark, floor=1.20),
        "ENTRY_BG": separate(
            over(spec.get("entry",
                          "rgba(0,0,0,0.5)" if dark else paper), bg),
            bg, dark, floor=1.15),
        "BORDER": over(spec["divider"], bg),
        "TEXT_FG": text,
        # DELIBERATE DEVIATION. Blue Radiance and WMC set $text-secondary to
        # rgba(255,255,255,0.9), which over their backgrounds is within 3% of
        # their primary text. That works in jf-web because its secondary text
        # is also smaller and lighter-weight; our tile subtitles and field
        # labels carry that hierarchy in the COLOUR, and at 3% apart it
        # collapses. Where the theme's own secondary is not visibly dimmer
        # than its primary, dim it ourselves.
        "SUBTLE_FG": text2 if contrast(text2, text) >= 1.5
        else mix(text, bg, 0.45),
        "ACCENT": over(spec["accent"], bg),
        "ACCENT_HOVER": over(spec["accent_light"], bg),
        # A fill that sits BEHIND text. jf-web darkens the accent for this,
        # which only works on a dark surface -- on a light theme the same move
        # gives dark-on-dark. Lighten there instead.
        "ACCENT_SOFT": over(spec["accent_dark"], bg) if dark
        else mix(over(spec["accent"], bg), "#ffffff", 0.72),
        # What is drawn ON an accent fill.
        "ACCENT_FG": "ffffff" if luminance(spec["accent"]) < 0.45 else "101010",
    }
    for key, src in (("WATCHED_GREEN", "watched"), ("FAV_RED", "fav")):
        if spec.get(src):
            palette[key] = over(spec[src], bg)

    theme = {"name": spec["label"], "palette": palette,
             "browse_bg": "#" + palette["WINDOW_BG"]}
    if spec.get("rounded"):
        theme["rounded"] = True
    return theme


if __name__ == "__main__":
    from jellyfin_mpv_shim.mpvtk_browser import themes as shim_themes

    for tid, spec in THEMES.items():
        theme = build(spec)
        path = os.path.join(OUT, tid + ".json")
        with open(path, "w") as fh:
            json.dump(theme, fh, indent=4)
            fh.write("\n")
        p = theme["palette"]
        print("=== %-16s %s" % (tid, theme["name"]))
        for pair, floor in (
                (("TEXT_FG", "WINDOW_BG"), 4.5),
                (("SUBTLE_FG", "WINDOW_BG"), 3.0),
                (("TEXT_FG", "CARD_BG"), 4.5),
                (("SUBTLE_FG", "CARD_BG"), 3.0),
                (("TEXT_FG", "BUTTON_BG"), 4.5),
                (("ACCENT_FG", "ACCENT"), 4.5),
                (("TEXT_FG", "ACCENT_SOFT"), 4.5),
                (("ACCENT", "WINDOW_BG"), 3.0)):
            ratio = contrast(p[pair[0]], p[pair[1]])
            flag = "  " if ratio >= floor else "!!"
            print("  %s %-24s %5.2f  (want >= %.1f)"
                  % (flag, "%s on %s" % pair, ratio, floor))
        # Every generated file must survive the real loader untouched.
        resolved = shim_themes.resolve(json.loads(json.dumps(theme)),
                                       where=path)
        assert resolved["palette"]["ACCENT"] == p["ACCENT"], tid
