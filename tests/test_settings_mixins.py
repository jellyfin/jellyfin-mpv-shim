"""``SettingsMixin`` is composed from one mixin per tab; composition has rules.

Splitting a 1,217-line class into six modules moves a whole category of
mistake from "impossible" to "silent": two mixins defining the same name no
longer collide at edit time, they collide at MRO-resolution time and the
loser simply never runs. Same failure the gateway split has a test for
(``tests/test_gateway_mixins.py``), same shape of test.

The member list is pinned from the class as it stood before the split, so a
method that gets dropped or renamed during a later reshuffle has to be
acknowledged here rather than quietly disappearing from Settings.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.settings import (  # noqa: E402
    SettingsMixin,
    base,
    downloads,
    general,
    home,
    logs,
    servers,
)

#: Every public member of SettingsMixin immediately before the split.
#:
#: Two acknowledged renames since (this list is a ratchet, not a fossil):
#: ``_settings_general`` -> ``_settings_form``, when the General page was
#: split into General/Browse/Playback tabs and one renderer started drawing
#: all three; and ``_settings_display``, which never appeared here because
#: the Display tab post-dated the split and has since been folded into the
#: Home Screen tab.
BEFORE_SPLIT = {
    "DL_POLL_SECS", "INDENT", "LOG_POLL_SECS", "LOG_ROW_H", "ROUTES",
    "SETTINGS_TABS", "_add_user", "_after_users_changed",
    "_apply_audio_settings", "_apply_work_offline", "_auto_dl_on",
    "_auto_dl_scope_name", "_auto_dl_servers", "_config", "_copy_logs",
    "_delete_download", "_delete_user", "_dl_delete_cb", "_dl_group",
    "_dl_group_item_ids", "_dl_item_row", "_dl_key", "_dl_row",
    "_dl_toggle", "_dynamic_note", "_invalidate_home", "_load_downloads",
    "_load_home_layout", "_move_downloads", "_open_config_folder",
    "_open_rename_user", "_open_settings", "_poll_downloads", "_poll_logs",
    "_remove_server", "_render_settings", "_retry_home_layout", "_section",
    "_seed_auto_download_server", "_server_row", "_set_home_slot",
    "_set_setting", "_set_settings_tab", "_setting_row",
    "_settings_downloads", "_settings_form", "_settings_home",
    "_settings_logs", "_settings_servers", "_toggle_advanced",
    "_toggle_auto_server", "_toggle_collections", "_user_row",
    "open_settings",
}

TABS = {
    "SettingsBase": base.SettingsBase,
    "GeneralTabMixin": general.GeneralTabMixin,
    "HomeTabMixin": home.HomeTabMixin,
    "ServersTabMixin": servers.ServersTabMixin,
    "DownloadsTabMixin": downloads.DownloadsTabMixin,
    "LogsTabMixin": logs.LogsTabMixin,
}


def own_members(cls):
    return {n for n in vars(cls) if not n.startswith("__")}


class TestTheTabsDoNotCollide(unittest.TestCase):
    def test_no_name_is_defined_by_two_mixins(self):
        seen = {}
        collisions = []
        for name, cls in TABS.items():
            for member in own_members(cls):
                if member in seen:
                    collisions.append("%s: %s and %s" % (member, seen[member], name))
                seen[member] = name
        self.assertEqual(collisions, [],
                         "two settings mixins define the same name; the one "
                         "later in the MRO silently never runs")

    def test_each_mixin_actually_contributes_something(self):
        # A mixin that has been emptied by a refactor should be deleted, not
        # left in the bases where it reads as structure that still exists.
        for name, cls in TABS.items():
            self.assertTrue(own_members(cls), "%s defines nothing" % name)


class TestNothingWasLostInTheSplit(unittest.TestCase):
    def test_every_pre_split_member_still_resolves(self):
        have = {n for n in dir(SettingsMixin) if not n.startswith("__")}
        self.assertEqual(BEFORE_SPLIT - have, set(),
                         "these were on SettingsMixin before the split and "
                         "are now unreachable")

    def test_the_tabs_cover_the_whole_class(self):
        """Every pre-split member comes from one of the tab mixins or the
        frame — nothing is orphaned onto some other base."""
        from_tabs = set()
        for cls in TABS.values():
            from_tabs |= own_members(cls)
        frame = own_members(SettingsMixin)
        self.assertEqual(BEFORE_SPLIT - from_tabs - frame, set())


class TestTheRouteStillPointsAtTheFrame(unittest.TestCase):
    def test_settings_renders_through_render_settings(self):
        self.assertEqual(SettingsMixin.ROUTES["settings"], (None, "_render_settings"))

    def test_every_tab_in_the_bar_has_a_renderer(self):
        """SETTINGS_TABS drives the tab bar by name; a tab whose renderer is
        missing is a dead tab that raises when clicked.

        Via TAB_RENDERERS rather than by guessing ``_settings_<tab>``: three
        tabs share one renderer since the General page was split, so the
        naming convention stopped being able to answer this.
        """
        for entry in SettingsMixin.SETTINGS_TABS:
            key = entry[0] if isinstance(entry, (tuple, list)) else entry
            name = SettingsMixin.TAB_RENDERERS.get(key)
            self.assertIsNotNone(
                name, "tab %r is in the bar with no entry in TAB_RENDERERS"
                % key)
            self.assertTrue(hasattr(SettingsMixin, name),
                            "tab %r maps to %s, which does not exist"
                            % (key, name))

    def test_no_renderer_is_mapped_for_a_tab_nobody_can_reach(self):
        # The other direction: a tab dropped from the bar but left in the
        # table is dead code that reads as a screen that still exists.
        self.assertEqual(
            set(SettingsMixin.TAB_RENDERERS) - set(SettingsMixin.SETTINGS_TABS),
            set())


if __name__ == "__main__":
    unittest.main()
