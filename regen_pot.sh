#!/bin/bash
#
# Regenerate the gettext template (base.pot) and merge it into every locale's
# base.po -- while making sure translation work living on the master branch is
# folded in first, so volunteer/Weblate translations are never lost when this
# runs on a feature branch.
#
# How it works, per locale:
#   1. Regenerate base.pot from the current source.
#   2. Take master's base.po as the authoritative translation source (that is
#      where Weblate lands, so it holds the freshest volunteer work).
#   3. Use the working-tree base.po as a *compendium* -- it only fills in
#      translations master doesn't already have (e.g. new strings added on this
#      branch that master has never seen).
#   4. msgmerge the combined translations against the freshly generated
#      base.pot and write the result back into the working tree.
#
# So for a given message: master's translation wins if present, otherwise the
# working-tree translation is used, otherwise the string is left untranslated
# for a translator to pick up. Nothing translated on either side is dropped.
#
# The master ref is configurable in case you want to merge against a fetched
# remote instead of your local branch:
#   MASTER_REF=origin/master ./regen_pot_merge_master.sh
# Run `git fetch` first if you want the very latest volunteer work from remote.

set -euo pipefail

MASTER_REF="${MASTER_REF:-master}"
POT="jellyfin_mpv_shim/messages/base.pot"

cd "$(dirname "$0")"

if ! git rev-parse --verify --quiet "$MASTER_REF" >/dev/null; then
    echo "error: git ref '$MASTER_REF' not found (set MASTER_REF to override)" >&2
    exit 1
fi

echo "Regenerating $POT from source..."
pygettext3 --default-domain=base -o "$POT" jellyfin_mpv_shim/*.py jellyfin_mpv_shim/**/*.py

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# Union of .po paths present in the working tree and on master, so locales that
# only exist on one side (e.g. a new Weblate language on master, or a new one
# added on this branch) are all handled.
{
    find jellyfin_mpv_shim/messages -iname '*.po'
    git ls-tree -r --name-only "$MASTER_REF" -- jellyfin_mpv_shim/messages \
        | grep -i '\.po$'
} | sort -u | while read -r po; do
    master_po="$tmpdir/master.po"
    have_master=0
    if git cat-file -e "$MASTER_REF:$po" 2>/dev/null; then
        git show "$MASTER_REF:$po" > "$master_po"
        have_master=1
    fi

    have_working=0
    [ -f "$po" ] && have_working=1

    if [ "$have_master" -eq 1 ] && [ "$have_working" -eq 1 ]; then
        # master translations win; working tree fills gaps master lacks.
        echo "merging (master + local): $po"
        msgmerge --quiet --previous \
            --compendium "$po" "$master_po" "$POT" -o "$po"
    elif [ "$have_master" -eq 1 ]; then
        # locale only on master -> pull it into the working tree.
        echo "adding from master: $po"
        mkdir -p "$(dirname "$po")"
        msgmerge --quiet --previous "$master_po" "$POT" -o "$po"
    else
        # locale only in working tree -> just refresh against the new template.
        echo "local only: $po"
        msgmerge --quiet --previous "$po" "$POT" -o "$po"
    fi
done

echo "Done. Review 'git diff' before committing."
