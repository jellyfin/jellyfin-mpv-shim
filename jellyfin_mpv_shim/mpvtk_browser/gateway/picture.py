"""Showing a still picture in the player's window, with nobody playing it.

The comic reader's half of the gateway. A comic page is *played* rather
than drawn — mpv decodes pictures already, keeps them on the GPU, and has
``video-zoom`` / ``video-pan-x`` / ``video-pan-y``, which is a better deal
than decoding each page with Pillow and pushing a viewport-sized bitmap
through the overlay transport on every pan (see ``jellyfin_mpv_shim.comic``
for the measurement behind that).

**This is a third window state, and it is the reason this file exists.**
The browser has had two: browsing (it owns the window, drawing over mpv's
idle background) and yielded (playback owns the window, the browser pushes
an empty scene). A comic is both at once — a picture in the window with
the library's own chrome over it — so it goes through neither
``_start`` nor ``_yield``.

That works because ``PlayerManager`` keys almost everything off
``self._video``, which stays None here: ``_on_eof_reached`` and
``_on_playback_abort`` both return without it, so nothing advances a queue;
the timeline reports nothing because there is no session; and ``idle_quit``
already refuses to fire while ``mpvtk_active``, which is exactly the state
the browser is in. What is *not* free is tidiness at the edges, so:

* ``keep_open`` and ``image_display_duration`` are set here and put back in
  :meth:`clear_picture`, because an image otherwise shows for one second
  and then mpv idles;
* leaving the reader must clear the picture, or the comic stays behind the
  library grid — the shell does that on a screen change;
* a real playback start replaces the picture, which is correct and needs
  no coordination: ``loadfile`` replaces whatever is loaded.
"""

import logging
import math

from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.picture")


class PictureMixin(GatewayCore):
    """Display a local image file, and move it about."""

    def show_picture(self, path):
        """Put ``path`` in the window. Returns True if it was asked for.

        Fire-and-forget through ``_act`` like every other player action:
        the work runs on the player's action thread, and the caller is the
        render loop, which must not wait on mpv.

        Note the shape — ``_act`` hands the callback the **PlayerManager**,
        not the mpv handle. Writing it the other way round is quiet rather
        than loud: ``pm.keep_open = True`` invents an attribute on the
        manager and only the first *method* call raises, so two of the
        three property writes went nowhere and the failure surfaced one
        line later than it started.
        """
        if not path:
            return False
        self._act(lambda pm: pm.show_picture(path))
        return True

    def clear_picture(self):
        """Take the picture down and put the browse window back."""
        self._act(lambda pm: pm.clear_picture())

    def reset_picture_view(self):
        """Undo a comic's zoom and pan, whatever is playing now.

        Separate from :meth:`clear_picture` because it has to run in the
        case that one refuses: playback starting is how a comic stops being
        on screen, and these are global mpv options, so a film that
        inherited the page's 2.67x zoom would keep it — and so would every
        film after it.
        """
        self._act(lambda pm: pm.reset_picture_view())

    def set_picture_view(self, zoom=None, pan_x=None, pan_y=None):
        """Set any of mpv's three view properties.

        ``zoom`` is mpv's own units — a **log2 exponent** on top of the
        scale that fits the picture in the window, so 0 is fit and 1 is
        twice that. :func:`fit_zoom` is what turns a reading mode into one.
        """
        self._act(lambda pm: pm.set_picture_view(zoom=zoom, pan_x=pan_x,
                                                 pan_y=pan_y))


def fit_scale(picture, window):
    """Pixels of window per pixel of picture at ``video-zoom`` 0.

    mpv letterboxes: it fits the whole picture inside the window, so the
    scale it picks is the smaller of the two ratios. Every other number
    here is relative to this one.
    """
    pw, ph = picture
    ww, wh = window
    if not pw or not ph or not ww or not wh:
        return 1.0
    return min(ww / float(pw), wh / float(ph))


def fit_zoom(picture, window, area, mode, zoom=1.0):
    """mpv's ``video-zoom`` for a reading mode.

    ``area`` is the height available *between the reader's bars*, which is
    not the window: mpv knows nothing about them, so fitting a page to the
    window puts its top and bottom underneath the chrome. ``mode`` is
    "width" (the comic default — the page as wide as the window, scrolled
    vertically) or "page" (the whole page visible at once). ``zoom``
    multiplies whichever of those was asked for.
    """
    pw, ph = picture
    ww, _wh = window
    base = fit_scale(picture, window)
    if not pw or not ph or not base:
        return 0.0
    if mode == "page":
        want = min(ww / float(pw), area / float(ph))
    else:
        want = ww / float(pw)
    want *= max(0.01, float(zoom))
    return math.log(want / base, 2)


def pan_bounds(picture, window, area, top, zoom):
    """How far the picture may be panned, in mpv's units.

    Returns ``(min_x, max_x, min_y, max_y)``.

    **The unit is the SCALED PICTURE, not the window** — measured, because
    it is the one thing here that cannot be reasoned out and the two
    readings differ by whatever the zoom is. With a 1400x2100 page at
    ``video-zoom`` 1.415 in a 1280x720 window (displayed height 1919), a
    ``video-pan-y`` of 0.4 put the page's top edge at y=168, which is
    ``-599.5 + 0.4 * 1919`` to within a pixel. Against the window's 720 it
    would have been y=-311, and reading it that way is what put the first
    version of this page entirely off the screen.

    **The sign moves the picture, not the viewport**: a positive pan pushes
    the picture *down*, which reveals its top. So the top of a page is
    ``max_y``.

    The picture must not be draggable off the reading area: an axis it is
    smaller than is pinned to the centre of that area, and one it is larger
    than stops when its edge reaches the edge. ``top`` is where the reading
    area starts inside the window (the top bar's height), so the vertical
    answer is offset by the chrome as well as bounded by it.
    """
    pw, ph = picture
    ww, wh = window
    if not pw or not ph or not ww or not wh:
        return 0.0, 0.0, 0.0, 0.0
    scale = fit_scale(picture, window) * (2.0 ** zoom)
    dw, dh = pw * scale, ph * scale
    if not dw or not dh:
        return 0.0, 0.0, 0.0, 0.0
    # An axis the picture does not fill is centred in the reading area
    # rather than in the window -- the bars are not part of the page.
    if dw <= ww:
        min_x = max_x = 0.0
    else:
        reach = (dw - ww) / 2.0 / dw
        min_x, max_x = -reach, reach
    centre = (top + area / 2.0 - wh / 2.0) / dh
    if dh <= area:
        min_y = max_y = centre
    else:
        # top edge at the area's top  <->  p = (top - wh/2 + dh/2) / dh
        max_y = (top - wh / 2.0 + dh / 2.0) / dh
        min_y = (top + area - wh / 2.0 - dh / 2.0) / dh
    return min_x, max_x, min_y, max_y
