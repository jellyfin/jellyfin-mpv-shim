"""Where mpv puts a still picture, and in what units.

The comic reader hands mpv a page and then moves it with ``video-zoom``,
``video-pan-x`` and ``video-pan-y``. None of that is drawn by us, so the
only thing to test is the arithmetic that decides the three numbers — and
one fact about mpv that had to be *measured* rather than reasoned out.

**The measurement.** A 1400x2100 page at ``video-zoom`` 1.415 in a 1280x720
window is displayed 1920 px tall. Setting ``video-pan-y`` to 0.4 put the
page's top edge at y=168, which is ``-599.5 + 0.4 * 1919`` — so the unit is
the **scaled picture**, not the window. Read as the window it would have
been y=-311, and that reading put the first version of this page entirely
off the screen with no error anywhere.
"""

import unittest

from jellyfin_mpv_shim.mpvtk_browser.gateway.picture import (
    fit_scale, fit_zoom, pan_bounds)

#: The measured case, so the numbers below are the ones from the window.
PAGE = (1400, 2100)
WINDOW = (1280, 720)
TOP = 44                    # the reader's top bar
AREA = 720 - 44 - 48        # what is left between the bars


def top_edge(zoom, pan):
    """Where the picture's top edge lands, in window pixels."""
    scale = fit_scale(PAGE, WINDOW) * (2.0 ** zoom)
    dh = PAGE[1] * scale
    return WINDOW[1] / 2.0 - dh / 2.0 + pan * dh


def bottom_edge(zoom, pan):
    scale = fit_scale(PAGE, WINDOW) * (2.0 ** zoom)
    dh = PAGE[1] * scale
    return WINDOW[1] / 2.0 + dh / 2.0 + pan * dh


class TestMeasuredUnit(unittest.TestCase):
    def test_the_pan_unit_is_the_scaled_picture(self):
        """The measurement itself, as an executable statement of it."""
        zoom = fit_zoom(PAGE, WINDOW, AREA, "width")
        self.assertAlmostEqual(top_edge(zoom, 0.4), 168.0, delta=1.0)


class TestFit(unittest.TestCase):
    def test_fit_width_fills_the_window_horizontally(self):
        zoom = fit_zoom(PAGE, WINDOW, AREA, "width")
        scale = fit_scale(PAGE, WINDOW) * (2.0 ** zoom)
        self.assertAlmostEqual(PAGE[0] * scale, WINDOW[0], delta=1.0)

    def test_fit_page_fits_between_the_bars_not_the_window(self):
        """mpv knows nothing about the reader's chrome, so fitting to the
        window puts the top and bottom of the page underneath it."""
        zoom = fit_zoom(PAGE, WINDOW, AREA, "page")
        scale = fit_scale(PAGE, WINDOW) * (2.0 ** zoom)
        self.assertAlmostEqual(PAGE[1] * scale, AREA, delta=1.0)
        self.assertLessEqual(PAGE[0] * scale, WINDOW[0] + 1)

    def test_zoom_multiplies_whichever_fit_was_asked_for(self):
        one = fit_zoom(PAGE, WINDOW, AREA, "width", 1.0)
        two = fit_zoom(PAGE, WINDOW, AREA, "width", 2.0)
        self.assertAlmostEqual(two - one, 1.0, places=6)   # log2(2)

    def test_a_degenerate_size_does_not_raise(self):
        """A page whose header would not read leaves the size None, and a
        zero here must not take the render pass down with it."""
        for picture in ((0, 0), (1400, 0), (0, 2100)):
            self.assertEqual(fit_zoom(picture, WINDOW, AREA, "width"), 0.0)
            self.assertEqual(pan_bounds(picture, WINDOW, AREA, TOP, 0.0),
                             (0.0, 0.0, 0.0, 0.0))


class TestBounds(unittest.TestCase):
    def test_the_page_top_lands_at_the_top_of_the_reading_area(self):
        zoom = fit_zoom(PAGE, WINDOW, AREA, "width")
        _minx, _maxx, _min_y, max_y = pan_bounds(PAGE, WINDOW, AREA, TOP,
                                                 zoom)
        self.assertAlmostEqual(top_edge(zoom, max_y), TOP, delta=1.0)

    def test_the_page_bottom_lands_at_the_bottom_of_it(self):
        zoom = fit_zoom(PAGE, WINDOW, AREA, "width")
        _minx, _maxx, min_y, _max_y = pan_bounds(PAGE, WINDOW, AREA, TOP,
                                                 zoom)
        self.assertAlmostEqual(bottom_edge(zoom, min_y), TOP + AREA,
                               delta=1.0)

    def test_a_page_that_fits_is_pinned_centred_between_the_bars(self):
        zoom = fit_zoom(PAGE, WINDOW, AREA, "page")
        _minx, _maxx, min_y, max_y = pan_bounds(PAGE, WINDOW, AREA, TOP,
                                                zoom)
        self.assertEqual(min_y, max_y, "a page that fits can still be moved")
        middle = (top_edge(zoom, max_y) + bottom_edge(zoom, max_y)) / 2.0
        self.assertAlmostEqual(middle, TOP + AREA / 2.0, delta=1.0)

    def test_the_page_cannot_be_dragged_off_the_screen(self):
        """Swept across zooms, because the bound is only interesting where
        the picture is bigger than the area — and at one zoom it is easy to
        pick a case where any formula looks right."""
        overflowed = 0
        for step in range(1, 24):
            zoom = fit_zoom(PAGE, WINDOW, AREA, "width", 0.25 * step)
            _mnx, _mxx, min_y, max_y = pan_bounds(PAGE, WINDOW, AREA, TOP,
                                                  zoom)
            self.assertLessEqual(min_y, max_y + 1e-9)
            if bottom_edge(zoom, 0.0) - top_edge(zoom, 0.0) <= AREA:
                # Smaller than the area: pinned centred, and the edges are
                # inside it rather than on it.
                self.assertEqual(min_y, max_y)
                continue
            overflowed += 1
            self.assertLessEqual(top_edge(zoom, max_y), TOP + 1.0)
            self.assertGreaterEqual(bottom_edge(zoom, min_y),
                                    TOP + AREA - 1.0)
        self.assertGreater(overflowed, 10, "the sweep never overflowed")

    def test_a_wide_page_can_be_panned_sideways_and_a_narrow_one_cannot(self):
        wide = (4000, 1000)
        zoom = fit_zoom(wide, WINDOW, AREA, "width", 3.0)
        min_x, max_x, _y0, _y1 = pan_bounds(wide, WINDOW, AREA, TOP, zoom)
        self.assertLess(min_x, 0.0)
        self.assertGreater(max_x, 0.0)
        min_x, max_x, _y0, _y1 = pan_bounds(PAGE, WINDOW, AREA, TOP,
                                            fit_zoom(PAGE, WINDOW, AREA,
                                                     "page"))
        self.assertEqual((min_x, max_x), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
