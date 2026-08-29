# Flatpak (development bundles)

This manifest builds a Flatpak bundle **from the working tree**, for CI and for
testing a branch on a machine that isn't set up to run from source. It is not
what Flathub ships.

## Building

```sh
git clone --depth=1 https://github.com/flathub/shared-modules.git flatpak/shared-modules
./gen_pkg.sh --skip-build   # compiles .mo files, fetches the shader pack
flatpak-builder build flatpak/com.github.iwalton3.jellyfin-mpv-shim.json --force-clean --repo=repo --user
./artifacts.sh flatpak amd64   # -> publish/Flatpak/jellyfin-mpv-shim_v<version>_amd64.flatpak
```

Run the commands from the repository root, not from this directory. Both
`shared-modules` and the build output are gitignored.

`gen_pkg.sh --skip-build` is not strictly required — without it the app still
builds, it just has no translations and no bundled shader pack.

The first build compiles mpv and its dependencies from source and takes a
while; `.flatpak-builder` caches that, and CI caches the directory across runs
keyed on this manifest.

## How this differs from the Flathub manifest

The packaging Flathub publishes lives in
[flathub/com.github.iwalton3.jellyfin-mpv-shim](https://github.com/flathub/com.github.iwalton3.jellyfin-mpv-shim).
It also pins `libdir` in the top-level `build-options`. flatpak-builder only
started passing one to meson and cmake in 1.4.4, and Ubuntu 24.04 — what CI
runs on — ships 1.4.2, where each build system picks for itself and both land
on `lib64`: meson does it on x86_64, cmake on aarch64, and either way the `.pc`
files end up off the pkg-config path.

The mpv module here is a copy of the one there, except that mujs is fetched by
git commit instead of as a tarball — Codeberg regenerates that archive, so the
sha256 the Flathub manifest pins no longer matches what it serves (the tar
stream inside is unchanged; only the gzip wrapper differs). Flathub's own
builds will hit this whenever their download cache misses.

The real difference is the app module:

- **Flathub** installs the *released* `jellyfin-mpv-shim` from PyPI, with every
  wheel and sdist pinned in `pypi-dependencies.json` (generated with
  `flatpak-pip-generator`), because Flathub builds have no network access.
- **This manifest** takes the checkout as a `dir` source and runs
  `pip3 install '.[all]'` with `--share=network`, so dependencies come straight
  from PyPI at whatever version `pyproject.toml` asks for. That keeps the
  manifest short and means a branch that bumps a dependency builds without
  regenerating anything — but it is also exactly why this manifest could not be
  submitted to Flathub as-is.

So a dependency change needs no work here, and still needs
`pypi-dependencies.json` regenerated in the Flathub repo at release time.

## Checking the pins

`tools/check_flatpak_pins.py` downloads every pinned source and verifies its
sha256, and asks `git ls-remote` whether each pinned tag still names the commit
beside it. A cold build otherwise discovers a stale pin one source at a time,
minutes in.

It also flags a git source that pins a `branch` *and* a `commit`, which is the
one shape that fails outright: flatpak-builder verifies that the branch is at
that commit and refuses otherwise, so a commit taken from the history of a
branch that is still moving dies on the next upstream push with
`Git commit for branch master is <head>, but expected <pin>`. That is why the
mpv source here carries a commit and no branch — flatpak-builder then finds no
ref at it and mirrors the whole repo (about 200 MB, cached in
`.flatpak-builder`) rather than shallow-cloning a ref. `tag` + `commit` is
fine, and is what every other source here uses, because a tag does not move.

Point it at the Flathub manifest to check that one too — that is where it earns
its keep, since `pypi-dependencies.json` pins several dozen files:

```sh
./tools/check_flatpak_pins.py
./tools/check_flatpak_pins.py ~/src/flathub-shim/com.github.iwalton3.jellyfin-mpv-shim.json
```
