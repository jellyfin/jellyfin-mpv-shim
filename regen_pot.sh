#!/bin/bash
#
# Regenerate the gettext template (base.pot).
#
# By DEFAULT that is all it does. Pass --merge to also merge the new template
# into every locale's base.po.
#
#   ./regen_pot.sh            # base.pot only -- what feature work wants
#   ./regen_pot.sh --merge    # also rewrite all ~86 base.po files
#
# Why the merge is opt-in: it rewrites every locale, and because msgmerge
# rewrites line references and re-wraps entries it touches, that is ~10k lines
# of churn across 86 files even when no translation has actually changed.
# Meanwhile Weblate is landing real translations on master continuously, so a
# feature branch carrying that churn conflicts with master in files nobody on
# the branch edited -- and the conflicts are unreadable, because the diff is
# almost entirely reference comments.
#
# base.pot alone is what translators actually need from a feature branch: it is
# the template Weblate reads to discover new strings. Filling the .po files in
# is Weblate's job. So: add strings, run this without --merge, commit the .pot.
#
# A --merge run must be committed and merged to master promptly -- within the
# hour, not the week -- or it is stale before it lands. It is a maintainer
# operation, not part of finishing a feature.
#
# How --merge works, per locale:
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
#   MASTER_REF=origin/master ./regen_pot.sh --merge
# Run `git fetch` first if you want the very latest volunteer work from remote.

set -euo pipefail

MASTER_REF="${MASTER_REF:-master}"
POT="jellyfin_mpv_shim/messages/base.pot"
MERGE_PO=0

for arg in "$@"; do
    case "$arg" in
        --merge) MERGE_PO=1 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; $d'
            exit 0 ;;
        *)
            echo "error: unknown argument '$arg' (try --help)" >&2
            exit 1 ;;
    esac
done

cd "$(dirname "$0")"

if [ "$MERGE_PO" -eq 1 ] \
        && ! git rev-parse --verify --quiet "$MASTER_REF" >/dev/null; then
    echo "error: git ref '$MASTER_REF' not found (set MASTER_REF to override)" >&2
    exit 1
fi

echo "Regenerating $POT from source..."
# xgettext, not pygettext3. Three reasons, in order of how much they cost us:
#
# 1. pygettext3 cannot extract msgctxt AT ALL -- it has no --keyword syntax for
#    a context argument, and emits zero contexts for a file full of pgettext
#    calls. So `_p()` (see i18n.py) is unusable without this swap, and `_p` is
#    the only way to translate a word that is a form label in one place and an
#    imperative verb in another ("Record", "Channels", "Download", "None").
# 2. --add-location=file. pygettext3 only offers GNU-vs-Solaris *style*, not
#    granularity, so every reference carried a line number and every string
#    added above another rewrote all of them: 87% of a 17k-line .po sweep was
#    `#:` comments alone. File-only references churn when a string moves
#    between files, which is rare.
# 3. It marks format strings (`#, python-format`) -- 50 of ours -- which lets
#    Weblate check that a translation kept its placeholders. pygettext emitted
#    none, so a translation that dropped a %s failed at runtime instead.
#
# --foreign-user omits the FSF copyright block xgettext otherwise writes into
# a project that is not FSF-assigned. Verified against pygettext3: identical
# msgid set, 677 either way.
#
# find, not a glob. This was `jellyfin_mpv_shim/*.py jellyfin_mpv_shim/**/*.py`,
# and without `shopt -s globstar` bash expands `**` exactly like `*` -- one
# level. So every package nested two deep was silently never scanned:
# mpvtk_browser/pages, /settings, /components and /gateway, which between them
# hold ~180 user-facing strings that could not be translated and gave no sign
# of it. A wrong glob fails by finding less, and nothing downstream knows the
# difference.
#
# Sorted so the template's file order -- and therefore its diff -- is stable
# between runs. __pycache__ holds no .py, but excluding it keeps the intent
# obvious if that ever changes.
mapfile -t SOURCES < <(
    find jellyfin_mpv_shim -name '*.py' -not -path '*/__pycache__/*' | sort)
if [ "${#SOURCES[@]}" -eq 0 ]; then
    echo "error: no Python sources found to scan" >&2
    exit 1
fi
echo "  scanning ${#SOURCES[@]} source files"
xgettext \
    --language=Python \
    --keyword=_ \
    --keyword=_p:1c,2 \
    --from-code=UTF-8 \
    --add-location=file \
    --foreign-user \
    --package-name=jellyfin-mpv-shim \
    -o "$POT.new" "${SOURCES[@]}"

# Carry the hand-written translator context across the regeneration.
#
# The `#.` comments in base.pot are the only context a Weblate translator
# gets -- the string and a filename is everything else they see -- and
# xgettext regenerates the template wholesale, so without this step every
# one of them dies on each run. They are keyed on (msgctxt, msgid), so a
# reworded string DROPS its note and the drop is reported; carrying it
# across a reword is the failure docs/i18n.md section 5 already records.
# Via a third file, so a failure anywhere in here leaves the committed
# template alone rather than truncating it.
python3 tools/pot_context.py "$POT.new" --restore "$POT" -o "$POT.ctx"
mv "$POT.ctx" "$POT"
rm -f "$POT.new"

# Flag the translations that RAISE when the app formats them.
#
# This is the one thing here that writes to the per-locale .po files, and it
# is a bounded exception to the rule above rather than a relaxation of it.
# The difference is the diff: "#, fuzzy" on the offenders is a handful of
# lines in a handful of files and leaves every other byte alone, where
# --merge rewrites references and re-wraps what it touches across all 86.
#
# Fuzzy does both jobs at once: Weblate shows the entry as "Needs editing"
# so it reaches a translator, and both compile paths already exclude fuzzy
# so it cannot reach a .mo. See tools/po_lint.py. The build filters again on
# its own, because a broken translation lands on Weblate's schedule and not
# on ours.
echo
echo "Checking translations that would raise when formatted:"
python3 "$(dirname "$0")/tools/po_lint.py" --fuzzy \
    "$(dirname "$0")"/jellyfin_mpv_shim/messages/*/LC_MESSAGES/base.po \
    || echo "  (could not run the format check)"

if [ "$MERGE_PO" -eq 0 ]; then
    echo
    echo "Wrote $POT. The per-locale .po files were NOT touched, except for"
    echo "  any '#, fuzzy' the format check added just above:"
    echo "  filling translations in is Weblate's job, and merging them here"
    echo "  is ~10k lines of churn across 86 files that conflicts with master."
    echo "Pass --merge if you are the maintainer doing a translation sync."
    exit 0
fi

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
        msgmerge --quiet --previous --add-location=file \
            --compendium "$po" "$master_po" "$POT" -o "$po"
    elif [ "$have_master" -eq 1 ]; then
        # locale only on master -> pull it into the working tree.
        echo "adding from master: $po"
        mkdir -p "$(dirname "$po")"
        msgmerge --quiet --previous --add-location=file \
            "$master_po" "$POT" -o "$po"
    else
        # locale only in working tree -> just refresh against the new template.
        echo "local only: $po"
        msgmerge --quiet --previous --add-location=file \
            "$po" "$POT" -o "$po"
    fi
done

echo
echo "Done. Review 'git diff' before committing."
echo "Weblate is landing translations on master continuously -- commit and"
echo "merge this promptly or it is stale before it lands."
