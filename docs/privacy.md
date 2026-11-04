# JellyHome Player: network behaviour and privacy

This describes the code in this repository. It does not promise "zero third-party
traffic": media, artwork and subtitle URLs come from *your* Jellyfin server, and the
features you switch on (Discord, update checks) talk to the services they name.

## 1. Every connection the application itself makes

| # | Destination | Trigger | What is sent | Controls |
|---|---|---|---|---|
| 1 | **Your Jellyfin server(s)** (address you typed) | Sign-in, Quick Connect, browsing, playback, websocket for casting, health checks, downloads | Your credentials/token as Jellyfin requires, device name and a per-install device id, playback progress, API queries | Remove the server; "Work offline" |
| 2 | Artwork / subtitle / trickplay URLs returned by your server | Showing tiles, playback | A plain GET; the access token is added only to URLs on the **same origin** as your server (`utils.same_origin`, `docs/auth-headers.md`) | Same as above |
| 3 | `github.com/braisravina/jellyhome-player/releases/latest` | **Only** (a) when you press **Check for Updates Now**, or (b) once a day *after you opt in* to "Check for Updates Automatically" | A plain HTTPS GET with no custom headers, params, cookies or body (no account, server address, device id or viewing history); the HTTP library still adds its own default headers, including a `python-requests` User-Agent. Redirects are not followed blindly: only the same GitHub owner is accepted, at most 4 hops / 15 s | Off by default; nothing is requested while off, at startup, during playback or in settings. If `constants.RELEASE_REPO` is empty the button explains and no request is made |
| 4 | Discord (local IPC to your Discord app, not a network call from us) | Only if `discord_presence` is on **and** `pypresence` is installed | Title/progress of what you play, shown by Discord on your profile | Off by default; the setting's note says what is shared |
| 5 | Local SVP API (`svp_url`, default `http://127.0.0.1:9901/`) | Only if you use the SVP menu | Local requests | Config |
| 6 | Anything you add yourself (`mpv.conf` options, user mpv scripts, shell-command hooks) | Your choice | Whatever you configure | Yours |

Removed from upstream: the request to `https://checkip.amazonaws.com/` that learned your public IP
whenever a server name resolved to a public IPv4 address. Nothing replaces it with another service.

One thing that looks like a connection and is not: `clients.get_local_ips()` opens a UDP socket and calls `connect()` to
`8.8.8.8:80` purely so the OS reports which local address it would use. A UDP `connect()` sends no packet. (Reasoned
from socket semantics, not packet-captured.)

One thing that is a lookup, made by your system's resolver rather than by a service this application chooses: while it
connects (start-up and every user switch), `clients.get_local_ips()` asks the resolver for the machine's own hostname
(`getaddrinfo(gethostname())`) to learn which private addresses this machine has, so saved servers on the same subnet are
tried first. Depending on your `/etc/hosts` and resolver configuration that can mean a DNS query for your own machine name
to your configured DNS server (measured: where the name is missing from `/etc/hosts` the answer took about 5 s; the packet
itself was not captured). Only private results are used. The wait is bounded (0.25 s; a slow lookup is abandoned on a
single daemon thread, never one per call), so a slow resolver cannot delay connecting.

One thing that *is* sent, to the local network only: when the sign-in form is shown with no saved server, the app
broadcasts a Jellyfin discovery query (UDP, port 7359, broadcast address) for about a second, in the background, to
list "Servers on your network". Seen in a launched local test build (the log reads `Found Servers: []`); the packet
itself was not captured. Replies are unauthenticated, so they are only offered as addresses to pick -- nothing is
selected or signed into automatically. It does not leave the LAN broadcast domain and involves no outside service.

Build-time only (not the running app): `gen_pkg.sh` downloads the shader pack from
`github.com/iwalton3/default-shader-pack` and pyinstaller's release; CI downloads runners' tools. These
happen when *you build*, not when users run the app. Operating-system or package-manager traffic
(Windows Update, Flatpak remotes, pip) is not initiated by this application.

Listening sockets: a loopback-only TCP socket (127.0.0.1, ephemeral port) and an instance lock file in the
config directory for single-instance activation; and, with the external-mpv backend, an mpv IPC endpoint (a local socket/pipe
named by python-mpv-jsonipc; its permissions were not audited here). The code binds only 127.0.0.1.

## 2. Connection Type (Auto / Local / Remote)

A per-server choice stored with that server's saved login (Settings → Servers). It controls **bandwidth only**:
Remote applies your Maximum Bitrate setting; Local removes the cap.

* **Auto** (default): Local if the server's name resolves to a private, loopback, link-local or unique-local
  address. If it resolves to a *public* address (hairpin NAT, public IPv6), the app asks **your Jellyfin server**
  (`GET /System/Endpoint` → `IsInNetwork`). Anything unknown or failing is **Remote**. Tailscale/CGNAT
  (100.64.0.0/10) is treated as Remote.
* **Local / Remote**: your word, overriding Auto.
* It never changes authentication, certificate checks, or which files may be opened. Direct media paths
  (`direct_paths`) are only enabled for servers whose name resolves **entirely** to private addresses (or if you
  explicitly set `remote_direct_paths`); the *Local* override and the server's own `IsInNetwork` claim do not
  grant that, and *Remote* removes it.

Limits: behind a reverse proxy that Jellyfin does not know (Known Proxies), the server may see the proxy's LAN
address for every client and answer `IsInNetwork: true`, so Auto can call a faraway client Local on a public
hostname. Set that server to **Remote**. Split-horizon DNS works (a settled answer is cached for 5 minutes and the name is re-resolved on every reconnect; an unknown or public-address answer is retried after 20 seconds); VPNs that route to your LAN address look Local. A dual-stack name with one LAN and one public record
counts as Local for bandwidth but is not trusted for direct paths. `/System/Endpoint` behaviour on Jellyfin
10.11.8 was **not tested** here (no server available); if it is refused the result is Remote.

## 3. Credentials, tokens and logs

* Saved logins live in `users.json` (and legacy `cred.json`) in the config directory
  (`%APPDATA%\jellyhome-player`, `~/.config/jellyhome-player`), as **plaintext JSON containing access tokens**.
  The app restricts file permissions where the OS supports it and keeps `.bak` copies with the same restriction.
  Anyone who can read your user profile can use those tokens (until you sign out/revoke the device in Jellyfin).
* A parental/profile PIN is an **access gate in the UI, not encryption**; it does not protect the files.
* `log.txt` is sanitised by default (`sanitize_output`): `api_key`/`ApiKey`, `X-MediaBrowser-Token`,
  `AccessToken`, `Token="…"` and credentials embedded in a URL are redacted (case-insensitively). Review a log
  before sharing it anyway.
* The device id (`client_uuid`) is created per config directory. JellyHome and upstream therefore appear as
  different devices to your server. `jellyhome-player import-upstream` copies preferences only; it drops the
  device id and does **not** copy logins.
* Certificate verification is on unless you set `ignore_ssl_cert`. A server typed without a scheme now defaults to
  `https://` unless it looks like a home-network address (private IP, `localhost`, dotless or `.local`/`.lan`
  names), so a public domain is never silently contacted over plain HTTP.

## 4. Remaining risks

Plaintext token storage; `http://` LAN servers send credentials unencrypted on your LAN; your Jellyfin server can
name URLs that your machine will fetch (artwork) and, if you enable direct paths, files; shell-command hooks in
`conf.json` run with your privileges (they are your configuration); unsigned installers.
