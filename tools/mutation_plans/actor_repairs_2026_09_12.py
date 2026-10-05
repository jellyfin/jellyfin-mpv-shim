"""Mutations for the actor/ownership repairs of 2026-09-12.

Two repairs and their tests, from the 2026-09-12 review round:

* **A** — the acting person is never read off a catalog row. A row's login
  names whoever *downloaded* the copy, and it was tried before the server,
  so on a shared machine the downloader won over the person at the keyboard.
* **B** — watched state must not outlive the row it belongs to.

    xvfb-run -a python3 tools/mutate_round.py \\
        tools/mutation_plans/actor_repairs_2026_09_12.py

Run `--dry-run` first after any refactor of these files: a pattern that no
longer matches is reported rather than skipped, because a mutation that
changes nothing looks exactly like one that survived.

The three findings this round closed all reached review past a **test
double** that answered with a real person where production answered with
nobody. So several entries below break a repair that only a re-derived
double can see fail, and the doubles are named in SELECT for that reason,
not only the modules that read obviously relevant.
"""

SELECT = [
    # The end-to-end module written for this round: real catalog, real
    # gateway, real resolver, two local profiles.
    '-k', 'test_offline_actor_e2e',
    # The resolver's own ordering and the disagreement check.
    '-k', 'test_catalog_actor_scope',
    # The fan-out, and the three doubles that had to be re-derived.
    '-k', 'test_ui_review_fixes',
    '-k', 'test_player_controller',
    '-k', 'test_playlist_offline',
    '-k', 'test_sync_manager',
    '-k', 'test_playstate_mirror',
    # The sweep's routing, the pull's retreat and the reap ordering.
    '-k', 'test_reap_after_sweep',
    '-k', 'test_auto_download',
]


MUTATIONS = [
    # -- A: the resolver ------------------------------------------------
    ('actor_of: the server is tried before the person doing the thing',
     'jellyfin_mpv_shim/sync/manager.py',
     '            if server_id and user_id:\n                return (server_id, user_id)\n            if acting_login:',
     '            if server_id and user_id:\n                return (server_id, user_id)\n            if server_id:\n                found = userManager.actor_on(server_id)\n                if found:\n                    return (server_id, found)\n            if acting_login:'),
    # Inverted 2026-09-12: the old spelling of this mutation turned the
    # correct code into what is now the correct code. The substitution it
    # used to defend was the previous round's repair, and it is the defect --
    # a login on another server was replaced by the active profile's account
    # on the ROW's server, which matches the row, so the store accepted it.
    ("actor_of: the substitution comes back, so a mismatch matches the row",
     'jellyfin_mpv_shim/sync/manager.py',
     '                if actor:\n                    # **As itself, even when it is on another server.**',
     '                if actor and (not server_id or actor[0] == server_id):\n                    # **As itself, even when it is on another server.**'),

    # -- C: the predicate itself, which had no killer at all ------------
    # The diagnosis measured that none of this plan's mutations touched the
    # store's server lookup or the userdata key -- the single rule the
    # riskiest repair rested on. These are that gap.
    ('filing: an item we hold no row for is treated as an orphan again',
     'jellyfin_mpv_shim/sync/db.py',
     '        if row is None:\n            return "no_row", None',
     '        if row is None:\n            return "local", (NO_ACTOR, NO_ACTOR)'),
    ('filing: another server\'s row accepts the write again',
     'jellyfin_mpv_shim/sync/db.py',
     '    if actor_server and actor_server != NO_ACTOR and actor_server != row_server:\n        return "refuse", None',
     '    if actor_server and actor_server != NO_ACTOR and actor_server != row_server:\n        return "sync", (row_server, user_id)'),
    ('filing: an orphan keeps the acting person on the other half',
     'jellyfin_mpv_shim/sync/db.py',
     '        # An orphan: no server, so no account, so one machine-wide bucket.\n        return "local", (NO_ACTOR, NO_ACTOR)',
     '        # An orphan: no server, so no account, so one machine-wide bucket.\n        return "local", (NO_ACTOR, user_id or NO_ACTOR)'),

    # -- D: the identity claim, which decides what a copy IS ------------
    ('claim: missing evidence is read as agreement',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if want is not None and have is not None and want == have:',
     '        if want is None or have is None or want == have:'),
    ('claim: an orphan is re-homed on the strength of nothing',
     'jellyfin_mpv_shim/sync/manager.py',
     '        want = self._declared_bytes((item.get("MediaSources") or [{}])[0])\n        have = self._held_bytes(row)',
     '        want = have = None'),
    ('claim: adoption goes back to taking whatever row it finds',
     'jellyfin_mpv_shim/sync/manager.py',
     '            if verdict in ("ours", "refused", "busy"):',
     '            if False:'),
    ('claim: the door reaps before the request has decided it wants the item',
     'jellyfin_mpv_shim/sync/manager.py',
     '            verdict = self.claim_identity(iid, item, may_reap=False)',
     '            verdict = self.claim_identity(iid, item)'),
    ('claim: a reap deletes the directory a worker is writing into',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if item_id in self._active_ids():',
     '        if False:'),
    ('claim: a refused homing still answers rehomed',
     'jellyfin_mpv_shim/sync/manager.py',
     '            if not self._home_row(item_id, content_id):',
     '            if self._home_row(item_id, content_id) and False:'),
    # **R32 turned the carry into a drop** (2026-09-20), so the first two
    # claims are the same shape with the opposite sign: the statement is
    # missing at one homing site and the orphan bucket is left stranded
    # under a key nothing reads. The third claim these replaced -- that a
    # collision handed the merge to the orphan bucket -- no longer has any
    # code to mutate, because there is no merge; what replaces it is the
    # over-broad delete, which is the way this method can now be wrong.
    ('home: the row learns its server and the orphan bucket stays behind',
     'jellyfin_mpv_shim/sync/db.py',
     '                self._drop_local_userdata(item_id)\n'
     '                self._conn.commit()',
     '                self._conn.commit()'),
    # The second homing site, which is where the N5 defect actually was:
    # `home_content_server` carried the statement and the migration's
    # backfill -- older, and run on every open -- did not.
    ('home: the migration homes the row and leaves the bucket behind',
     'jellyfin_mpv_shim/sync/db.py',
     '            for _server_id, item_id in filled:\n'
     '                self._drop_local_userdata(item_id)',
     '            for _server_id, item_id in filled:\n'
     '                pass'),
    ('home: the drop takes every actor with it, not the anonymous bucket',
     'jellyfin_mpv_shim/sync/db.py',
     '            "DELETE FROM item_userdata "\n'
     '            "WHERE item_id=? AND server_id=? AND user_id=?",\n'
     '            (item_id, NO_ACTOR, NO_ACTOR))',
     '            "DELETE FROM item_userdata WHERE item_id=?",\n'
     '            (item_id,))'),
    ('home: an empty server id is accepted, so a rolled back',
     'jellyfin_mpv_shim/sync/db.py',
     '            if not server_id:',
     '            if server_id is False:'),

    # -- A: the four sites that must not infer a person from a row ------
    # Restoring `acting_login=server_uuid or target_server` at the two
    # queueing sites was tried as a mutation and SURVIVED -- correctly, and
    # the reason is the repair rather than a weak test. `target_server` is a
    # Jellyfin ServerId now, and a ServerId matches no saved login's uuid, so
    # `actor_for` finds nothing and the call falls through to exactly where
    # it would have gone anyway. Measured. The mutation that carries the
    # defect is therefore the one below, which puts a *login* back in that
    # position; both fallbacks become live again the moment it does.
    ('watched_targets: answers with the row\'s login, so the downloader wins',
     'jellyfin_mpv_shim/sync/db.py',
     '        return [(row["item_id"], row["content_server_id"])\n                for row in self._query(\n                    "SELECT item_id, content_server_id FROM downloads "',
     '        return [(row["item_id"], row["server_uuid"])\n                for row in self._query(\n                    "SELECT item_id, server_uuid FROM downloads "'),
    # SURVIVES since 2026-09-12 batch 2, and the reason is the repair rather
    # than a weak test -- measured, not assumed. Both fan-out mutations below
    # now have no observable effect:
    #   * ONLINE, an unscoped fan-out returns the other server's rows, but
    #     each write is then refused by `_row_sync_state` (the acting login
    #     answers as its own server, which does not match that row), so the
    #     database, the `moved` count and the change notification are all
    #     identical.
    #   * OFFLINE, the scope is already None -- the browser browses a
    #     pseudo-server -- so the mutation changes nothing at all.
    # Left in place rather than deleted: they are the killers again the day
    # the store stops refusing, which is the direction this could regress.
    # The one thing neither version protects is an offline mark on a SERIES
    # id shared by two servers, which is the offline source's grouping rule
    # and not the per-actor filing rule's.
    ('mirror_watched: the online fan-out is asked unscoped',
     'jellyfin_mpv_shim/sync/manager.py',
     '            targets = db.watched_targets(\n                item_id, server_id=self.content_id_for(server_uuid))',
     '            targets = db.watched_targets(item_id)'),
    ('playback: the downloader is the viewer again',
     'jellyfin_mpv_shim/sync/offline_media.py',
     '            from ..clients import clientManager\n            return clientManager.uuid_for_client(self.client)',
     '            return syncManager.db.get(self.item_id).get("server_uuid")'),
    ('reading position: the cursor is filed without the row\'s server',
     'jellyfin_mpv_shim/mpvtk_browser/gateway/userdata.py',
     '                actor = syncManager.actor_of(acting_login=server_uuid,\n                                             server_id=db.owner_of(item_id))\n                db.set_reading_position(item_id, int(ticks),',
     '                actor = syncManager.actor_of(acting_login=server_uuid)\n                db.set_reading_position(item_id, int(ticks),'),
    ('reading position: and the queued half, which resolves separately',
     'jellyfin_mpv_shim/mpvtk_browser/gateway/userdata.py',
     '                actor = syncManager.actor_of(acting_login=server_uuid,\n                                             server_id=db.owner_of(item_id))\n                db.upsert_playstate(item_id, actor=actor,',
     '                actor = syncManager.actor_of(acting_login=server_uuid)\n                db.upsert_playstate(item_id, actor=actor,'),

    # -- A: the scope ---------------------------------------------------
    ('watched_targets: unscoped again, so a mark crosses servers',
     'jellyfin_mpv_shim/sync/db.py',
     '        clause, params = self._content_clause(\n            server_id, (STATUS_COMPLETE, item_id, item_id, item_id))',
     '        clause, params = self._content_clause(\n            None, (STATUS_COMPLETE, item_id, item_id, item_id))'),
    ('queue: the fan-out is asked without the login\'s content id',
     'jellyfin_mpv_shim/mpvtk_browser/gateway/userdata.py',
     '            targets = db.watched_targets(\n                item_id, server_id=syncManager.content_id_for(server_uuid))',
     '            targets = db.watched_targets(item_id)'),

    # -- E: the pull's retreat, and the gate that is not "any queued row" --
    ('retreat: the undelivered mark stops holding it back',
     'jellyfin_mpv_shim/sync/db.py',
     '"WHERE item_id=? AND server_id=? AND user_id=? AND played=1",',
     '"WHERE item_id=? AND server_id=? AND user_id=? AND played=1 AND 0",'),
    ('retreat: any queued row blocks it, which is the overbroad reading',
     'jellyfin_mpv_shim/sync/db.py',
     '"WHERE item_id=? AND server_id=? AND user_id=? AND played=1",',
     '"WHERE item_id=? AND server_id=? AND user_id=?",'),
    ('retreat: the sweep goes back to advance-only',
     'jellyfin_mpv_shim/sync/manager.py',
     '                                allow_retreat=True):',
     '                                allow_retreat=False):'),

    # -- F: the sweep's routing, which is the two-door case ----------------
    ('sweep: grouped by the login on the row again',
     'jellyfin_mpv_shim/sync/manager.py',
     '            by_server.setdefault(row["content_server_id"], []).append(',
     '            by_server.setdefault(row.get("server_uuid") or\n'
     '                                 row["content_server_id"], []).append('),
    ('sweep: a half-refreshed account counts as answered',
     'jellyfin_mpv_shim/sync/manager.py',
     '                    answered.discard(actor)\n                    break',
     '                    break'),

    # -- G: the reap ordering ----------------------------------
    ('reap: the hold is gone, so a failed sweep reaps stale',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if self._sweep_owed(now):\n            return row',
     '        if False:\n            return row'),
    ('reap: it holds even with nobody to ask, so offline never reaps',
     'jellyfin_mpv_shim/sync/manager.py',
     '        owed = {self.actor_of(acting_login=routes[content_id][0])\n'
     '                for content_id in wanted if content_id in routes}',
     '        owed = {(content_id, "nobody") for content_id in wanted}'),
    ('reap: the bound never expires',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if now >= self._reap_hold_until:\n'
     '            self._reap_hold_until = None\n'
     '            log.info("Auto-download: reaping without a fresh sweep',
     '        if False:\n'
     '            self._reap_hold_until = None\n'
     '            log.info("Auto-download: reaping without a fresh sweep'),
    ('reap: nothing asks for the sweep, so hours in the flag is down',
     'jellyfin_mpv_shim/sync/manager.py',
     '        self._sweep_due = True\n        self._sweep_if_due(now)\n'
     '        if self._sweep_owed(now):',
     '        self._sweep_if_due(now)\n'
     '        if self._sweep_owed(now):'),

    # -- H: the cap at exactly full, and the profile switch ------------------------------
    ('cap: exactly full stops evicting, so a capped store stalls for a day',
     'jellyfin_mpv_shim/sync/auto.py',
     '                if size < cap or self._interrupted():',
     '                if size <= cap or self._interrupted():'),
    # Two entries stood here for the profile switch: "nothing schedules a
    # sweep for the new profile" and "the new profile inherits the old one's
    # answered servers". D1 deleted the code both named --
    # `request_profile_sweep` and its call in `clients.switch_user` -- so
    # neither claim has a site to break any more: the switch is a reconnect
    # like any other, and the answered set is keyed on the account, so the
    # inheritance is unrepresentable rather than prevented. What replaces
    # them is the single site that still decides it.
    ("switch: the answered set goes back to being keyed on the server, so "
     "one profile's answer counts for the next",
     'jellyfin_mpv_shim/sync/manager.py',
     '            answered.add(actor)',
     '            answered.add(content_id)'),

    # -- B: watched state must not outlive its row ----------------------
    ('delete: the row goes and its watched state stays',
     'jellyfin_mpv_shim/sync/db.py',
     '                self._conn.execute("DELETE FROM item_userdata WHERE item_id=?",\n                                   (item_id,))\n                self._conn.commit()\n            except sqlite3.Error:',
     '                self._conn.commit()\n            except sqlite3.Error:'),
    ('reap: the same, at the site that does not route through delete',
     'jellyfin_mpv_shim/sync/db.py',
     '                self._conn.execute("DELETE FROM item_userdata WHERE item_id=?",\n                                   (item_id,))\n                self._conn.commit()\n                return row',
     '                self._conn.commit()\n                return row'),
]
