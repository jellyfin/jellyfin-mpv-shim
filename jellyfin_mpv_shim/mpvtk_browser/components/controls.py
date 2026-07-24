"""Buttons and other clickable chrome shared across page families.

Only genuinely cross-family controls belong here. A control used by one page
family — the grid's sort/filter bar, the music action bar — is that page's
own method; moving it here would trade a mixin for a components dumping
ground, which is the same coupling with a nicer directory name.

See ``docs/ARCHITECTURE_TARGET.md`` §1.4 for the test: a component may take
render resources and callbacks, never ``nav``, ``source`` or ``route``.
"""

from typing import Any, List

from ...mpvtk.widgets import Icon, Row, Text
from .. import theme


def action_btn(icon, text, node_id, cb, on=False, primary=False, size=16):
    """An icon+label action button.

    ``primary`` is the accent-filled call to action (Play, Next Up); ``on``
    is a *toggle* that happens to share the accent fill (Watched, Favorite).
    Both use white on blue — black on blue read as disabled.

    Every button in an action row must come from here, icon or not: the
    plain Button widget defaults to a 20px label against this one's 16,
    which made the odd trailing button ~5px taller than its neighbours.

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
               radius=6, align="center", on_click=cb)
