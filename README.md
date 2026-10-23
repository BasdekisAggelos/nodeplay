# Nodeplay — Linux controller for BluOS players (unofficial)

Native GTK4/libadwaita controller for BluOS players (Bluesound, NAD, …). Bluesound ships
controllers for Windows/macOS/Android/iOS only; this fills the Linux gap.

It talks directly to the player on the local network through the BluOS Custom Integration API
that Lenbrook publishes — the same local HTTP interface (port 11000) the official apps use.
No cloud service, no account, nothing installed on the player.

## Build & install

```
./packaging/build_deb.sh                       # -> dist/nodeplay_<ver>_all.deb
sudo apt install ./dist/nodeplay_*.deb
```

Install from the terminal as above: double-clicking the .deb only works if GNOME Software has
PackageKit (`packagekit`, `gnome-software-plugin-deb`).

Run from source: `/usr/bin/python3 -m nodeplay` (system python — needs `gi`).
Dev screenshot hook: `BLUOS_SNAPSHOT=/path/out.png,9` renders the window to PNG after 9 s.

## How it talks to the player

| What | Endpoint |
|---|---|
| Documented CI API v1.7 (status, volume, transport, queue, presets, browse + context menus, grouping, doorbell, inputs) | port 11000 |
| Home / Recently Played (server-driven UI, same as official app) | `/ui/Home`, `/ui/RecentlyPlayed` |
| Device settings tree (rendered generically) | `GET /Settings?schemaVersion=34` (→ :11001); write = `POST <url>` `name=value` on :11000 |
| Network shares | web form on :80 (`/manshare`, `/findremoveshares`); `AddShare`/`RemoveShare` exist too |
| Music Services, WiFi, IR learning | player web pages, embedded with WebKitGTK |
| Discovery | LSDP (UDP 11430), fallback /24 scan of port 11000 |

Notes:
- Track play uses `autoplayURL` (continue with album/playlist) unless turned off in Settings.
- WebKit needs Mesa EGL on NVIDIA (set only for the web process in `WebPage`).
- "Share a folder from this PC" runs `packaging/nodeplay-share-helper` via pkexec: Samba share,
  read-only, `force user` = you (guest → `nobody` can't traverse `/media/$USER`).
- Vendor PDFs/zips are kept locally but not committed (Lenbrook copyright).

Named Nodeplay (not "BluOS …") because BluOS/Bluesound are Lenbrook trademarks; we only say it works with them.
