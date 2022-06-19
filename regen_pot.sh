#!/bin/bash
pygettext3 --default-domain=base -o jellyfin_mpv_shim/messages/base.pot jellyfin_mpv_shim/*.py jellyfin_mpv_shim/**/*.py
find -iname '*.po' | while read -r file
do
    msgmerge --update "$file" --backup=none --previous jellyfin_mpv_shim/messages/base.pot
done

