# JellyHome Player security and privacy review

_Status key: Fixed = changed and covered by a test or an inspected artifact; the Inno Setup script, Flatpak build and Windows registry code could **not** be executed here (see section 5)._

Reviewed checkout: branch `master` at `d7b54f8b` (upstream jellyfin-mpv-shim 3.3.0 line, remote `origin` =
`github.com/braisravina/jellyhome-player`). The fixes below are on branch `jellyhome-privacy-fork`, committed as a
series (see `git log`); nothing has been pushed or published. Rows 1 and 2 of the coverage table, and every
"Finding", describe the code **as found** at `d7b54f8b`; the "Status" column says what the branch does now.

This is a review of the code paths named below, not a certification. It does not
show the software is free of vulnerabilities.

## 1. Coverage

| Area | Method | Notes |
|---|---|---|
| `update_check.py`, `is_local_domain` (then in `utils.py`), `clients.py` locality code, `media.py` locality use | Manual read, end to end | Both privacy items live here |
| `conf.py` (settings, migrations), `conffile.py`, `users.py` credential store, `log_utils.py` | Manual read of the relevant parts | Credential files are already permission-restricted (`users._restrict_existing`); logs are redacted by `log_utils.sanitize` |
| Outbound HTTP (`grep` for `requests`, `urllib`, `socket`, `webbrowser`) | Automated search + manual read of each hit | See `docs/privacy.md` |
| Shell hooks (`os.system(settings.*_cmd)`), SVP URL, archive readers (`comic.py`, `epub/`) | Manual spot check | Hook commands are taken from the user's own config and are not interpolated with server data; archive members are read by name, never `extractall` |
| Packaging (the Inno Setup script, since renamed `JellyHome Player.iss`, `flatpak/*.json`, `.desktop`, appdata, `gen_pkg.sh`, `.github/workflows/main.yml`) | Manual read | Identity, privileges, process shutdown |
| `mpvtk/`, `mpvtk_browser/` rendering code, `sync/` internals, `syncplay.py`, translations, vendored shaders | **Not** manually audited | Covered only by the project's existing test suite |
| Python dependencies | `pip install` into a throw-away venv only; no `pip-audit` available offline | See section 4 |

## 2. Findings

Severity is for a default install of this fork. "Confirmed" means reproduced by reading
the code path or by a test.

| ID | Type | Severity | Finding | Evidence | Status |
|---|---|---|---|---|---|
| JH-01 | Confirmed privacy issue | Medium | `is_local_domain` (in `utils.py`) requests `https://checkip.amazonaws.com/` (a third party) from the client to learn the public IP, whenever a server name resolves to a public IPv4 address. It runs inside `Media.__init__`, so it fires on every queue item, on the playback thread. | `utils.py` (`is_local_domain`), `media.py` (`Media.__init__`) | Fixed |
| JH-02 | Confirmed bug (fail-open) | Medium | When locality cannot be determined, `Media.__init__` sets `is_local = True` on any exception, and `client is None` also yields `True`. `is_local` gates `settings.direct_paths` (direct filesystem/SMB paths handed to mpv) and removes the bitrate cap, so an unreachable or unresolvable server was *trusted*. Ambiguity must resolve to Remote. | `media.py` (`Media.__init__`, `Video` direct-path check) | Fixed |
| JH-03 | Confirmed bug | Low | `is_local_domain` only inspected `getaddrinfo(...)[0]`, so a dual-stack name with a LAN IPv4 record listed second was judged by the first record. Blocking DNS on the playback path with no timeout. | `utils.py` | Partly fixed: every address a name resolves to is now considered (Local for bandwidth only if all are private, trusted for direct paths only if entirely private) and the answer is cached (5 min; 20 s when unknown). **Still open:** `getaddrinfo` takes no timeout, so the lookup still blocks its caller, which can be the playback thread (`network_locality` and `utils.resolved_host_is_private` say so in their docstrings). The separate lookup of the machine's own hostname on the connect path is bounded to 0.25 s on one thread |
| JH-04 | Confirmed privacy issue | Medium (policy) | `check_updates` defaults to `True` and contacts `github.com/jellyfin/jellyfin-mpv-shim/releases/latest` on startup and on first play each day. In a fork this tells the upstream project host that this install exists, and would announce *upstream* releases to fork users. | `conf.py`, `update_check.py`, `player.py` (`update_check.check()`), `mpvtk_browser/app.py` (`start_background_work`) | Fixed |
| JH-05 | Confirmed bug | Low | An upstream config (`config_version` ≤ 6) carries an explicit `check_updates: true` that is indistinguishable from the old default; it would silently authorise background checks in the fork. | `conf._migrate` | Fixed (migration 7) |
| JH-06 | Confirmed bug | Low | The fork shares `APP_NAME`, desktop id, Inno `AppId`, Flatpak id with upstream, so config dir, `cred.json`/`users.json`, single-instance lock, installer and uninstaller collide with a parallel upstream install. | `constants.py`, `.iss`, `flatpak/`, `integration/` | Fixed |
| JH-07 | Potential risk (not a defect) | Info | Credentials (`users.json`, `cred.json`) are stored in plaintext JSON with file-mode restrictions. Parental PINs are access gates, not encryption. | `users.py` | Documented in `docs/privacy.md` |
| JH-08 | Potential risk | Info | `svp_integration.list_request` calls `urllib.request.urlopen(settings.svp_url + ...)` which would also accept `file:` URLs. The value comes only from the local config file, which is already a local-administrator capability. | `svp_integration.py` | Not changed; noted |
| JH-09 | Confirmed bug (installer) | Low | The Windows installer defaulted to all-users/UAC prompt and `CloseApplications=force` with filter `*.exe,*.dll` and a generic executable name (`run.exe`): it force-closed whatever held matching files without asking. Now per-user install, `CloseApplications=yes`, filter `JellyHomePlayer.exe,mpv-2.dll`. | `.iss` | Fixed |
| JH-10 | Not applicable | — | `os.system(settings.idle_cmd …)` hooks: commands come from the user's own configuration and receive no server-supplied text. Treated as an expected local-administrator capability. | `timeline.py`, `player.py` | No change |
| JH-11 | Confirmed hardening gap | Low | `.github/workflows/main.yml` had no `permissions:` block, so jobs ran with the repository's default token scope. The jobs only build and upload artifacts. | workflow head | Fixed (`contents: read`) |
| JH-12 | Confirmed bug (cleartext credentials) | Medium | `ClientManager._normalize_server` turned any address typed *without a scheme* into `http://host:8096`, including public domains and IPs, so a password could be sent in the clear without the user choosing it. | `clients.py` (`_normalize_server`) | Fixed: `http://` only for private IPs, `localhost`, dotless and `.local/.lan/...` names; everything else `https://` |
| JH-13 | Confirmed bug (log redaction) | Low | `log_utils` redacted only lowercase-hex values of fixed key spellings: `api_key=0123ABCD` logged as `api_key=REDACTEDABCD`, `apikey=`/non-hex `AccessToken` values and `https://user:pw@host` were not redacted at all. Jellyfin issues lowercase-hex tokens, so real exposure is limited to other-case values and basic-auth URLs. | `log_utils.py` | Fixed (delimiter-based, case-insensitive; URL userinfo) |
| JH-14 | Optional improvement | Info | Appdata screenshots and the old README pointed at a third party's hosted images of a different application. | appdata, README | Removed |

## 3. Things that were checked and found sound

* Credentials are never placed on URLs for another origin: `utils.same_origin` compares scheme, host and port (documented in `docs/auth-headers.md`).
* `log_utils.sanitize` redacts `api_key`, `X-MediaBrowser-Token`, `AccessToken` and `Token="..."` forms.
* The update checker already refuses redirects outside an allow-listed owner, caps hops and time, and requires version-shaped tags.
* Comic/epub readers open archive members by name; there is no `extractall` anywhere.

## 4. Dependencies

First session: `pip install -e '.[all]'` into a fresh venv succeeded (python-mpv, python-mpv-jsonipc, jellyfin-apiclient-python,
requests, pillow, certifi, pystray, pypresence). No online advisory database was consulted in this environment, so
**no dependency vulnerability claim is made**. Run `pip-audit` (or GitHub Dependabot) from a networked machine
before a release.

## 5. Limitations

During the first review session no `mpv`/`libmpv`/`xvfb` were installed and there was no `sudo`, so 56 test modules that
construct the real player could not run (identical before and after the changes). Later sessions used a private,
non-root mpv and Xvfb and a disposable loopback Jellyfin 10.11.8 (see `docs/verification-record.md`), which is how the
test suite and the integration legs were run; that does not change what is *not* covered: no Windows host, no
Flatpak build, no macOS, no real or non-loopback Jellyfin deployment, no packet capture. See
`docs/implementation-summary.md` for the status record.

## 6. Observed after this review

Not new vulnerabilities, but behaviour that bears on the privacy claims and that the first review did not describe.
All are documented in `docs/privacy.md`; none was packet-captured.

* The sign-in form, shown with no saved server, broadcasts a Jellyfin discovery query (UDP port 7359) on the local
  network for about a second. Replies are unauthenticated and are only offered as addresses to pick.
* Connecting asks the system resolver for the machine's own hostname, which depending on `/etc/hosts` and the resolver
  configuration can mean a DNS query for it. The wait is bounded (0.25 s, one lookup thread).
* The update check's request carries no custom headers, but the HTTP library adds its default ones, including a
  `python-requests` User-Agent.
* "Start With Computer" (off by default) writes one per-user startup entry, and `import-upstream` copies preferences
  but never saved logins or the device id.
* The external-mpv backend's IPC endpoint is a local socket whose permissions were not audited.
* Credentials remain plaintext JSON with file-mode restrictions (JH-07); nothing changed there.
