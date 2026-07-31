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
The mpv module here is a copy of the one there; the difference is the app
module:

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
