"""Buttons and other clickable chrome shared across page families.

Only genuinely cross-family controls belong here. A control used by one page
family — the grid's sort/filter bar, the music action bar — is that page's
own method; moving it here would trade a mixin for a components dumping
ground, which is the same coupling with a nicer directory name.

See ``docs/ARCHITECTURE_TARGET.md`` §1.4 for the test: a component may take
render resources and callbacks, never ``nav``, ``source`` or ``route``.
"""

from typing import Any, List

from ...mpvtk.widgets import Button, Icon, Row, Text
from .. import theme


def action_btn(icon, text, node_id, cb, on=False, primary=False, size=16,
               autofocus=False):
    """An icon+label action button.

    ``primary`` is the accent-filled call to action (Play, Next Up); ``on``
    is a *toggle* that happens to share the accent fill (Watched, Favorite).
    Both use white on blue — black on blue read as disabled.

    Every button in an action row must come from here, icon or not: the
    plain Button widget defaults to a 20px label against this one's 16,
    which made the odd trailing button ~5px taller than its neighbours.

    ``autofocus`` nominates this button as the page's default for a
    keyboard or remote arrival (the renderer only honours it on a
    navigation it was asked to focus, and never while the pointer is
    driving). Off by default and set deliberately: on a page with two
    accented buttons, "the primary one" is not the same question as "the
    one a remote should land on".

    Was ``ViewsMixin._action_btn``.
    """
    accent = on or primary
    fg = theme.ACCENT_FG if accent else theme.TEXT_FG
    # Annotated: the optional Icon goes in first, so mypy would otherwise
    # infer list[Icon] from it and reject the Text that always follows.
    children: List[Any] = []
    if icon:
        children.append(Icon(icon, size + 2, color=fg))
    children.append(Text(text, size=size, color=fg))
    return Row(children,
               id=node_id, gap=7, pad=10,
               bg=theme.ACCENT if accent else theme.BUTTON_BG,
               hover={"fill": theme.ACCENT_HOVER if accent
                      else theme.BUTTON_ACTIVE},
               radius=6, align="center", on_click=cb, autofocus=autofocus)


def tab_btn(label, node_id, active, cb, style=None):
    """One tab in a tab bar — music library, Live TV, Settings.

    **Which tab you are on outranks which tab the pointer is over.** The
    plain Button widget defaults to ``hover={"fill": CONTROL_HOVER}``, and a
    hover fill paints over the background rather than blending with it — so
    an accent-filled active tab lost its accent the moment the cursor landed
    on it, which reads as the selection having moved away. Hovering the
    active tab lightens the accent instead (``action_btn`` already does this
    for its toggles; a tab bar is the same question asked of a set).

    Every tab bar goes through here rather than each spelling the two
    conditionals out, because that is how the bug survived in three places
    at once: the fix is not the colour, it is that one function decides.

    ``style`` merges a theme's chrome styling (``theme.chrome_button_style``
    — accent border, rounder corners, hover glow) underneath. **Its ``bg``
    and ``hover`` are overridden, never merged into**: those two keys are the
    whole point of this function, and the accented-button themes set both.
    A ``glow`` in the theme's hover is kept, since that is decoration on top
    of a fill rather than a competing statement about which tab is current.
    """
    style = dict(style or {})
    hover = {k: v for k, v in (style.pop("hover", None) or {}).items()
             if k != "fill"}
    hover["fill"] = theme.ACCENT_HOVER if active else theme.BUTTON_ACTIVE
    style.pop("bg", None)
    return Button(label, id=node_id,
                  bg=theme.ACCENT if active else theme.BUTTON_BG,
                  fg=theme.ACCENT_FG if active else theme.TEXT_FG,
                  hover=hover, on_click=cb, **style)
