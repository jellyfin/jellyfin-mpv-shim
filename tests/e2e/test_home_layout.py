"""The home screen's layout lives on the SERVER, in jellyfin-web's document.

Everything here is interop, and interop is the one thing a fake cannot
disagree about: the fake is written from the same reading of the API the code
is, so both are wrong together. What is at stake is another client's home
screen -- the layout is DisplayPreferences under id ``usersettings`` and
client ``emby``, which is jellyfin-web's legacy namespace, and any other
client string addresses a different, empty preference set that only this
application can see.

Two encoding rules carry the whole interop contract and both are easy to
regress because getting either wrong still works *here*:

* **An empty slot means that slot's default, not "none".** Only the literal
  ``"none"`` blanks a slot. Read it the other way and a user who has never
  touched their home screen gets a blank one.
* **A slot holding its own default is written back as ``""``.** Writing the
  literal value instead pins that user's layout to today's defaults forever,
  including on the web client, and nothing about the screen looks wrong when
  it happens.

Plus the two things a save must not destroy: section types this browser
cannot draw (they are preserved, so configuring the shim never degrades the
same user's web home screen), and the rest of the DisplayPreferences
document, which the guide settings share.

**Everything here writes real server state, so both classes normalise in
``setUp`` as well as restoring on cleanup.** That is `test_playback_eof`'s
discipline applied to a different resource, and it is not only about this
file: a home layout left customised by an interrupted run takes the
library-tiles row off the home screen, and `test_keyboard_nav` walks
exactly that row. The failure lands there, as "no node id containing
<guid>", with nothing to point back here.

The last class covers ``LatestItemsExcludes`` -- "Display in home screen
sections" -- which is applied by the *server* for Continue Watching and Next
Up and by *us* for the per-library Latest rows, because those are one request
each with a ParentId and that bypasses the server's own handling. Two halves
of one setting, and the failure mode of getting the split wrong is a library
the user hid showing up anyway.

The server half is also a claim about **which endpoint** is asked, and that
is why it is asserted on the row and not on the request: only
``GetResumeItems`` and ``GetNextUp`` consult the setting, so a resume query
sent to the generic item route carries no ParentId, looks entirely correct
from the parameters, and ignores the exclusion (#703).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser import home_sections  # noqa: E402

UUID = _e2e.SOURCE_UUID

#: jellyfin-web's DisplayPreferences client namespace, spelled out rather
#: than imported. Reading it from `home_sections` would make the interop test
#: agree with whatever the code currently says -- the value IS the claim.
WEB_CLIENT = "emby"


class _PrefsTest(_e2e.E2ETestCase):
    """A real LibrarySource, with the user's DisplayPreferences document put
    back exactly as it was found."""

    def setUp(self):
        super().setUp()
        self.source = self.session.library_source()
        self.original = self._dto()
        self.addCleanup(self._restore)
        # Normalised in setUp as well as restored on cleanup, the way
        # `test_playback_eof` resets its series' playstate and for the same
        # reason: a run that died halfway leaves this account's home layout
        # however the failing test left it, and the *next* run measures that
        # instead. It does not stay inside this file either -- the layout is
        # real server state, and `test_keyboard_nav` walks the home screen's
        # library tiles, which only exist while some slot still holds
        # `smalllibrarytiles`. Losing that row there reads as a keyboard
        # regression ("no node id containing <guid>") with nothing pointing
        # back here.
        self._write_custom(**{"homesection%d" % slot: ""
                              for slot in range(home_sections.SLOT_COUNT)})

    def _dto(self):
        return self.session.api.get_user_settings(
            client=home_sections.DISPLAY_PREFS_CLIENT) or {}

    def _custom(self):
        return self._dto().get("CustomPrefs") or {}

    def _write_custom(self, **pairs):
        """Write raw CustomPrefs keys, the way jellyfin-web would."""
        dto = self._dto()
        custom = dict(dto.get("CustomPrefs") or {})
        custom.update(pairs)
        dto["CustomPrefs"] = custom
        self.session.api.update_user_settings(
            dto, client=home_sections.DISPLAY_PREFS_CLIENT)

    def _restore(self):
        try:
            self.session.api.update_user_settings(
                self.original, client=home_sections.DISPLAY_PREFS_CLIENT)
        except Exception:
            pass

    def _resolved(self):
        """What the browser would draw, re-read from the server."""
        layout, _excludes = self.source.get_home_prefs(UUID, refresh=True)
        return layout


@_e2e.require_server
class HomeLayoutRoundTripTest(_PrefsTest):

    def test_a_saved_layout_comes_back_from_the_server(self):
        wanted = list(home_sections.DEFAULT_LAYOUT)
        wanted[0], wanted[1] = home_sections.NEXT_UP, home_sections.LATEST
        self.source.save_home_layout(UUID, wanted)
        self.assertEqual(self._resolved(), wanted)

    def test_the_layout_is_written_where_jellyfin_web_reads_it(self):
        """The client string is the whole of the interop.

        `emby` is jellyfin-web's legacy namespace. Any other value addresses
        a preference set that resolves, saves and reads back perfectly --
        and that no other client can see, so the symptom is not an error but
        a home screen the user configured here and nowhere else.
        """
        wanted = list(home_sections.DEFAULT_LAYOUT)
        wanted[1] = home_sections.NEXT_UP
        self.source.save_home_layout(UUID, wanted)

        # Read back under the LITERAL client name. Asking `home_sections`
        # for it would have this test agree with the code no matter what
        # the code said, which is exactly the failure it exists to catch.
        web = self.session.api.get_user_settings(client=WEB_CLIENT) or {}
        self.assertEqual((web.get("CustomPrefs") or {}).get("homesection1"),
                         home_sections.NEXT_UP,
                         "the layout was not written where jellyfin-web "
                         "reads it (client %r)" % WEB_CLIENT)
        other = self.session.api.get_user_settings(client="jms-not-emby") or {}
        self.assertNotEqual(
            (other.get("CustomPrefs") or {}).get("homesection1"),
            home_sections.NEXT_UP,
            "the layout is readable under a client name that is not "
            "jellyfin-web's, so this test cannot tell the two apart")

    def test_a_blank_slot_comes_back_as_that_slots_default(self):
        """Measured, and not what the encoding rules assume.

        The shim writes ``""`` for a slot holding its own default, which is
        what jellyfin-web stores -- but **this server does not keep the
        blank**. It answers the literal default instead, so a slot written
        as empty reads back as e.g. ``smalllibrarytiles``. Both of the
        pure-logic rules are still right and are pinned by
        `tests/test_home_sections.py`; what only a real server can say is
        that the blank never survives to be read, which is why "empty means
        the default" cannot be exercised end to end here at all.

        Asserted because it decides what the *other* tests in this file can
        mean. If a later server version starts storing the blank, this test
        fails and says so, rather than the round-trip tests quietly starting
        to cover a different path.
        """
        self._write_custom(**{"homesection%d" % slot: ""
                              for slot in range(home_sections.SLOT_COUNT)})
        custom = self._custom()
        self.assertEqual(
            [custom.get("homesection%d" % slot)
             for slot in range(home_sections.SLOT_COUNT)],
            list(home_sections.DEFAULT_LAYOUT),
            "the server's handling of a blank slot has changed")
        # Either way round, what the browser draws is the default -- which
        # is the property the encoding exists to produce.
        self.assertEqual(self._resolved(),
                         list(home_sections.DEFAULT_LAYOUT))

    def test_only_the_literal_none_blanks_a_slot(self):
        self._write_custom(homesection1=home_sections.NONE)
        self.assertEqual(self._resolved()[1], home_sections.NONE)

    def test_a_save_preserves_a_section_this_browser_cannot_draw(self):
        """Configuring the shim must not degrade the same user's web home
        screen. jellyfin-web has section types we do not render; a save that
        normalised them to "none" would delete them from that user's account
        with nothing on either client to say so.
        """
        alien = "librarybuttons"
        self._write_custom(homesection3=alien)
        layout = self._resolved()
        self.assertEqual(layout[3], alien,
                         "an unknown section type was not even read back")

        layout[0] = home_sections.LATEST     # change something else
        self.source.save_home_layout(UUID, layout)

        self.assertEqual(self._custom().get("homesection3"), alien,
                         "saving the home layout rewrote a section type "
                         "this browser cannot draw")

    def test_saving_the_layout_leaves_the_rest_of_the_document_alone(self):
        """There is no partial-update path on this API: a save GETs the whole
        DisplayPreferences document, mutates CustomPrefs and POSTs it back.
        The guide preferences live in that same document, so a save that
        rebuilt it rather than editing it takes them with it.
        """
        self._write_custom(**{"livetv-channelorder": "DatePlayed",
                              "guide-indicator-hd": "false"})
        self.source.save_home_layout(UUID,
                                     list(home_sections.DEFAULT_LAYOUT))
        custom = self._custom()
        self.assertEqual(custom.get("livetv-channelorder"), "DatePlayed")
        self.assertEqual(custom.get("guide-indicator-hd"), "false")

    def test_the_layout_survives_several_saves_unchanged(self):
        """Resolve-then-serialize has to be a fixed point.

        One save cannot show this: every rule here is a translation between
        two encodings, and a translation that is wrong in one direction only
        looks perfect until the value goes round again. A layout that walks
        is a user's home screen quietly rearranging itself.
        """
        wanted = list(home_sections.DEFAULT_LAYOUT)
        wanted[2] = home_sections.LATEST
        seen = []
        for _ in range(3):
            self.source.save_home_layout(UUID, wanted)
            seen.append(self._resolved())
        self.assertEqual(seen, [wanted, wanted, wanted])


@_e2e.require_server
class LatestExcludesTest(_e2e.E2ETestCase):
    """"Display in home screen sections", which the server applies for some
    rows and we apply for the others."""

    def setUp(self):
        super().setUp()
        self.source = self.session.library_source()
        self.user = self.session.api.get_user() or {}
        self.config = dict(self.user.get("Configuration") or {})
        self.addCleanup(self._restore_config)
        # Cleared here as well as restored on cleanup -- see _PrefsTest.
        # A library left excluded by a run that died halfway is a library
        # missing from the home screen for every later test.
        self._exclude()

    def _restore_config(self):
        try:
            self.session._request(
                "/Users/%s/Configuration" % self.session.user_id,
                method="POST", body=self.config)
        except Exception:
            pass

    def _exclude(self, *library_ids):
        config = dict(self.config)
        config["LatestItemsExcludes"] = list(library_ids)
        self.session._request(
            "/Users/%s/Configuration" % self.session.user_id,
            method="POST", body=config)

    def test_the_exclusion_list_is_read_off_the_user_configuration(self):
        movies = self.session.view("Movies")
        self._exclude(movies["Id"])
        self.assertIn(movies["Id"],
                      self.source.get_latest_excludes(UUID))

    def test_an_excluded_library_gets_no_recently_added_row(self):
        """The per-library Latest rows are one request each **with** a
        ParentId, which bypasses the server's own handling of this setting --
        so it has to be applied here, exactly as jellyfin-web's
        recentlyAdded.ts does. Sending the query and trusting the server
        gives back the row the user asked to hide.
        """
        movies = self.session.view("Movies")
        layout = [home_sections.LATEST] + [home_sections.NONE] * 9

        # By `parent_id`, never by title: the title is a translated string
        # and every library's is a substring of some other's ("Latest
        # Movies" against "Latest Bulk Movies"), which is a match that
        # passes whatever the exclusion did. The id rides along on the row
        # for exactly this reason.
        before = self.source.get_home_rows(
            UUID, sections=("latest",), layout=layout,
            latest_excludes=frozenset())
        self.assertIn(
            movies["Id"], [row.get("parent_id") for row in before],
            "no Recently Added row for %r to begin with, so hiding it "
            "proves nothing" % movies["Name"])

        self._exclude(movies["Id"])
        after = self.source.get_home_rows(
            UUID, sections=("latest",), layout=layout,
            latest_excludes=self.source.get_latest_excludes(UUID))
        self.assertNotIn(
            movies["Id"], [row.get("parent_id") for row in after],
            "a library excluded from the home screen still got a Recently "
            "Added row")

    def _resume_titles(self):
        rows = self.source.get_home_rows(
            UUID, sections=("primary",),
            layout=[home_sections.RESUME] + [home_sections.NONE] * 9,
            latest_excludes=frozenset())
        return [item["Name"] for row in rows for item in row["items"]]

    def test_an_excluded_library_drops_out_of_continue_watching(self):
        """#703, asserted on the row rather than on the request.

        The predecessor of this test spied on the outgoing kwargs and checked
        for a ParentId, and it passed for the whole life of the bug: the
        parameter really was absent, and the exclusion still did not apply,
        because ``api.get_resume_items`` sends
        ``Users/{uid}/Items?Filters=IsResumable`` and the exclusion lives in
        ``ItemsController.GetResumeItems``. Which endpoint is asked is not
        visible from the parameters, so only the answer can settle it.
        """
        movies = self.session.view("Movies")
        film = self.session.find_all(parent_id=movies["Id"],
                                     item_type="Movie")[0]
        self.session.api.item_played(film["Id"], False)
        self.session.api.update_userdata_for_item(
            film["Id"], {"PlaybackPositionTicks": 60 * 10 ** 7,
                         "Played": False})
        self.addCleanup(self.session.reset_played, film["Id"])

        self.assertIn(film["Name"], self._resume_titles(),
                      "%r is not in Continue Watching to begin with, so "
                      "hiding its library proves nothing" % film["Name"])

        self._exclude(movies["Id"])
        self.assertNotIn(
            film["Name"], self._resume_titles(),
            "a library excluded from the home screen still contributed to "
            "Continue Watching")


if __name__ == "__main__":
    unittest.main()
