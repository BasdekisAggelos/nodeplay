#!/usr/bin/env bash
# Build nodeplay_<version>_all.deb into dist/
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(python3 -c 'import re;print(re.search(r"\"(.+)\"",open("nodeplay/__init__.py").read())[1])')
PKG=nodeplay
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
chmod 755 "$ROOT"

install -d "$ROOT/DEBIAN" "$ROOT/usr/bin" "$ROOT/usr/lib/$PKG/nodeplay" \
           "$ROOT/usr/share/applications" "$ROOT/usr/share/icons/hicolor/scalable/apps" \
           "$ROOT/usr/share/doc/$PKG"
install -m644 nodeplay/*.py "$ROOT/usr/lib/$PKG/nodeplay/"
install -m644 packaging/io.github.basdekisaggelos.Nodeplay.desktop "$ROOT/usr/share/applications/io.github.basdekisaggelos.Nodeplay.desktop"
install -m644 packaging/io.github.basdekisaggelos.Nodeplay.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/"
install -m755 packaging/nodeplay-share-helper "$ROOT/usr/lib/$PKG/nodeplay-share-helper"
install -d "$ROOT/usr/share/metainfo"
install -m644 packaging/io.github.basdekisaggelos.Nodeplay.metainfo.xml "$ROOT/usr/share/metainfo/io.github.basdekisaggelos.Nodeplay.metainfo.xml"
chmod 644 "$ROOT/usr/share/metainfo/io.github.basdekisaggelos.Nodeplay.metainfo.xml"
install -d "$ROOT/usr/share/polkit-1/actions"
install -m644 packaging/io.github.basdekisaggelos.Nodeplay.policy "$ROOT/usr/share/polkit-1/actions/"
cat > "$ROOT/usr/bin/$PKG" <<'SH'
#!/bin/sh
export PYTHONPATH=/usr/lib/nodeplay${PYTHONPATH:+:$PYTHONPATH}
exec /usr/bin/python3 -m nodeplay "$@"
SH
chmod 755 "$ROOT/usr/bin/$PKG"
printf 'Nodeplay — unofficial Linux controller for BluOS players. MIT licence.\nUses the BluOS Custom Integration API (HTTP, port 11000).\n' > "$ROOT/usr/share/doc/$PKG/README"
chmod 644 "$ROOT/usr/share/doc/$PKG/README"

cat > "$ROOT/DEBIAN/control" <<CTL
Package: $PKG
Version: $VERSION
Section: sound
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.4), gir1.2-webkit-6.0, pkexec | policykit-1
Replaces: bluos-controller
Conflicts: bluos-controller
Provides: bluos-controller
Recommends: samba
Maintainer: Aggelos Basdekis <262899638+BasdekisAggelos@users.noreply.github.com>
Installed-Size: $(du -sk "$ROOT/usr" | cut -f1)
Description: Nodeplay - unofficial Linux controller for BluOS players
 Control Bluesound, NAD and other BluOS streamers on your network:
 now playing, presets, browse/search TIDAL, TuneIn and other services,
 play queue, inputs and volume. Native GTK4/libadwaita app.
CTL
cat > "$ROOT/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
SH
chmod 755 "$ROOT/DEBIAN/postinst"

mkdir -p dist
dpkg-deb --root-owner-group --build "$ROOT" "dist/${PKG}_${VERSION}_all.deb"
