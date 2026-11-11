# Linux package (Debian 13 / amd64)

`build-deb.sh` builds `publish/Linux/jellyhome-player_<version>-1_amd64.deb` from the working tree. It installs
nothing on the build machine: everything happens in a temporary directory. Design notes and the licence story are in
`docs/packaging.md` section 7; what has and has not been verified is in `docs/native-verification.md`.

```sh
linux/build-deb.sh                       # needs dpkg-deb, python3 3.13 with pip + setuptools>=77, network
sha256sum -c publish/Linux/jellyhome-player_*_amd64.deb.sha256
sudo apt install ./publish/Linux/jellyhome-player_3.3.0-1_amd64.deb     # apt resolves the dependencies
```

## Files here

| File | What it is |
|---|---|
| `build-deb.sh` | The build. `--no-shader-pack` skips the optional video profiles. |
| `jellyhome-player` | The launcher installed as `/usr/bin/jellyhome-player` (`#!/usr/bin/python3 -sE`). It finds the application in `/usr/lib/jellyhome-player` relative to itself and exports `JELLYHOME_LAUNCHER`, which is how *Start With Computer* finds it again. |
| `requirements-linux-cp313-amd64.lock` | The bundled Python libraries, hash-pinned (CPython 3.13, manylinux x86-64). |

## What the build does and what it can be told

Environment variables: `OUT` (output directory), `SOURCE_DATE_EPOCH` (default: the HEAD commit time),
`DEB_MAINTAINER` (`"Name <address>"`), and `SHADER_PACK_TAG` together with `SHADER_PACK_TREE_SHA256`.

* **Shader pack.** Pinned in the script to a release tag *and* the SHA-256 of its extracted tree; the build fails if
  the download differs. To move to another release, look at it, then change `PINNED_SHADER_PACK_TAG` and
  `PINNED_SHADER_PACK_TREE_SHA256` together (the script prints the hash it computed when they disagree).
* **Python libraries.** Installed with `pip install --require-hashes --no-deps --target`; a different file fails the
  build. The hashes are trust-on-first-use from PyPI (taken 2026-11-05), not a publisher signature.
* **Maintainer.** The default `Maintainer:` is a placeholder and the build says so. A release build sets
  `DEB_MAINTAINER` to a real name and address; none is invented here.
* **Reproducibility.** Same commit, same `SOURCE_DATE_EPOCH`, same shader pin and the same Python / pip / setuptools /
  wheel versions give a byte-identical package (checked: two builds, identical SHA-256). CI pins pip 25.1.1,
  setuptools 78.1.1 and wheel 0.46.1; the manifest next to the package records the versions used, the source commit
  (and whether the packaged paths were dirty), the shader pack tag and the lock file's hash.

## Dependencies, as declared

`Depends: python3 (>= 3.13), python3 (<< 3.14), libmpv2 (>= 0.38), libfribidi0, libc6 (>= 2.34)`. The floor for libmpv is
the oldest the app supports; **Debian 13 provides 0.40**, and the app copes with an older mpv than 0.41 by quitting its
mpv instance when the window is hidden to the tray (the app keeps running as a cast target and opens a new window when
needed). `Recommends` a text font (`fonts-dejavu-core | fonts-noto-core`: without one the app falls back to Pillow's
built-in font), `python3-pystray` (tray), `zenity | kdialog` (start-up error dialog) and `xdg-utils`; `Suggests` CJK and
colour-emoji fonts.

## Updating the lock

1. Choose the new versions (keep Pillow at the tested version unless you have run the suite against the new one).
2. Download the wheels and take their hashes, for example:
   `pip download --no-deps --only-binary=:all: --python-version 3.13 --platform manylinux_2_28_x86_64 --platform any -d /tmp/wheels <name>==<version>`
   then `sha256sum /tmp/wheels/*`.
3. Edit the lock (one `name==version --hash=sha256:...` line each), run `python3 tests/test_desktop_packaging.py`
   (it checks every line is hash-pinned and that every library is named in `THIRD_PARTY_NOTICES.md`), update the
   notices table and the hash date in the lock's header, and rebuild.
4. `tools/collect_licenses.py` fails the build for a library without a licence file or licence metadata.

## What is not in the package

No autostart entry (the app's own opt-in setting writes a per-user one), nothing in any home directory, no credentials
or test state (the script stages sources from `git ls-files` plus an allow-list and fails on a `cred*.json`,
`users.json*`, `conf.json`, `*.db` or `log.txt` in the bundle), and no native library: `libmpv2` and `libfribidi0` come
from Debian.

## Limits

Debian 13 amd64 only; Ubuntu 24.04 (Python 3.12) and Debian 12 should use the Flatpak. The package has been unpacked and
loaded headlessly, never installed with `apt` or played on a real desktop. `lintian` has not been run.
