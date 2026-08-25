#!/bin/bash
set -euo pipefail

gsettings set org.gnome.desktop.background picture-uri \
    'file:///usr/share/backgrounds/noi_wallpaper_00.png'
gsettings set org.gnome.desktop.background picture-options 'spanned'
gsettings set org.gnome.desktop.session idle-delay 'uint32 0'
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.desktop.interface enable-animations false
gsettings set org.gnome.system.locale region 'zh_CN.UTF-8'
gsettings set org.gnome.desktop.input-sources sources \
    "[('xkb', 'us'), ('ibus', 'libpinyin')]"
gsettings set org.gnome.desktop.input-sources mru-sources \
    "[('xkb', 'us'), ('ibus', 'libpinyin')]"
gsettings set org.gnome.desktop.input-sources current 'uint32 0'
gsettings set org.gnome.desktop.wm.keybindings switch-input-source \
    "['<Super>space', '<Ctrl>space']"
gsettings set org.gnome.desktop.wm.keybindings switch-input-source-backward \
    "['<Shift><Super>space', '<Shift><Ctrl>space']"

# GNOME only launches desktop entries as applications after they are trusted.
# Persist this metadata inside the student's own DBus session; the finalizer
# retries after GVfs starts, which covers slower cold boots.
for launcher in \
    "$HOME/Desktop/answers.desktop" \
    "$HOME/Desktop/web-submit.desktop" \
    "$HOME/Desktop/CSP 程序回收系统.desktop" \
    "$HOME/Desktop/03_开始答题.desktop"; do
    [[ -f "$launcher" ]] || continue
    # Managed launchers are symlinks to the root-owned read-only contest
    # bundle and are already executable. Only legacy student-owned launchers
    # need a local mode correction here.
    if [[ ! -L "$launcher" ]]; then
        chmod 0755 "$launcher"
    fi
    gio set "$launcher" metadata::trusted true || true
done
