# Large run logs — summaries only

The three raw logs below totalled ~1 MB and were NOT committed. Their result
lines are preserved here; regenerate with the commands in the review docs.

## `jms-release-unit-review.log` (460K)
```
    from . import deps
  File "/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/mpvtk_browser/gateway/deps.py", line 18, in <module>
    from ...clients import clientManager       # noqa: F401  (rebound by tests)
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/izzie/bookmarks/scripts/jellyfin-mpv-shim/jellyfin_mpv_shim/clients.py", line 1, in <module>
    from jellyfin_apiclient_python import JellyfinClient
ModuleNotFoundError: No module named 'jellyfin_apiclient_python'

======================================================================
ERROR: test_the_form_is_still_shown (test_user_policy.TheTimerEditor.test_the_form_is_still_shown)
Read-only, not withheld: the settings are the reason to open it.
----------------------------------------------------------------------

*** killed after 300s (--timeout). It passes alone; suspect contention for the X server or for mpv.

2217 tests in 182 modules, 8 workers, 300.6s wall clock
slowest: test_user_policy.py 300.0s, test_imageutil.py 20.0s, test_sync_manager.py 11.5s, test_auto_download.py 11.0s, test_child_process_teardown.py 9.8s
FAILED modules: test_action_rows.py test_async_runner.py test_audio_settings.py test_background_setting.py test_banner_placeholder.py test_browser_features.py test_caption_band.py test_carousel_heading_alignment.py test_close_to_tray.py test_colorspace_hint.py test_credentials_save.py test_dpi_matrix.py test_duplicate_tile_ids.py test_gamepad.py test_gateway_mixins.py test_grid_artwork.py test_home_rows.py test_imageutil.py test_ipc_teardown_timeout.py test_items_api.py test_key_claims.py test_keysweep.py test_kiosk_fullscreen.py test_last_server_and_load_ui.py test_late_bound_calls.py test_library_queries.py test_list_page.py test_live_tv.py test_live_tv_home.py test_log_sanitization.py test_lua_fallback.py test_lua_only_options.py test_mpv_observe.py test_mpv_options.py test_mpv_stat_properties.py test_mpvtk_browser_mixins.py test_mpvtk_cast.py test_mpvtk_daemons.py test_mpvtk_fake_conformance.py test_mpvtk_headless.py test_mpvtk_ui_wiring.py test_mpvtk_virtualized_scrolls.py test_offline_books.py test_offline_seasons.py test_osc_fallback_persists.py test_osc_third_party.py test_page_contract.py test_page_margins.py test_parallel_connect.py test_photos.py test_picture_view.py test_play_all.py test_play_prev_lookup.py test_playback_failure.py test_player_controller.py test_player_locking.py test_playlist_edit.py test_playlist_offline.py test_playstate_mirror.py test_playstate_payload.py test_reading_position.py test_remote_menu_commands.py test_remote_playback.py test_remote_seek.py test_restart.py test_scene_snapshots.py test_season_headings.py test_settings_nullable.py test_shell_books.py test_shell_chrome.py test_shell_comic.py test_shell_delete_item.py test_shell_downloads.py test_shell_library.py test_shell_media_info.py test_shell_music.py test_shell_paging.py test_shell_playback.py test_shell_playlists.py test_shell_provider_links.py test_shell_reader.py test_shell_routing.py test_shell_settings.py test_source_invariants.py test_sync_queue_and_offline.py test_syncplay_disable.py test_syncplay_e2e.py test_syncplay_pause_ignore.py test_syncplay_player_contract.py test_syncplay_protocol.py test_syncplay_release_on_stop.py test_themes.py test_thumbnail_cache.py test_tile_play_chip.py test_track_picker_labels.py test_transparent_logos.py test_ui_review_fixes.py test_user_policy.py test_view_prefs.py test_window_controls.py test_window_geometry.py test_window_identity.py
```

## `jms-release-integration-review.log` (140K)
```
      after 16 requests (16 known processed) with 0 events remaining.
ERROR
test_an_item_past_the_fold_can_be_selected (tests.integration.test_mpvtk_browser.TestLongDropdownScroll.test_an_item_past_the_fold_can_be_selected)
Item 60 is well below the window; selecting it is the whole ... FAIL
test_hover_is_blocked_under_an_open_popup (tests.integration.test_mpvtk_browser.TestLongDropdownScroll.test_hover_is_blocked_under_an_open_popup)
A popup floats over the page and eats the click, so the page ... FAIL
test_the_popup_is_clamped_to_the_window (tests.integration.test_mpvtk_browser.TestLongDropdownScroll.test_the_popup_is_clamped_to_the_window)
The drawn popup must fit on screen. Unclamped, 80 entries drew ... FAIL
test_the_scrollbar_thumb_can_be_dragged (tests.integration.test_mpvtk_browser.TestLongDropdownScroll.test_the_scrollbar_thumb_can_be_dragged) ... FAIL
test_click_navigates_into_a_library (tests.integration.test_mpvtk_browser.TestMpvtkBrowserOnRealMpv.test_click_navigates_into_a_library) ... FAIL
test_renders_home_in_real_window (tests.integration.test_mpvtk_browser.TestMpvtkBrowserOnRealMpv.test_renders_home_in_real_window) ... FAIL
test_the_ui_takes_mpvs_own_window_dragging (tests.integration.test_mpvtk_browser.TestMpvtkBrowserOnRealMpv.test_the_ui_takes_mpvs_own_window_dragging)
The renderer's half of the client-side title bar, against a real ... FAIL
test_a_real_click_reaches_the_button (tests.integration.test_mpvtk_browser.TestRealMousePosPath.test_a_real_click_reaches_the_button)
A press and a release through mpv's own input stack, section ... FAIL
test_the_real_pointer_hovers_leaves_and_comes_back (tests.integration.test_mpvtk_browser.TestRealMousePosPath.test_the_real_pointer_hovers_leaves_and_comes_back) ... FAIL
test_left_click_still_activates (tests.integration.test_mpvtk_browser.TestTableRowContextMenu.test_left_click_still_activates) ... FAIL
Terminated
```

## `jms-release-integration-isolated.log` (376K)
```
Ran 324 tests in 151.331s

OK (skipped=2)
/usr/lib/python3.13/subprocess.py:1140: ResourceWarning: subprocess 3529489 is still running

========================================================================
INTEGRATION MATRIX SUMMARY
========================================================================
  test_clients_concurrency/test_sync_manager_races/test_syncplay_generation/test_single_instance_multiproc PASS  [39 run, 0 skipped]
  test_harness_isolation/test_restart_relaunch         PASS  [9 run, 0 skipped]
  test_player_state_machine/test_playback_start/test_keyboard_controls/test_lifecycle/test_mpv_lifecycle [libmpv] PASS  [143 run, 1 skipped]
  test_realmpv_picture/test_settings_screen/test_realmpv_smoke/test_mpvtk_browser/test_mpvtk_hud/test_mpvtk_auth/test_e2e_offline/test_window_decorations [libmpv] PASS  [82 run, 1 skipped]
  test_player_state_machine/test_playback_start/test_keyboard_controls/test_lifecycle/test_mpv_lifecycle [jsonipc] PASS  [143 run, 1 skipped]
  test_realmpv_picture/test_settings_screen/test_realmpv_smoke/test_mpvtk_browser/test_mpvtk_hud/test_mpvtk_auth/test_e2e_offline/test_window_decorations [jsonipc] PASS  [82 run, 1 skipped]
  whole suite [libmpv]                                 PASS  [322 run, 2 skipped]
  whole suite [jsonipc]                                PASS  [322 run, 2 skipped]
========================================================================
All legs passed.
```

