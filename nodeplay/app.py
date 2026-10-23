"""Nodeplay — GTK4/libadwaita desktop app for Bluesound/NAD/BluOS players."""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .api import BluOSError, Player, scan_subnet  # noqa: E402
from .discovery import discover  # noqa: E402

APP_ID = "io.github.basdekisaggelos.Nodeplay"  # must match .desktop + icon names
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_PATH = CONFIG_DIR / "nodeplay" / "config.json"
OLD_CONFIG_PATH = CONFIG_DIR / "bluos-controller" / "config.json"  # before the rename to Nodeplay
IN_FLATPAK = Path("/.flatpak-info").exists()
FEEDBACK_URL = "https://github.com/BasdekisAggelos/nodeplay/issues"


# ---------------------------------------------------------------- helpers
def run_bg(fn, on_done=None, on_error=None):
    """Run fn() in a thread; deliver result/exception back on the GTK main loop."""
    def worker():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001 — surfaced to the UI
            if on_error:
                GLib.idle_add(on_error, e)
            return
        if on_done:
            GLib.idle_add(on_done, res)
    threading.Thread(target=worker, daemon=True).start()


def fmt_time(secs):
    if secs is None or secs < 0:
        return "--:--"
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def local_tz():
    """IANA zone name (e.g. Europe/Athens) — the player schedules alarms in it."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if "/" in tz:
        return tz
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def load_config():
    try:
        path = CONFIG_PATH if CONFIG_PATH.exists() else OLD_CONFIG_PATH
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


class Images:
    """Fetch artwork off the main thread, cache textures, set them on Gtk.Picture/Gtk.Image."""

    def __init__(self):
        self.cache = {}
        self.slots = threading.Semaphore(8)  # big libraries: don't open hundreds of connections

    def set(self, widget, url):
        widget._want_url = url
        if not url:
            self._apply(widget, None)
            return
        if url in self.cache:
            self._apply(widget, self.cache[url])
            return

        def fetch():
            sep = "&" if "?" in url else "?"
            full = url + (sep + "followRedirects=1" if "/Artwork" in url else "")
            req = urllib.request.Request(full, headers={"User-Agent": "Nodeplay-Linux"})
            with self.slots, urllib.request.urlopen(req, timeout=10) as r:
                return Gdk.Texture.new_from_bytes(GLib.Bytes.new(r.read()))

        def done(tex):
            self.cache[url] = tex
            if getattr(widget, "_want_url", None) == url:
                self._apply(widget, tex)

        run_bg(fetch, done, lambda e: None)

    @staticmethod
    def _apply(widget, tex):
        if isinstance(widget, Gtk.Picture):
            widget.set_paintable(tex)
        else:
            if tex is None:
                widget.set_from_icon_name("audio-x-generic-symbolic")
            else:
                widget.set_from_paintable(tex)


IMAGES = Images()


class ColorCSS:
    """Per-colour CSS classes on one provider (GTK4 has no per-widget style providers)."""
    provider = None
    known = {}

    @classmethod
    def cls(cls, bg, fg):
        key = f"{bg}-{fg}".lower()
        if key not in cls.known:
            name = "c" + "".join(ch for ch in key if ch.isalnum())
            cls.known[key] = name
            if cls.provider is None:
                cls.provider = Gtk.CssProvider()
                Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), cls.provider,
                                                          Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
            css = "".join(f"button.{n} {{ background: {k.split('-')[0]}; color: {k.split('-')[1]}; }} "
                          f"button.{n}:hover {{ filter: brightness(1.12); }} " for k, n in cls.known.items())
            cls.provider.load_from_data(css.encode())
        return cls.known[key]


def local_ip_for(remote_ip):
    """This PC's LAN address as seen from the player's network."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_ip, 11000))
        return s.getsockname()[0]
    finally:
        s.close()


def thumb(size=40):
    img = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
    img.set_pixel_size(size)
    img.set_size_request(size, size)
    img.add_css_class("thumb")
    return img


CSS = b"""
.art { border-radius: 12px; }
.thumb { border-radius: 6px; }
.title1 { font-size: 20pt; font-weight: 800; }
.title2 { font-size: 13pt; }
.title3 { font-size: 11pt; opacity: 0.7; }
.playbtn { min-width: 64px; min-height: 64px; }
.presetcard { padding: 8px; border-radius: 12px; }
.current-track { font-weight: bold; }
window, .view, scrolledwindow > viewport { background: #1e2223; }
.sidebar-pane { background: #000; }
.sidebar-pane .navigation-sidebar { background: #000; }
.sidebar-pane row.nav-gap { min-height: 6px; padding: 0; margin: 0; }
.home-gear { min-width: 40px; min-height: 40px; }
.row-title { font-size: 17pt; font-weight: 400; }
.row-arrow { min-width: 30px; min-height: 30px; padding: 0; background: alpha(#fff, 0.06); }
.tile { background: #2a2e30; border-radius: 10px; padding: 14px; }
.tile:hover { background: #33383a; }
.src-btn { border-radius: 6px; min-height: 34px; font-weight: 500; }
.playdot { min-width: 34px; min-height: 34px; padding: 0; border-radius: 999px;
           background: alpha(#000, 0.55); color: #fff; border: 2px solid alpha(#fff, 0.85); }
.playdot:hover { background: alpha(#000, 0.8); }
.svc-icon { opacity: 0.85; }
.bar-round { min-width: 44px; min-height: 44px; border-radius: 999px; border: 2px solid alpha(#fff, 0.8);
             background: transparent; }
.bar-round:hover { background: alpha(#fff, 0.08); }
.sidebar-pane row { padding: 10px 14px; border-left: 3px solid transparent; }
.sidebar-pane row:selected { background: alpha(#fff, 0.10); color: #fff; border-left-color: @accent_bg_color; }
.sidebar-pane row:hover { background: alpha(#fff, 0.06); }
.sidebar-pane row label { font-size: 13pt; }
.brand { font-size: 21pt; font-weight: 200; letter-spacing: 7px; margin: 20px 0 0 0; }
.brand-sub { font-size: 9pt; letter-spacing: 2px; opacity: 0.55; margin: 2px 0 12px 0; }
.page-title { font-size: 26pt; font-weight: 400; letter-spacing: 1px; }
.shelf { padding: 4px 0; }
.shelf.solid { background: #26292b; border-radius: 12px; padding: 16px; }
.shelf-title { font-size: 15pt; letter-spacing: 1px; }
.card { background: #2a2e30; border-radius: 10px; padding: 12px; }
.card:hover { background: #33383a; }
.card-label { font-size: 10pt; }
.playerbar { background: #2b2b2b; border-top: 1px solid alpha(#fff, 0.08); padding: 0; }
.playerbar-art { min-width: 80px; min-height: 80px; background: #fff; }
"""


# ---------------------------------------------------------------- pages
class NowPlayingPage(Gtk.ScrolledWindow):
    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win = win
        self._vol_lock = False
        self._vol_timer = None
        self._seek_lock = False

        clamp = Adw.Clamp(maximum_size=520, margin_top=18, margin_bottom=18, margin_start=16, margin_end=16)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        clamp.set_child(box)
        self.set_child(clamp)

        self.art = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.art.set_size_request(300, 300)
        self.art.add_css_class("art")
        box.append(self.art)

        self.l1 = self._label("title1")
        self.l2 = self._label("title2")
        self.l3 = self._label("title3")
        for lbl in (self.l1, self.l2, self.l3):
            box.append(lbl)
        self.quality = self._label("caption")
        self.quality.add_css_class("dim-label")
        box.append(self.quality)

        # progress
        self.progress = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.progress.set_draw_value(False)
        self.progress.connect("change-value", self._on_seek)
        times = Gtk.Box(spacing=6)
        self.t_pos = Gtk.Label(label="--:--", xalign=0, hexpand=True)
        self.t_len = Gtk.Label(label="--:--", xalign=1)
        for t in (self.t_pos, self.t_len):
            t.add_css_class("caption")
            t.add_css_class("numeric")
            times.append(t)
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.progress_box.append(self.progress)
        self.progress_box.append(times)
        box.append(self.progress_box)

        # transport
        tr = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.shuffle_btn = Gtk.ToggleButton(icon_name="media-playlist-shuffle-symbolic", tooltip_text="Shuffle",
                                            valign=Gtk.Align.CENTER)
        self.shuffle_btn.add_css_class("flat")
        self.shuffle_btn.connect("clicked", self._on_shuffle)
        self.back_btn = self._round("media-skip-backward-symbolic", "Previous", self._on_back)
        self.play_btn = self._round("media-playback-start-symbolic", "Play / Pause", self._on_playpause)
        self.play_btn.add_css_class("suggested-action")
        self.play_btn.add_css_class("playbtn")
        self.next_btn = self._round("media-skip-forward-symbolic", "Next", self._on_next)
        self.repeat_btn = Gtk.Button(icon_name="media-playlist-repeat-symbolic", tooltip_text="Repeat",
                                     valign=Gtk.Align.CENTER)
        self.repeat_btn.add_css_class("flat")
        self.repeat_btn.connect("clicked", self._on_repeat)
        self.stop_btn = self._round("media-playback-stop-symbolic", "Stop", lambda _b: self.win.cmd(lambda p: p.stop()))
        for w in (self.shuffle_btn, self.back_btn, self.play_btn, self.next_btn, self.repeat_btn):
            tr.append(w)
        box.append(tr)

        extra = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        for icon, tip, cb in (("go-previous-symbolic", "Previous preset", lambda _b: self.win.cmd(lambda p: p.preset_step(-1))),
                              ("starred-symbolic", None, None),
                              ("go-next-symbolic", "Next preset", lambda _b: self.win.cmd(lambda p: p.preset_step(1)))):
            if cb is None:
                lbl = Gtk.Label(label="Preset")
                lbl.add_css_class("dim-label")
                extra.append(lbl)
                continue
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            extra.append(b)
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=8, margin_end=8)
        extra.append(sep)
        extra.append(self.stop_btn)
        url_btn = Gtk.Button(label="Play URL…", tooltip_text="Play an internet radio stream or audio file URL")
        url_btn.add_css_class("flat")
        url_btn.connect("clicked", lambda _b: self._ask_url())
        extra.append(url_btn)
        box.append(extra)
        self.sleep_lbl = Gtk.Label()
        self.sleep_lbl.add_css_class("caption")
        self.sleep_lbl.add_css_class("dim-label")
        box.append(self.sleep_lbl)

        # love / ban for radio services that offer it
        self.actions_box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        box.append(self.actions_box)

        # volume
        self.vol_box = Gtk.Box(spacing=8, margin_top=8)
        self.mute_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic", tooltip_text="Mute")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.connect("clicked", self._on_mute)
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol.set_hexpand(True)
        self.vol.set_draw_value(True)
        self.vol.set_value_pos(Gtk.PositionType.RIGHT)
        self.vol.connect("value-changed", self._on_volume)
        self.vol_box.append(self.mute_btn)
        self.vol_box.append(self.vol)
        box.append(self.vol_box)
        self.fixed_vol = Gtk.Label(label="Fixed volume — use your amplifier's volume control")
        self.fixed_vol.add_css_class("dim-label")
        self.fixed_vol.add_css_class("caption")
        box.append(self.fixed_vol)

        self.status = {}
        self.status_at = 0.0
        GLib.timeout_add_seconds(1, self._tick)

    @staticmethod
    def _label(css):
        lbl = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        lbl.add_css_class(css)
        return lbl

    @staticmethod
    def _round(icon, tip, cb):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
        b.add_css_class("circular")
        b.connect("clicked", cb)
        return b

    # -- state from player
    def update(self, s):
        self.status = s
        self.status_at = time.monotonic()
        self.l1.set_label(s.get("title1") or s.get("name") or "Nothing playing")
        self.l2.set_label(s.get("title2") or s.get("artist") or "")
        self.l3.set_label(s.get("title3") or s.get("album") or "")
        self.l2.set_visible(bool(self.l2.get_label()))
        self.l3.set_visible(bool(self.l3.get_label()))
        q = [s.get("serviceName") or s.get("service") or "", s.get("streamFormat") or ""]
        self.quality.set_label("  ·  ".join(x for x in q if x))

        img = s.get("image") or s.get("stationImage")
        IMAGES.set(self.art, self.win.player.url(img) if img and self.win.player else None)

        playing = s.get("state") in ("play", "stream", "connecting")
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")

        is_stream = bool(s.get("streamUrl"))
        names = {a.get("name") for a in s.get("actions", [])}
        self.shuffle_btn.set_visible(not is_stream)
        self.repeat_btn.set_visible(not is_stream)
        self.back_btn.set_sensitive(not is_stream or "back" in names)
        self.next_btn.set_sensitive(not is_stream or "skip" in names)
        self.shuffle_btn.handler_block_by_func(self._on_shuffle)
        self.shuffle_btn.set_active(s.get("shuffle") == 1)
        self.shuffle_btn.handler_unblock_by_func(self._on_shuffle)
        rep = s.get("repeat")
        self.repeat_btn.set_icon_name("media-playlist-repeat-song-symbolic" if rep == 1
                                      else "media-playlist-repeat-symbolic")
        self.repeat_btn.set_opacity(0.4 if rep in (2, None) else 1.0)
        self.repeat_btn.set_tooltip_text({0: "Repeat: queue", 1: "Repeat: track"}.get(rep, "Repeat: off"))

        child = self.actions_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.actions_box.remove(child)
            child = nxt
        for a in s.get("actions", []):
            if a.get("name") in ("love", "ban") and a.get("url"):
                b = Gtk.Button(label=a.get("text") or a["name"].title())
                b.add_css_class("pill")
                b.connect("clicked", lambda _b, a=a: self.win.cmd(lambda p: p.action(a["url"]),
                                                                 toast=a.get("notification")))
                self.actions_box.append(b)

        sleep = (s.get("sleep") or "").strip()
        self.sleep_lbl.set_label(f"Sleep timer: {sleep} min" if sleep else "")
        self.sleep_lbl.set_visible(bool(sleep))
        totlen = s.get("totlen")
        self.progress_box.set_visible(bool(totlen) or s.get("secs") is not None)
        self.progress.set_sensitive(bool(s.get("canSeek")) and bool(totlen))
        self._seek_lock = True
        self.progress.set_range(0, max(totlen or 1, 1))
        self._seek_lock = False
        self._render_pos()

        vol = s.get("volume")
        fixed = vol is None or vol < 0
        self.vol_box.set_visible(not fixed)
        self.fixed_vol.set_visible(vol == -1)
        if not fixed and self._vol_timer is None:
            self._vol_lock = True
            self.vol.set_value(vol)
            self._vol_lock = False
        muted = s.get("mute") == 1
        self.mute_btn.handler_block_by_func(self._on_mute)
        self.mute_btn.set_active(muted)
        self.mute_btn.handler_unblock_by_func(self._on_mute)
        self.mute_btn.set_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")

    def _ask_url(self):
        dlg = Adw.MessageDialog(transient_for=self.win, heading="Play a stream URL",
                                body="An internet radio stream or audio file, e.g. https://…/stream.mp3")
        entry = Gtk.Entry(placeholder_text="https://", activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("play", "Play")
        dlg.set_default_response("play")
        dlg.set_response_appearance("play", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            url = entry.get_text().strip()
            if r == "play" and url:
                self.win.cmd(lambda p: p.play_url(url), toast="Playing URL")
        dlg.connect("response", resp)
        dlg.present()

    def _cur_secs(self):
        s = self.status
        secs = s.get("secs")
        if secs is None:
            return None
        if s.get("state") in ("play", "stream"):
            secs += time.monotonic() - self.status_at
        tot = s.get("totlen")
        return min(secs, tot) if tot else secs

    def _render_pos(self):
        cur = self._cur_secs()
        self.t_pos.set_label(fmt_time(cur))
        self.t_len.set_label(fmt_time(self.status.get("totlen")) if self.status.get("totlen") else "LIVE"
                             if self.status.get("streamUrl") else "--:--")
        if cur is not None and self.status.get("totlen"):
            self._seek_lock = True
            self.progress.set_value(cur)
            self._seek_lock = False

    def _tick(self):
        if self.status:
            self._render_pos()
        return True

    # -- user actions
    def _on_playpause(self, _b):
        st = self.status.get("state")
        if st in ("play", "stream", "connecting"):
            self.win.cmd(lambda p: p.pause())
        else:
            self.win.cmd(lambda p: p.play())

    def _on_next(self, _b):
        url = next((a.get("url") for a in self.status.get("actions", []) if a.get("name") == "skip"), None)
        self.win.cmd(lambda p: p.action(url) if url else p.skip())

    def _on_back(self, _b):
        url = next((a.get("url") for a in self.status.get("actions", []) if a.get("name") == "back"), None)
        self.win.cmd(lambda p: p.action(url) if url else p.back())

    def _on_shuffle(self, b):
        on = b.get_active()
        self.win.cmd(lambda p: p.shuffle(on))

    def _on_repeat(self, _b):
        # cycle: off(2) -> queue(0) -> track(1) -> off(2)
        nxt = {2: 0, 0: 1, 1: 2}.get(self.status.get("repeat"), 0)
        self.win.cmd(lambda p: p.repeat(nxt))

    def _on_seek(self, _scale, _scroll, value):
        if self._seek_lock or not self.status.get("canSeek"):
            return False
        self.win.cmd(lambda p: p.play(seek=max(0, value)))
        return False

    def _on_mute(self, b):
        on = b.get_active()
        self.win.cmd(lambda p: p.set_mute(on))

    def _on_volume(self, scale):
        if self._vol_lock:
            return
        if self._vol_timer:
            GLib.source_remove(self._vol_timer)

        def send():
            self._vol_timer = None
            level = int(scale.get_value())
            self.win.cmd(lambda p: p.set_volume(level))
            return False
        self._vol_timer = GLib.timeout_add(150, send)


class PresetsPage(Gtk.ScrolledWindow):
    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win = win
        self.prid = None
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                max_children_per_line=6, min_children_per_line=2,
                                row_spacing=12, column_spacing=12,
                                margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
                                valign=Gtk.Align.START)
        self.empty = Adw.StatusPage(icon_name="starred-symbolic", title="No presets",
                                    description="Presets are created in the BluOS app on your phone.")
        self.stack = Gtk.Stack()
        self.stack.add_named(self.flow, "list")
        self.stack.add_named(self.empty, "empty")
        self.set_child(self.stack)

    def maybe_reload(self, prid):
        if prid != self.prid:
            self.prid = prid
            self.reload()

    def reload(self):
        run_bg(lambda: self.win.player.presets(), self._fill, self.win.error)

    def _fill(self, presets):
        self.win.home.set_presets(presets)
        while (c := self.flow.get_first_child()):
            self.flow.remove(c)
        for p in presets:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("presetcard")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            pic.set_size_request(140, 140)
            pic.add_css_class("art")
            IMAGES.set(pic, self.win.player.url(p["image"]) if p.get("image") else None)
            lbl = Gtk.Label(label=f"{p.get('id')}. {p.get('name', '')}", wrap=True,
                            justify=Gtk.Justification.CENTER, max_width_chars=18)
            v.append(pic)
            v.append(lbl)
            btn.set_child(v)
            btn.connect("clicked", lambda _b, p=p: self.win.cmd(lambda pl: pl.load_preset(p["id"]),
                                                                toast=f"Playing {p.get('name')}"))
            self.flow.append(btn)
        self.stack.set_visible_child_name("list" if presets else "empty")


COLLECTION_TYPES = {"albums", "artists", "playlists", "tracks", "composers", "genres", "items", "folders"}
SORTS = ["Default order", "Title A–Z", "Title Z–A", "Artist A–Z", "Artist Z–A"]
MAX_ITEMS = 5000


def load_all(player, key, query=None):
    """Browse, but merge alphabetic sections and follow nextKey paging so lists are complete."""
    from concurrent.futures import ThreadPoolExecutor

    def pages(k):
        res = player.browse(k, context_menus=True)
        items = [i for _, its in res["sections"] for i in its]
        nk, guard = res.get("nextKey"), 0
        while nk and len(items) < MAX_ITEMS and guard < 100:
            more = player.browse(nk, context_menus=True)
            items += [i for _, its in more["sections"] for i in its]
            nk, guard = more.get("nextKey"), guard + 1
        return res, items

    res = player.browse(key, query, context_menus=True)
    if res.get("type") == "sections":
        keys = [i["browseKey"] for _, its in res["sections"] for i in its if i.get("browseKey")]
        with ThreadPoolExecutor(6) as ex:
            parts = list(ex.map(pages, keys))
        items = [i for _, its in parts for i in its]
        meta = dict(parts[0][0]) if parts else dict(res)
        meta.pop("nextKey", None)
        meta["sections"] = [(None, items)]
        meta["merged"] = True
        return meta
    if res.get("nextKey") and len(res["sections"]) == 1 and res["sections"][0][0] is None:
        _, items = pages(key) if not query else (None, None)
        if items is not None:
            res = dict(res)
            res.pop("nextKey", None)
            res["sections"] = [(None, items)]
    return res


class BrowsePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.history = []  # [(key, title, query)]
        self.search_key = None
        self.next_key = None

        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        self.back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        self.back.connect("clicked", lambda _b: self.go_back())
        self.home = Gtk.Button(icon_name="go-home-symbolic", tooltip_text="Sources")
        self.home.connect("clicked", lambda _b: self.open(None, "Sources", reset=True))
        self.search = Gtk.SearchEntry(hexpand=True, placeholder_text="Search")
        self.search.connect("activate", self._on_search)
        self.add_lib = Gtk.Button(label="Add library", visible=False, tooltip_text="Add a network music folder")
        self.add_lib.add_css_class("suggested-action")
        self.add_lib.connect("clicked", lambda _b: win.open_shares())
        bar.append(self.back)
        bar.append(self.home)
        bar.append(self.search)
        bar.append(self.add_lib)
        self.append(bar)

        self.crumb = Gtk.Label(xalign=0, margin_start=16, margin_bottom=4, ellipsize=3)
        self.crumb.add_css_class("heading")
        self.append(self.crumb)

        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           margin_start=12, margin_end=12, margin_bottom=12)
        clamp = Adw.Clamp(maximum_size=1800)
        clamp.set_child(self.box)
        self.scroller.set_child(clamp)
        self.append(self.scroller)
        self.spinner = Gtk.Spinner()

    def reset(self):
        self.open(None, "Sources", reset=True)

    def open(self, key, title, query=None, reset=False):
        if reset:
            self.history = []
        self.history.append((key, title, query))
        self._load()

    def go_back(self):
        if len(self.history) > 1:
            self.history.pop()
            self._load()

    def _on_search(self, entry):
        q = entry.get_text().strip()
        if q:
            self.open(self.search_key, f"Search: {q}", query=q)

    def _load(self):
        key, title, query = self.history[-1]
        self.add_lib.set_visible(any((k or "").startswith("LocalMusic") for k, _, _ in self.history))
        self.back.set_sensitive(len(self.history) > 1)
        self.crumb.set_label(" › ".join(t for _, t, _ in self.history[-3:]))
        self._clear()
        self.box.append(self.spinner)
        self.spinner.start()
        player = self.win.player
        run_bg(lambda: load_all(player, key, query), self._fill, self._fail)

    def _clear(self):
        while (c := self.box.get_first_child()):
            self.box.remove(c)

    def _fail(self, e):
        self._clear()
        self.box.append(Adw.StatusPage(icon_name="dialog-warning-symbolic", title="Can't open this",
                                       description=str(e)))

    def _fill(self, res, append=False):
        secs = res.get("sections") or []
        if (not append and res.get("type") in COLLECTION_TYPES and len(secs) == 1 and secs[0][0] is None
                and len(secs[0][1]) > 1):
            self._clear()
            if res.get("searchKey"):
                self.search_key = res["searchKey"]
            self.search.set_placeholder_text(f"Search {res.get('serviceName', 'all sources')}")
            self._collection(res["type"], secs[0][1])
            self.scroller.get_vadjustment().set_value(0)
            return
        if not append:
            self._clear()
            if res.get("searchKey"):
                self.search_key = res["searchKey"]
            elif len(self.history) == 1:
                self.search_key = None
            self.search.set_placeholder_text(f"Search {res.get('serviceName', 'all sources')}")
        else:
            self.box.remove(self.more_btn)
        self.next_key = res.get("nextKey")
        for heading, items in res["sections"]:
            if heading:
                h = Gtk.Label(label=heading, xalign=0, margin_top=6)
                h.add_css_class("title-4")
                self.box.append(h)
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            lb.add_css_class("boxed-list")
            for it in items:
                lb.append(self._row(it))
            self.box.append(lb)
        if not res["sections"]:
            self.box.append(Adw.StatusPage(icon_name="edit-find-symbolic", title="Nothing here"))
        if self.next_key:
            self.more_btn = Gtk.Button(label="Load more", halign=Gtk.Align.CENTER)
            self.more_btn.add_css_class("pill")
            nk = self.next_key
            self.more_btn.connect("clicked", lambda _b: run_bg(lambda: self.win.player.browse(nk, context_menus=True),
                                                               lambda r: self._fill(r, append=True),
                                                               self.win.error))
            self.box.append(self.more_btn)
        if not append:
            self.scroller.get_vadjustment().set_value(0)

    # ---- complete lists with view preferences (grid/list, sort, filter)
    def _collection(self, typ, items):
        cfg = self.win.cfg.setdefault("view", {})
        pref = cfg.setdefault(typ, {"grid": typ in ("albums", "artists", "playlists"), "sort": 0})
        state = {"shown": 300}

        bar = Gtk.Box(spacing=8, margin_top=4)
        flt = Gtk.SearchEntry(placeholder_text="Filter", hexpand=True)
        sort = Gtk.DropDown(model=Gtk.StringList.new(SORTS), selected=pref["sort"], tooltip_text="Sort")
        grid_btn = Gtk.ToggleButton(icon_name="view-grid-symbolic", tooltip_text="Grid", active=pref["grid"])
        list_btn = Gtk.ToggleButton(icon_name="view-list-symbolic", tooltip_text="List", group=grid_btn,
                                    active=not pref["grid"])
        views = Gtk.Box()
        views.add_css_class("linked")
        views.append(grid_btn)
        views.append(list_btn)
        count = Gtk.Label()
        count.add_css_class("dim-label")
        for w in (flt, sort, views, count):
            bar.append(w)
        self.box.append(bar)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.append(holder)

        def keyfn(mode):
            title = lambda i: (i.get("text") or "").casefold()
            artist = lambda i: ((i.get("text2") or "").casefold(), title(i))
            return {1: title, 2: title, 3: artist, 4: artist}.get(mode)

        def render(*_a):
            while (c := holder.get_first_child()):
                holder.remove(c)
            q = flt.get_text().strip().casefold()
            rows = [i for i in items if not q or q in (i.get("text", "") + " " + i.get("text2", "")).casefold()]
            mode = sort.get_selected()
            if keyfn(mode):
                rows = sorted(rows, key=keyfn(mode), reverse=mode in (2, 4))
            count.set_label(f"{len(rows)} of {len(items)}" if q else f"{len(items)} items")
            pref.update(grid=grid_btn.get_active(), sort=mode)
            save_config(self.win.cfg)
            visible = rows[:state["shown"]]
            if grid_btn.get_active():
                fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                 max_children_per_line=30, min_children_per_line=2,
                                 row_spacing=12, column_spacing=12, valign=Gtk.Align.START)
                for it in visible:
                    fb.append(card(self.win, it.get("text", ""), it.get("image"),
                                   lambda it=it: self._activate(it), subtitle=it.get("text2"), size=140))
                holder.append(fb)
            else:
                lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
                lb.add_css_class("boxed-list")
                for it in visible:
                    lb.append(self._row(it))
                holder.append(lb)
            if len(rows) > len(visible):
                more = Gtk.Button(label=f"Show more ({len(rows) - len(visible)} left)", halign=Gtk.Align.CENTER)
                more.add_css_class("pill")
                more.connect("clicked", lambda _b: (state.update(shown=state["shown"] + 300), render()))
                holder.append(more)

        flt.connect("search-changed", lambda *_: (state.update(shown=300), render()))
        sort.connect("notify::selected", render)
        grid_btn.connect("toggled", lambda b: b.get_active() and render())
        list_btn.connect("toggled", lambda b: b.get_active() and render())
        render()

    def play_link(self, it):
        """Doc 7.1: autoplayURL plays the item and continues with the rest of its album/playlist;
        playURL plays only the item. Which one is the default is a user preference."""
        if self.win.cfg.get("autoplay", True) and it.get("autoplayURL"):
            return it["autoplayURL"]
        return it.get("playURL")

    def _activate(self, it):
        if it.get("browseKey"):
            self.open(it["browseKey"], it.get("text", ""))
        elif self.play_link(it):
            url = self.play_link(it)
            self.win.cmd(lambda p: p.action(url), toast=f"Playing {it.get('text', '')}")

    def _menu_button(self, it):
        """⋮ menu with the player's context actions: play now, add next/last, favourite, preset…"""
        mb = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, tooltip_text="More")
        mb.add_css_class("flat")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        pop.set_child(box)
        mb.set_popover(pop)

        def fill(entries):
            while (c := box.get_first_child()):
                box.remove(c)
            for e in entries:
                if not (e.get("actionURL") or e.get("browseKey")):
                    continue
                b = Gtk.Button(label=e.get("text", ""))
                b.add_css_class("flat")
                if "delete" in (e.get("type") or ""):
                    b.add_css_class("destructive-action")
                b.connect("clicked", lambda _b, e=e: (pop.popdown(), self._run_menu(e)))
                box.append(b)
            if not box.get_first_child():
                box.append(Gtk.Label(label="No actions"))

        if it.get("contextMenu"):
            fill(it["contextMenu"])
        else:
            box.append(Gtk.Spinner(spinning=True))
            key = it["contextMenuKey"]
            mb.connect("notify::active", lambda m, _p: m.get_active() and not getattr(m, "_loaded", False) and
                       (setattr(m, "_loaded", True),
                        run_bg(lambda: [i for _, its in self.win.player.browse(key)["sections"] for i in its],
                               fill, self.win.error)))
        return mb

    def _run_menu(self, e):
        if e.get("browseKey") and not e.get("actionURL"):
            self.open(e["browseKey"], e.get("text", ""))
            return
        url, typ, text = e["actionURL"], e.get("type") or "", e.get("text", "")

        def done(_r):
            self.win.toast(text)
            if "favourite" in typ or typ == "delete":
                self._load()
        if typ == "delete":
            self.win.settings._confirm(f"{text}?", "This cannot be undone.", text,
                                       lambda: self.win.cmd(lambda p: p.action(url)) or self._load())
        else:
            p = self.win.player
            run_bg(lambda: p.action(url), lambda r: (done(r), self.win.kick()), self.win.error)

    def _row(self, it):
        row = Adw.ActionRow(title=GLib.markup_escape_text(it.get("text", "")),
                            subtitle=GLib.markup_escape_text(it.get("text2", "")))
        img = thumb()
        IMAGES.set(img, self.win.player.url(it["image"]) if it.get("image") else None)
        row.add_prefix(img)
        browse_key, play_url = it.get("browseKey"), self.play_link(it)
        if it.get("contextMenu") or it.get("contextMenuKey"):
            row.add_suffix(self._menu_button(it))
        if play_url:
            b = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play",
                           valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b: self.win.cmd(lambda p: p.action(play_url),
                                                         toast=f"Playing {it.get('text', '')}"))
            row.add_suffix(b)
        if browse_key:
            row.set_activatable(True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r: self.open(browse_key, it.get("text", "")))
        elif play_url:
            row.set_activatable(True)
            row.connect("activated", lambda _r: self.win.cmd(lambda p: p.action(play_url),
                                                             toast=f"Playing {it.get('text', '')}"))
        return row


class QueuePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.pid = None
        self.current = None
        self.rows = {}

        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        self.info = Gtk.Label(xalign=0, hexpand=True)
        self.info.add_css_class("heading")
        save = Gtk.Button(label="Save as playlist")
        save.connect("clicked", self._on_save)
        self._save_btn = save
        clear = Gtk.Button(label="Clear queue")
        clear.add_css_class("destructive-action")
        clear.connect("clicked", self._on_clear)
        bar.append(self.info)
        bar.append(self._save_btn)
        bar.append(clear)
        self.append(bar)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        clamp = Adw.Clamp(maximum_size=900, margin_start=12, margin_end=12, margin_bottom=12)
        clamp.set_child(self.list)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(clamp)
        self.append(sc)

    def maybe_reload(self, pid, song):
        if pid != self.pid:
            self.pid = pid
            self.reload()
        if song != self.current:
            self._mark(self.current, False)
            self.current = song
            self._mark(song, True)

    def _mark(self, song, on):
        row = self.rows.get(song)
        if row:
            (row.add_css_class if on else row.remove_css_class)("current-track")
            row.icon.set_visible(on)

    def reload(self):
        run_bg(lambda: self.win.player.queue(0, 499), self._fill, self.win.error)

    def _fill(self, songs):
        self.list.remove_all()
        self.rows = {}
        self.info.set_label(f"{len(songs)} tracks" if songs else "Queue is empty — radio and inputs don't use the queue")
        self.list.set_visible(bool(songs))
        self._save_btn.set_sensitive(bool(songs))
        for s in songs:
            row = Adw.ActionRow(title=GLib.markup_escape_text(s["title"]),
                                subtitle=GLib.markup_escape_text(" — ".join(x for x in (s["artist"], s["album"]) if x)),
                                activatable=True)
            row.icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
            row.icon.set_visible(False)
            row.add_prefix(row.icon)
            for icon, tip, delta in (("go-up-symbolic", "Move up", -1), ("go-down-symbolic", "Move down", 1)):
                mv = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
                mv.add_css_class("flat")
                new = s["id"] + delta
                mv.set_sensitive(0 <= new < len(songs))
                mv.connect("clicked", lambda _b, old=s["id"], new=new: self.win.cmd(lambda p: p.move_track(old, new)))
                row.add_suffix(mv)
            rm = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove", valign=Gtk.Align.CENTER)
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, i=s["id"]: self.win.cmd(lambda p: p.delete_track(i)))
            row.add_suffix(rm)
            row.connect("activated", lambda _r, i=s["id"]: self.win.cmd(lambda p: p.play_queue_track(i)))
            self.list.append(row)
            self.rows[s["id"]] = row
        self._mark(self.current, True)

    def _on_save(self, _b):
        dlg = Adw.MessageDialog(transient_for=self.win, heading="Save queue as playlist",
                                body="It appears under Playlists in the sidebar.")
        entry = Gtk.Entry(placeholder_text="Playlist name", activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_default_response("save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            name = entry.get_text().strip()
            if r == "save" and name:
                p = self.win.player
                run_bg(lambda: p.save_queue(name), lambda n: self.win.toast(f"Saved “{name}” ({n} tracks)"),
                       self.win.error)
        dlg.connect("response", resp)
        dlg.present()

    def _on_clear(self, _b):
        dlg = Adw.MessageDialog(transient_for=self.win, heading="Clear the play queue?",
                                body="All tracks will be removed from the queue.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("clear", "Clear")
        dlg.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "clear" and self.win.cmd(lambda p: p.clear_queue()))
        dlg.present()


# ---------------------------------------------------------------- device settings
class SettingsPage(Gtk.Box):
    """Player settings, rendered from the schema the player serves (/Settings) — like the official app."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.path = []  # [(title, page_id, element or None)]
        self._bt_timer = None

        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=4, margin_start=12, margin_end=12)
        self.back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        self.back.connect("clicked", lambda _b: self.go_back())
        self.title = Gtk.Label(xalign=0, hexpand=True)
        self.title.add_css_class("title-2")
        bar.append(self.back)
        bar.append(self.title)
        self.append(bar)

        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                           margin_top=8, margin_start=12, margin_end=12, margin_bottom=24)
        clamp = Adw.Clamp(maximum_size=760)
        clamp.set_child(self.box)
        self.scroller.set_child(clamp)
        self.append(self.scroller)

    # -- navigation
    def reset(self):
        self.path = [("Settings", None, None, None)]
        self.reload()

    def open(self, group, base_url):
        self.path.append((group.get("displayName", ""), group.get("id"),
                          group if len(group) else None, group.get("url") or base_url))
        self.reload()

    def go_back(self):
        if len(self.path) > 1:
            self.path.pop()
            self.reload()

    def open_shares(self):
        self.path.append(("Network shares", "__shares__", None, None))
        self.reload()

    def open_special(self, title, page_id, data=None):
        self.path.append((title, page_id, data, None))
        self.reload()

    def reload(self, keep_scroll=False):
        entry = self.path[-1]
        title, page_id = entry[0], entry[1]
        self.back.set_visible(len(self.path) > 1)
        self.title.set_label(title)
        if self._bt_timer and page_id != "__bt__":
            GLib.source_remove(self._bt_timer)
            self._bt_timer = None
        p = self.win.player
        if page_id == "__shares__":
            run_bg(p.shares, self._render_shares, self.win.error)
            return
        if page_id == "__alarms__":
            run_bg(lambda: p.alarms(local_tz()), self._render_alarms, self.win.error)
            return
        if page_id == "__alarm__":
            run_bg(p.alarm_sources, lambda src: self._render_alarm_edit(entry[2], src), self.win.error)
            return
        if page_id == "__bt__":
            self._bt_refresh(scan=True)
            return
        pos = self.scroller.get_vadjustment().get_value() if keep_scroll else 0

        def fetch():
            root = p.settings(page_id)
            node = root
            if page_id:
                node = next((g for g in root.iter("menuGroup") if g.get("id") == page_id), root)
            info = p.sync_status() if page_id is None else None
            return node, info

        def done(res):
            node, info = res
            self._render(node, info)
            GLib.idle_add(lambda: self.scroller.get_vadjustment().set_value(pos) and False)
        run_bg(fetch, done, self.win.error)

    # -- rendering
    def _clear(self):
        while (c := self.box.get_first_child()):
            self.box.remove(c)

    def _render(self, node, info):
        self._clear()
        base_url = self.path[-1][3] if len(self.path[-1]) > 3 else None
        values = {s.get("name"): s.get("value") for s in node.iter("setting") if s.get("name")}
        if info:
            self.box.append(self._device_card(info))

        group = Adw.PreferencesGroup()
        for el in node:
            if el.tag == "menuGroup" and el.get("id") in ("alarms",):
                continue
            row = self._row(el, base_url or node.get("url"), values)
            if row is None:
                continue
            if isinstance(row, Adw.PreferencesGroup):  # inline sub-group
                self.box.append(group)
                self.box.append(row)
                group = Adw.PreferencesGroup()
            else:
                group.add(row)
        self.box.append(group)

        if info:
            extra = Adw.PreferencesGroup(title="More")
            for title, sub, icon, pid, show in (
                    ("Alarms", "Wake up to a favorite, an input or whatever was playing",
                     "alarm-symbolic", "__alarms__", not any(e.get("class") == "alarms" for e in node.iter())),
                    ("Bluetooth", "Play from your phone, or listen on Bluetooth headphones",
                     "bluetooth-symbolic", "__bt__", True)):
                if not show:
                    continue
                r = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
                r.add_prefix(Gtk.Image.new_from_icon_name(icon))
                r.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                r.connect("activated", lambda _r, t=title, pid=pid: self.open_special(t, pid))
                extra.add(r)
            self.box.append(extra)

            app = Adw.PreferencesGroup(title="This app")
            ap = Adw.SwitchRow(title="Keep playing the album or playlist",
                               subtitle="When you play a single track, continue with the tracks after it. "
                                        "Off: play only that track.",
                               active=self.win.cfg.get("autoplay", True))
            ap.connect("notify::active", lambda r, _p: (self.win.cfg.__setitem__("autoplay", r.get_active()),
                                                        save_config(self.win.cfg)))
            app.add(ap)
            fb = Adw.ActionRow(title="Suggest a feature or report a problem",
                               subtitle="Have a different BluOS device (speaker, soundbar, amplifier)? "
                                        "Tell us what you need — we only test on a NODE so far.",
                               activatable=True)
            fb.add_prefix(Gtk.Image.new_from_icon_name("mail-send-symbolic"))
            fb.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
            fb.connect("activated", lambda _r: Gtk.UriLauncher.new(FEEDBACK_URL).launch(self.win, None, None, None))
            app.add(fb)
            self.box.append(app)
            danger = Adw.PreferencesGroup(title="Maintenance")
            rb = Adw.ActionRow(title="Reboot player", subtitle="Soft restart — music stops for about a minute",
                               activatable=True)
            rb.add_prefix(Gtk.Image.new_from_icon_name("system-reboot-symbolic"))
            rb.connect("activated", lambda _r: self._confirm(
                "Reboot the player?", "Playback stops while it restarts.", "Reboot",
                lambda: self.win.cmd(lambda p: p.reboot(), toast="Rebooting…")))
            danger.add(rb)
            db = Adw.ActionRow(title="Play doorbell chime", subtitle="Test the chime used by doorbell integrations",
                               activatable=True)
            db.add_prefix(Gtk.Image.new_from_icon_name("preferences-system-notifications-symbolic"))
            db.connect("activated", lambda _r: self.win.cmd(lambda p: p.doorbell(), toast="Ding dong"))
            danger.add(db)
            self.box.append(danger)

    def _render_shares(self, shares):
        self._clear()
        cur = Adw.PreferencesGroup(title="Your music libraries",
                                   description="Shared folders the player indexes for the Library source.")
        if not shares:
            cur.add(Adw.ActionRow(title="No network shares yet"))
        for path, label in shares:
            r = Adw.ActionRow(title=GLib.markup_escape_text(label))
            r.add_prefix(Gtk.Image.new_from_icon_name("folder-remote-symbolic"))
            rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove", valign=Gtk.Align.CENTER)
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, path=path: self._confirm(
                "Remove this library?", f"{path}\nThe music files stay where they are; the player just stops indexing them.",
                "Remove", lambda: self._share_op(lambda p: p.remove_share(path), "Library removed")))
            r.add_suffix(rm)
            cur.add(r)
        if shares:
            ri = Adw.ActionRow(title="Rescan library", subtitle="Pick up music you added or removed",
                               activatable=True)
            ri.add_prefix(Gtk.Image.new_from_icon_name("view-refresh-symbolic"))
            ri.connect("activated", lambda _r: self.win.cmd(
                lambda p: p.reindex(), toast="Rescanning — new music shows up in a few minutes"))
            cur.add(ri)
        self.box.append(cur)

        add = Adw.PreferencesGroup(title="Add a NAS or another computer",
                                   description="Enter its shared folder as \\\\Computer\\Share, e.g. \\\\nas\\Music.")
        share = Adw.EntryRow(title="Share path (\\\\Computer\\Share)")
        user = Adw.EntryRow(title="User name (if required)")
        pw = Adw.PasswordEntryRow(title="Password (if required)")
        for w in (share, user, pw):
            add.add(w)
        btn = Gtk.Button(label="Add share", halign=Gtk.Align.END, margin_top=10)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")

        def do_add(_b):
            path = share.get_text().strip().replace("/", "\\")
            if path.startswith("smb:"):
                path = path[4:]
            if not path.startswith("\\\\"):
                path = "\\\\" + path.lstrip("\\")
            if path.count("\\") < 3:
                self.win.toast("Use the form \\\\Computer\\Share")
                return
            self._add_and_verify(btn, "Add share", lambda: path, user.get_text(), pw.get_text())
        btn.connect("clicked", do_add)
        add.add(btn)

        if IN_FLATPAK:  # the sandbox can't install or configure Samba: explain how instead
            ip = local_ip_for(self.win.player.ip)
            local = Adw.PreferencesGroup(
                title="Share a folder from this PC",
                description="1. In your file manager, right-click your music folder → Properties → "
                            "Local Network Share (or install Samba and share it read-only).\n"
                            f"2. Enter it below as \\\\{ip}\\<share name>, e.g. \\\\{ip}\\Music.")
        else:
            local = Adw.PreferencesGroup(
                title="Share a folder from this PC",
                description="Pick your music folder. The app shares it read-only on your network "
                            "(installs Samba if needed — asks for your password once) and adds it to the player.")
            pick = Gtk.Button(label="Choose music folder…", halign=Gtk.Align.START)
            pick.add_css_class("suggested-action")
            pick.add_css_class("pill")
            pick.connect("clicked", lambda _b: self._pick_local_folder(pick))
            local.add(pick)
        self.box.append(local)
        self.box.append(add)

    # -- alarms
    DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")  # player's order
    DURATIONS = ((0, "Until stopped"), (15, "15 minutes"), (30, "30 minutes"), (45, "45 minutes"),
                 (60, "1 hour"), (90, "1.5 hours"), (120, "2 hours"))

    def _days_text(self, days):
        if all(days):
            return "Every day"
        if not any(days):
            return "Once"
        if days == [False] + [True] * 5 + [False]:
            return "Weekdays"
        return ", ".join(d for d, on in zip(self.DAYS, days) if on)

    def _render_alarms(self, alarms):
        self._clear()
        g = Adw.PreferencesGroup(description="The player keeps the alarms — they ring even when this PC is off.")
        if not alarms:
            g.add(Adw.ActionRow(title="No alarms yet"))
        p = self.win.player
        for a in sorted(alarms, key=lambda a: (a["hour"], a["minute"])):
            r = Adw.ActionRow(title=f"{a['hour']:02d}:{a['minute']:02d}",
                              subtitle=GLib.markup_escape_text(
                                  f"{self._days_text(a['days'])} · {a.get('source') or 'Current queue'}"),
                              activatable=True)
            r.add_css_class("property")
            sw = Gtk.Switch(active=a["enable"], valign=Gtk.Align.CENTER)

            def toggled(s, _p, a=a):
                if s.get_active() != a["enable"]:
                    a["enable"] = s.get_active()
                    run_bg(lambda: p.save_alarm(a, local_tz()), None, self.win.error)
            sw.connect("notify::active", toggled)
            r.add_suffix(sw)
            r.connect("activated", lambda _r, a=a: self.open_special("Edit alarm", "__alarm__", dict(a)))
            g.add(r)
        self.box.append(g)
        btn = Gtk.Button(label="Add alarm", halign=Gtk.Align.END)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", lambda _b: self.open_special("New alarm", "__alarm__", {
            "id": 0, "hour": 7, "minute": 0, "days": [False] + [True] * 5 + [False], "duration": 30,
            "volume": 25, "fadein": True, "enable": True, "source": "", "service": "", "url": "", "image": ""}))
        self.box.append(btn)

    def _render_alarm_edit(self, a, sources):
        self._clear()
        when = Adw.PreferencesGroup(title="Time")
        tbox = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        for sp, v in ((hour, a["hour"]), (minute, a["minute"])):
            sp.set_value(v)
            sp.set_wrap(True)
            sp.set_orientation(Gtk.Orientation.VERTICAL)
            sp.add_css_class("title-1")
            sp.connect("output", lambda s: (s.set_text(f"{int(s.get_value()):02d}"), True)[1])
        tbox.append(hour)
        tbox.append(Gtk.Label(label=":", css_classes=["title-1"]))
        tbox.append(minute)
        when.add(tbox)
        dbox = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER, margin_top=6)
        day_btns = []
        for i, d in enumerate(self.DAYS):
            b = Gtk.ToggleButton(label=d, active=a["days"][i])
            b.add_css_class("circular")
            day_btns.append(b)
            dbox.append(b)
        when.add(dbox)
        when.add(Gtk.Label(label="No days selected = rings once", css_classes=["dim-label", "caption"],
                           margin_top=4))
        self.box.append(when)

        what = Adw.PreferencesGroup(title="Sound")
        names = [s["source"] for s in sources]
        cur = next((i for i, s in enumerate(sources) if s["source"] == a.get("source")), 0)
        src = Adw.ComboRow(title="Play", model=Gtk.StringList.new(names), selected=cur)
        what.add(src)
        vol = Adw.ActionRow(title="Volume")
        vs = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        vs.set_value(a["volume"])
        vs.set_size_request(220, -1)
        vs.set_draw_value(True)
        vol.add_suffix(vs)
        what.add(vol)
        fade = Adw.SwitchRow(title="Fade in", subtitle="Start quiet and rise to the volume", active=a["fadein"])
        what.add(fade)
        dur = Adw.ComboRow(title="Stop after", model=Gtk.StringList.new([t for _, t in self.DURATIONS]),
                           selected=next((i for i, (m, _) in enumerate(self.DURATIONS) if m == a["duration"]), 2))
        what.add(dur)
        self.box.append(what)

        btns = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        p = self.win.player
        if a["id"]:
            rm = Gtk.Button(label="Delete")
            rm.add_css_class("destructive-action")
            rm.add_css_class("pill")
            rm.connect("clicked", lambda _b: run_bg(lambda: p.delete_alarm(a["id"]),
                                                     lambda _r: (self.win.toast("Alarm deleted"), self.go_back()),
                                                     self.win.error))
            btns.append(rm)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.add_css_class("pill")

        def do_save(_b):
            s = sources[src.get_selected()] if sources else {}
            a.update(hour=int(hour.get_value()), minute=int(minute.get_value()),
                     days=[b.get_active() for b in day_btns], volume=int(vs.get_value()),
                     fadein=fade.get_active(), duration=self.DURATIONS[dur.get_selected()][0],
                     source=s.get("source", ""), service=s.get("service", ""),
                     url=s.get("url", ""), image=s.get("image", ""))
            if not a["id"]:
                a["enable"] = True
            run_bg(lambda: p.save_alarm(a, local_tz()),
                   lambda _r: (self.win.toast(f"Alarm set for {a['hour']:02d}:{a['minute']:02d}"), self.go_back()),
                   self.win.error)
        save.connect("clicked", do_save)
        btns.append(save)
        self.box.append(btns)

    # -- bluetooth headphones
    def _bt_fetch(self, **action):
        """Everything the Bluetooth page shows: input mode, input active?, headphone devices."""
        p = self.win.player
        mode = next((s for s in p.settings("capture").iter("setting")
                     if s.get("name") == "bluetoothAutoplay"), None)
        info = p.sync_status()
        st = p.status()
        devices = p.bt_devices(**action) if info.get("bluetoothOutput") is not None else None
        url = (st.get("streamUrl") or "").lower()  # e.g. Capture:hw:soundbt,0/44100/16/2
        listening = url.startswith("capture:") and ("bt" in url or "bluez" in url or "bluetooth" in url)
        return mode, listening, devices

    def _bt_refresh(self, scan=False):
        kw = {"refresh": 1} if scan else {}
        run_bg(lambda: self._bt_fetch(**kw), self._render_bt, self.win.error)
        if not self._bt_timer:  # keep the list live while the page is open, like the official app
            self._bt_timer = GLib.timeout_add_seconds(5, self._bt_tick)

    def _bt_tick(self):
        if self.path[-1][1] != "__bt__" or self.win.pages.get_visible_child_name() != "settings":
            self._bt_timer = None
            return False
        run_bg(self._bt_fetch, self._render_bt, lambda e: None)
        return True

    def _bt_op(self, msg, **action):
        self.win.toast(msg)
        run_bg(lambda: self._bt_fetch(**action), self._render_bt, self.win.error)

    def _render_bt(self, res):
        if self.path[-1][1] != "__bt__":
            return
        mode, listening, devices = res
        pos = self.scroller.get_vadjustment().get_value()
        self._clear()
        name = self.win.players.get(self.win.player.key, {}).get("name", "the player")

        # -- phone → player (Bluetooth input)
        inp = Adw.PreferencesGroup(
            title="Play from your phone",
            description=f"On your phone, open Bluetooth settings and connect to “{name}”, then play any music. "
                        "The sound comes out of the player.\n"
                        "Not in your phone's list? Pairing starts on the player itself — see its manual "
                        "(usually: hold the Bluetooth/pairing button until the light flashes blue).")
        state = Adw.ActionRow(title="Bluetooth input",
                              subtitle="Active — waiting for or playing from your phone" if listening
                              else "Not active — the player is playing something else")
        state.add_prefix(Gtk.Image.new_from_icon_name("phone-symbolic"))
        if not listening:
            go = Gtk.Button(label="Switch to Bluetooth", valign=Gtk.Align.CENTER)
            go.add_css_class("suggested-action")
            go.connect("clicked", lambda _b: (self.win.cmd(lambda p: p.play_input("bluetooth-1")),
                                              GLib.timeout_add_seconds(2, lambda: self._bt_tick() and False)))
            state.add_suffix(go)
        inp.add(state)
        if mode is not None:
            opts = [(v.get("name"), v.get("displayName")) for v in mode.findall("value")]
            helptext = {"0": "You switch to Bluetooth yourself. If Bluetooth disappears after changing this, "
                             "restart the player (Settings → Reboot player)",
                        "1": "Switches to Bluetooth when your phone starts playing",
                        "2": "Like automatic, but disconnects the phone when you pick another source",
                        "3": "Phones can't connect"}
            cur = next((i for i, (v, _) in enumerate(opts) if v == mode.get("value")), 0)
            mr = Adw.ComboRow(title="When a phone connects",
                              subtitle=helptext.get(mode.get("value"), ""),
                              model=Gtk.StringList.new([t for _, t in opts]), selected=cur)

            def changed(r, _p):
                v = opts[r.get_selected()][0]
                if v != mode.get("value"):
                    mode.set("value", v)
                    r.set_subtitle(helptext.get(v, ""))
                    self._write(mode.get("url") or "/audiomodes", "bluetoothAutoplay", v)
            mr.connect("notify::selected", changed)
            inp.add(mr)
        self.box.append(inp)

        if devices is None:  # player can't send to headphones
            GLib.idle_add(lambda: self.scroller.get_vadjustment().set_value(pos) and False)
            return
        mine = [d for d in devices if "true" in (d.get("paired"), d.get("connecting"), d.get("connected"))]
        near = sorted((d for d in devices if d not in mine),
                      key=lambda d: (not d.get("name"), (d.get("name") or d.get("id", "")).lower()))
        intro = Adw.PreferencesGroup(
            title="Listen on Bluetooth headphones",
            description="Put your headphones in pairing mode, then pick them below. "
                        "While connected, the player's sound goes to the headphones.")
        self.box.append(intro)

        def row(d):
            name = d.get("name") or d.get("id", "")
            state = ("Connected" if d.get("connected") == "true" else
                     "Connecting…" if d.get("connecting") == "true" else
                     "Paired" if d.get("paired") == "true" else "")
            r = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=state)
            r.add_prefix(Gtk.Image.new_from_icon_name("audio-headphones-symbolic"))
            return r

        g = Adw.PreferencesGroup(title="My headphones")
        if not mine:
            g.add(Adw.ActionRow(title="None paired yet"))
        for d in mine:
            r = row(d)
            if d.get("connected") == "true":
                b = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda _b: self._bt_op("Disconnecting…", disconnect=1))
            else:
                b = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
                b.add_css_class("suggested-action")
                b.connect("clicked", lambda _b, d=d: self._bt_op("Connecting…", connect=d["id"]))
            r.add_suffix(b)
            fg = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Forget this device",
                            valign=Gtk.Align.CENTER)
            fg.add_css_class("flat")
            fg.connect("clicked", lambda _b, d=d: self._confirm(
                "Forget this device?", "You'll need to pair it again to use it.", "Forget",
                lambda: self._bt_op("Forgetting…", unpair=d["id"])))
            r.add_suffix(fg)
            g.add(r)
        self.box.append(g)

        n = Adw.PreferencesGroup(title="Nearby devices")
        scan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Scan again", valign=Gtk.Align.CENTER)
        scan.add_css_class("flat")
        scan.connect("clicked", lambda _b: self._bt_op("Scanning…", refresh=1))
        n.set_header_suffix(scan)
        if not near:
            n.add(Adw.ActionRow(title="Searching…", subtitle="Make sure the headphones are in pairing mode"))
        for d in near:
            r = row(d)
            r.set_activatable(True)
            r.add_suffix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
            r.connect("activated", lambda _r, d=d: self._bt_op("Pairing…", connect=d["id"]))
            n.add(r)
        self.box.append(n)
        GLib.idle_add(lambda: self.scroller.get_vadjustment().set_value(pos) and False)

    def _add_and_verify(self, btn, label, make_path, username="", password=""):
        """Run make_path() (may do setup), add the share to the player, poll until it's listed."""
        btn.set_sensitive(False)
        btn.set_label("Working… (can take a minute)")
        p = self.win.player

        def work():
            path = make_path()
            reply = p.add_share(path, username, password)
            for _ in range(18):  # player remounts shares; list is briefly empty
                if any(s.lower() == path.lower() for s, _ in p.shares()):
                    return path, True, reply
                time.sleep(5)
            return path, False, reply

        def done(res):
            path, ok, reply = res
            btn.set_sensitive(True)
            btn.set_label(label)
            if ok:
                self.win.toast(f"Added {path} — indexing starts automatically")
                self.win.refresh_sources()
            else:
                self._confirm_info("The player could not open the share",
                                   f"{path}\n\nPlayer said: {reply[:200] or '(nothing)'}\n\n"
                                   "Check the computer is on, the share name is right, and the user/password if one is needed.")
            self.reload()

        def fail(e):
            btn.set_sensitive(True)
            btn.set_label(label)
            self.win.error(e)
        run_bg(work, done, fail)

    def _pick_local_folder(self, btn):
        dlg = Gtk.FileDialog(title="Choose your music folder", modal=True)
        start = Path("/media") / os.environ.get("USER", "")
        dlg.set_initial_folder(Gio.File.new_for_path(str(start if start.is_dir() else Path.home())))

        def chosen(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return  # cancelled
            if folder:
                self._share_local(btn, folder.get_path())
        dlg.select_folder(self.win, None, chosen)

    def _share_local(self, btn, folder):
        import re
        import subprocess
        name = re.sub(r"[^A-Za-z0-9_-]", "", Path(folder).name.replace(" ", "_"))[:40] or "Music"
        user = os.environ.get("USER") or Path.home().name
        helper = Path("/usr/lib/nodeplay/nodeplay-share-helper")
        if not helper.exists():  # running from the source tree
            helper = Path(__file__).resolve().parent.parent / "packaging" / "nodeplay-share-helper"
        player_ip = self.win.player.ip

        def setup():
            # a per-user share with the same name would shadow ours
            subprocess.run(["net", "usershare", "delete", name], capture_output=True)
            r = subprocess.run(["pkexec", str(helper), "add", folder, name, user], capture_output=True, text=True)
            if r.returncode in (126, 127):
                raise BluOSError("Cancelled — no permission to share the folder")
            if r.returncode != 0:
                raise BluOSError((r.stderr or r.stdout).strip() or f"share helper failed ({r.returncode})")
            return f"\\\\{local_ip_for(player_ip)}\\{name}"
        self._add_and_verify(btn, "Choose music folder…", setup)

    def _confirm_info(self, heading, body):
        dlg = Adw.MessageDialog(transient_for=self.win, heading=heading, body=body)
        dlg.add_response("ok", "OK")
        dlg.present()

    def _share_op(self, fn, msg):
        p = self.win.player
        run_bg(lambda: fn(p), lambda _r: (self.win.toast(msg), self.reload(), self.win.refresh_sources()),
               self.win.error)

    def _device_card(self, info):
        g = Adw.PreferencesGroup(title=info.get("name", ""),
                                 description=f"{info.get('brand', '')} {info.get('modelName', '')}".strip())
        for label, key in (("Model", "model"), ("Firmware", "version"), ("Address", "id"), ("MAC", "mac"),
                           ("Group", "group")):
            if info.get(key):
                r = Adw.ActionRow(title=label, subtitle=GLib.markup_escape_text(str(info[key])))
                r.set_subtitle_selectable(True)
                g.add(r)
        return g

    def _depends_ok(self, el, values):
        return all(values.get(d.get("name")) == d.get("value") for d in el.findall("dependsOn"))

    def _row(self, el, base_url, values):
        name, cls = el.get("name"), el.get("class")
        title = GLib.markup_escape_text(el.get("displayName", ""))
        url = el.get("url") or base_url
        sub = GLib.markup_escape_text(el.get("explanation") or el.get("description") or "")

        if el.tag == "menuGroup":
            # menuGroups whose children are already here and small are shown inline as a sub-section
            r = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(el.get("description") or ""),
                              activatable=True)
            r.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            r.connect("activated", lambda _r: self.open(el, url))
            self._icon(r, el)
            return r

        ok = self._depends_ok(el, values)
        if not ok and el.get("hideIfDisabled") == "true":
            return None
        row = None
        if el.get("id") == "sharecfg":  # native editor instead of the player's web form
            row = Adw.ActionRow(title=title, subtitle=sub or "Music folders on your network", activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r: self.open_shares())
        elif el.find("webview") is not None:
            link = el.find("webview").get("url")
            row = Adw.ActionRow(title=title, subtitle=sub or "Opens the player's web page", activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
            row.connect("activated", lambda _r, t=el.get("displayName", ""): self.win.open_web(link, t))
        elif cls == "boolean":
            opts = el.findall("value")
            cur = el.get("value") or ""
            disp = next((v.get("displayName") for v in opts if v.get("name") == cur), cur)
            row = Adw.SwitchRow(title=title, subtitle=sub, active=(disp or "").lower() == "on")

            def toggled(r, _p, el=el, opts=opts, url=url):
                if opts:
                    want = "on" if r.get_active() else "off"
                    val = next((v.get("name") for v in opts if (v.get("displayName") or "").lower() == want),
                               opts[-1 if r.get_active() else 0].get("name"))
                else:
                    val = "ON" if r.get_active() else "OFF"
                self._write(url, el.get("name"), val)
            row.connect("notify::active", toggled)
        elif cls in ("list", "iconlist"):
            opts = el.findall("value")
            def nice(v):
                if v.get("displayName"):
                    return v.get("displayName")
                n = v.get("name") or ""
                if "/" in n:  # iconlist: /images/capture/ic_tv.png -> Tv
                    n = n.rsplit("/", 1)[-1].removesuffix(".png").removeprefix("ic_").replace("_", " ").title()
                return n
            labels = Gtk.StringList.new([nice(v) for v in opts])
            row = Adw.ComboRow(title=title, subtitle=sub, model=labels)
            idx = next((i for i, v in enumerate(opts) if v.get("name") == el.get("value")), 0)
            row.set_selected(idx)
            row.connect("notify::selected", lambda r, _p, el=el, opts=opts, url=url:
                        self._write(url, el.get("name"), opts[r.get_selected()].get("name")))
        elif cls == "range":
            v = el.find("value")
            lo, hi = float(v.get("min", 0)), float(v.get("max", 100))
            step = float(v.get("step") or 1)
            units = v.get("units") or ""
            row = Adw.ActionRow(title=title, subtitle=sub)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
            scale.set_size_request(260, -1)
            scale.set_draw_value(True)
            scale.set_digits(1 if step < 1 else 0)
            scale.set_value_pos(Gtk.PositionType.LEFT)
            scale.set_format_value_func(lambda _s, x, u=units: f"{x:g} {u}".strip())
            scale.set_value(float(el.get("value") or lo))
            scale.set_valign(Gtk.Align.CENTER)
            self._debounced(scale, lambda s, el=el, url=url, step=step:
                            self._write(url, el.get("name"), f"{round(s.get_value() / step) * step:g}"))
            row.add_suffix(scale)
        elif cls == "dual-range":
            v = el.find("value")
            lo, hi = float(v.get("min", -90)), float(v.get("max", 0))
            min_range = float(v.get("minRange") or 0)
            cur = [float(x) for x in (el.get("value") or f"{lo},{hi}").split(",")]
            row = Adw.ActionRow(title=title, subtitle=sub or f"Lowest and highest volume ({v.get('units', '')})")
            box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
            a = Gtk.SpinButton.new_with_range(lo, hi, 1)
            b = Gtk.SpinButton.new_with_range(lo, hi, 1)
            a.set_value(cur[0])
            b.set_value(cur[-1])

            def send(_w, el=el, url=url):
                x, y = a.get_value(), b.get_value()
                if y - x < min_range:
                    self.win.toast(f"Range must be at least {min_range:g} dB")
                    return
                self._write(url, el.get("name"), f"{x:g},{y:g}")
            for w in (a, b):
                self._debounced(w, send)
            box.append(a)
            box.append(Gtk.Label(label="to"))
            box.append(b)
            row.add_suffix(box)
        elif cls == "text":
            row = Adw.EntryRow(title=title, text=el.get("value") or "", show_apply_button=True)
            pattern = el.get("pattern")

            def apply(r, el=el, url=url, pattern=pattern):
                import re
                txt = r.get_text()
                if pattern and not re.fullmatch(pattern, txt):
                    self.win.toast(el.get("patternError") or "Invalid value")
                    return
                self._write(url, el.get("name"), txt, refresh_players=el.get("id") == "nodename")
            row.connect("apply", apply)
        elif cls == "button":
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            row.connect("activated", lambda _r, el=el, url=url: self._confirm(
                f"{el.get('displayName')}?", el.get("description") or "", el.get("displayName"),
                lambda: self._write(url, el.get("name"), 1)))
        elif cls == "sleep":
            mins = el.get("sleep")
            row = Adw.ActionRow(title=title, subtitle=f"{mins} minutes left" if mins else "Off",
                                activatable=True)
            step_btn = Gtk.Button(label="Next step", valign=Gtk.Align.CENTER, tooltip_text="Off → 15 → 30 → 45 → 60 → 90 → Off")
            step_btn.connect("clicked", lambda _b: run_bg(lambda: self.win.player.sleep_timer(),
                                                          lambda _m: self.reload(keep_scroll=True), self.win.error))
            row.add_suffix(step_btn)
        elif cls == "alarms":
            n = int(el.get("count") or 0)
            row = Adw.ActionRow(title=title, subtitle=f"{n} set" if n else "None set — add one", activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r: self.open_special("Alarms", "__alarms__"))
        else:
            return None
        self._icon(row, el)
        if not ok:
            row.set_sensitive(False)
        return row

    def _icon(self, row, el):
        if el.get("icon") and self.win.player:
            img = thumb(28)
            IMAGES.set(img, self.win.player.url(el.get("icon")))
            row.add_prefix(img)

    @staticmethod
    def _debounced(widget, fn, ms=500):
        state = {"t": None}

        def changed(w):
            if state["t"]:
                GLib.source_remove(state["t"])

            def fire():
                state["t"] = None
                fn(w)
                return False
            state["t"] = GLib.timeout_add(ms, fire)
        widget.connect("value-changed", changed)

    def _write(self, url, name, value, refresh_players=False):
        if not url or not name:
            self.win.toast("This setting can't be changed from here")
            return
        p = self.win.player

        def done(_r):
            self.win.toast("Saved")
            self.reload(keep_scroll=True)
            if refresh_players:
                self.win.rename_current()
        run_bg(lambda: p.set_setting(url, name, value), done, self.win.error)

    def _confirm(self, heading, body, action, fn):
        dlg = Adw.MessageDialog(transient_for=self.win, heading=heading, body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("go", action)
        dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "go" and fn())
        dlg.present()


# ---------------------------------------------------------------- home
def card(win, title, image, on_click, subtitle=None, size=150, on_play=None, service_icon=None):
    """Artwork tile like the official app: cover, round play button on it, title + service icon."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.START,
                    valign=Gtk.Align.START)
    outer.add_css_class("card")
    ov = Gtk.Overlay()
    btn = Gtk.Button()
    btn.add_css_class("flat")
    btn.set_has_frame(False)
    pic = thumb(size)  # fixed-size image: Gtk.Picture would grow with its container
    pic.add_css_class("art")
    IMAGES.set(pic, win.player.url(image) if image and win.player else None)
    btn.set_child(pic)
    btn.connect("clicked", lambda _b: on_click())
    ov.set_child(btn)
    if on_play:
        dot = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play",
                         halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=6, margin_bottom=6)
        dot.add_css_class("playdot")
        dot.connect("clicked", lambda _b: on_play())
        ov.add_overlay(dot)
    outer.append(ov)
    line = Gtk.Box(spacing=6)
    lbl = Gtk.Label(label=title, xalign=0, ellipsize=3, max_width_chars=max(8, size // 10), hexpand=True)
    lbl.add_css_class("card-label")
    line.append(lbl)
    if service_icon and win.player:
        ic = Gtk.Image(pixel_size=16)
        ic.add_css_class("svc-icon")
        IMAGES.set(ic, win.player.url(service_icon))
        line.append(ic)
    outer.append(line)
    if subtitle:
        sl = Gtk.Label(label=subtitle, xalign=0, ellipsize=3, max_width_chars=max(8, size // 10))
        sl.add_css_class("caption")
        sl.add_css_class("dim-label")
        outer.append(sl)
    outer.set_size_request(size + 24, -1)
    outer.set_hexpand(False)  # the ellipsizing label must not make tiles grab spare row width
    return outer


def ui_play_uri(el):
    """Best 'play this' link for a server-driven UI item (/ui/... screens)."""
    pa = el.find("playAction")
    if pa is not None and pa.get("URI"):
        return pa.get("URI")
    a = el.find("action")
    if a is not None and a.get("type") == "player-link":
        return a.get("URI")
    cm = el.find("contextMenu")
    if cm is not None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(cm.get("URI", "")).query)
        if q.get("preset_url"):
            return q["preset_url"][0]
    return None


def ui_browse_key(uri, service=None):
    """Map a /ui deep-link or browse URI to a /Browse key our Browse page understands."""
    if not uri:
        return None
    if uri.startswith("/music-service/"):
        parts = uri.split("/")[2:]  # [service, 'menuGroup', name]
        if len(parts) >= 3 and parts[1] == "menuGroup":
            return f"{parts[0]}:MG/{parts[2]}"
        return f"{parts[0]}:"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
    svc = (q.get("service") or [service])[0]
    return f"{svc}:" if svc else None


class HomePage(Gtk.ScrolledWindow):
    """Renders the player's own Home screen (/ui/Home) — Most Used, Presets, Recent Stations, Recently Played."""

    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win = win
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22,
                           margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        self.set_child(self.box)
        self._loading = False

    def set_presets(self, _presets):
        pass  # presets come with /ui/Home now

    def reload(self):
        if self._loading or not self.win.player:
            return
        self._loading = True
        p = self.win.player

        def done(root):
            self._loading = False
            self.render(root)

        def fail(e):
            self._loading = False
            self.win.error(e)
        run_bg(lambda: p.get("/ui/Home"), done, fail)

    render_screen = None

    def _shelf(self, title, cards, actions=(), solid=False, arrows=True):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.add_css_class("shelf")
        if solid:
            outer.add_css_class("solid")
        head = Gtk.Box(spacing=8)
        t = Gtk.Label(label=title, xalign=0, hexpand=True)
        t.add_css_class("row-title")
        head.append(t)
        sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        if arrows and len(cards) > 3:
            for icon, d in (("go-previous-symbolic", -1), ("go-next-symbolic", 1)):
                a = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER)
                a.add_css_class("circular")
                a.add_css_class("row-arrow")
                a.connect("clicked", lambda _b, d=d: self._scroll(sc, d))
                head.append(a)
        for label, cb in actions:
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.add_css_class("flat" if solid else "pill")
            b.connect("clicked", lambda _b, cb=cb: cb())
            head.append(b)
        outer.append(head)
        row = Gtk.Box(spacing=12)
        for c in cards:
            row.append(c)
        sc.set_child(row)
        outer.append(sc)
        return outer

    @staticmethod
    def _scroll(sc, direction):
        adj = sc.get_hadjustment()
        target = min(max(adj.get_value() + direction * adj.get_page_size() * 0.8, 0),
                     adj.get_upper() - adj.get_page_size())
        anim = Adw.TimedAnimation.new(sc, adj.get_value(), target, 250,
                                      Adw.CallbackAnimationTarget.new(lambda v: adj.set_value(v)))
        anim.play()

    def item_card(self, el, size=150):
        w = self.win
        title = el.get("title") or ""
        sub = el.get("subTitle") or el.get("subSubTitle")
        uri = ui_play_uri(el)
        play = (lambda: w.cmd(lambda p: p.action(uri), toast=f"Playing {title}")) if uri else None
        a = el.find("action")
        if a is not None and a.get("type") != "player-link":
            click = lambda: self._do_action(a, title)
        else:
            click = play or (lambda: None)
        is_thumb = el.tag == "smallThumbnail"
        return card(w, title, el.get("image") or el.get("icon"), click, subtitle=sub, size=size,
                    on_play=None if is_thumb else play,
                    service_icon=None if is_thumb else (el.get("icon") if el.get("image") else None))

    def _do_action(self, a, title=""):
        w = self.win
        if a is None:
            return
        typ, uri = a.get("type"), a.get("URI")
        if typ == "player-link":
            w.cmd(lambda p: p.action(uri), toast=title and f"Playing {title}")
            if a.get("refreshScreen") == "true":
                GLib.timeout_add(800, lambda: (self.reload(), False)[1])
        elif typ == "setting":
            w.show_settings()
        elif typ == "webpage":
            w.open_web(w.player.url(uri), "Music Services" if "services" in uri else "")
        elif uri and uri.startswith("/ui/presets"):
            w.show_page("presets")
        elif uri and uri.startswith("/ui/RecentlyPlayed"):
            w.show_recent()
        elif uri and uri.startswith("/ui/Favourites"):
            self._open_favourites(a.get("service"), a.get("title") or title)
        else:
            key = ui_browse_key(uri, a.get("service"))
            if key:
                w.browse_to(key, a.get("title") or title)

    def _open_favourites(self, service, title):
        """A service's Favourites button: find its favourites entry in /Browse (TuneIn 'Favorites',
        Tidal 'My Music'…) instead of landing on the service's front page."""
        w, p = self.win, self.win.player

        def find():
            items = [it for _h, its in p.browse(f"{service}:")["sections"] for it in its]
            names = {title.lower(), "favourites", "favorites", "my music"}
            hit = next((it for it in items if it.get("text", "").lower() in names), None) or \
                next((it for it in items if "favourite" in (it.get("browseKey") or "").lower()
                      or "url=presets" in (it.get("browseKey") or "")), None)
            return hit.get("browseKey") if hit else f"{service}:"
        run_bg(find, lambda key: w.browse_to(key, title), w.error)

    def _source_card(self, src):
        w = self.win
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("card")
        logo = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        logo.set_size_request(150, 44)
        IMAGES.set(logo, w.player.url(src.get("icon")) if src.get("icon") else None)
        box.remove_css_class("card")
        box.add_css_class("tile")
        box.set_size_request(170, -1)
        top = Gtk.Button()
        top.add_css_class("flat")
        top.set_child(logo)
        top.connect("clicked", lambda _b: self._do_action(src.find("action"), src.get("title") or ""))
        box.append(top)
        if src.get("title"):
            box.append(Gtk.Label(label=src.get("title")))
        for b in src.findall("button"):
            btn = Gtk.Button(label=b.get("text", ""))
            btn.add_css_class("src-btn")
            btn.add_css_class(ColorCSS.cls(b.get("backgroundColor") or "#292d2f", b.get("textColor") or "#ffffff"))
            btn.connect("clicked", lambda _b, b=b: self._do_action(b.find("action"), src.get("title") or ""))
            box.append(btn)
        return box

    def render(self, root):
        while (c := self.box.get_first_child()):
            self.box.remove(c)
        head = Gtk.Box()
        title = Gtk.Label(label=(root.get("screenTitle") if root is not None else None) or "Home", xalign=0,
                          hexpand=True)
        title.add_css_class("page-title")
        head.append(title)
        gear = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings", valign=Gtk.Align.START)
        gear.add_css_class("flat")
        gear.add_css_class("home-gear")
        gear.connect("clicked", lambda _b: self.win.show_settings())
        head.append(gear)
        self.box.append(head)
        if root is None:
            return
        for row in root.iter("row"):
            rid = row.get("id")
            if rid == "teaser":
                continue
            actions = []
            for ma in row.findall("menuAction"):
                a = ma.find("action")
                actions.append((ma.get("text") or "More", lambda a=a: self._do_action(a)))
            if rid == "mostUsed" or row.find("source") is not None:
                cards = [self._source_card(s) for s in row.findall("source")]
            else:
                cards = [self.item_card(el, size=110 if el.tag == "smallThumbnail" else 150)
                         for el in row if el.tag.endswith("Thumbnail") or el.tag == "item"]
            if cards:
                self.box.append(self._shelf(row.get("title") or "", cards, actions,
                                            solid=row.get("solidBackground") == "true",
                                            arrows=row.get("scrollable") == "true"))


class RecentPage(Gtk.ScrolledWindow):
    """'View All' for Recently Played — /ui/RecentlyPlayed as a grid."""

    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win = win
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                      margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        head = Gtk.Box()
        t = Gtk.Label(label="Recently Played", xalign=0, hexpand=True)
        t.add_css_class("page-title")
        clear = Gtk.Button(label="Clear")
        clear.add_css_class("pill")
        clear.connect("clicked", lambda _b: self._clear())
        head.append(t)
        head.append(clear)
        box.append(head)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                max_children_per_line=30, min_children_per_line=2,
                                row_spacing=12, column_spacing=12, valign=Gtk.Align.START)
        box.append(self.flow)
        self.set_child(box)

    def reload(self):
        p = self.win.player
        run_bg(lambda: p.get("/ui/RecentlyPlayed"), self._fill, self.win.error)

    def _fill(self, root):
        self.flow.remove_all()
        if root is None:
            return
        for el in root.iter("item"):
            self.flow.append(self.win.home.item_card(el))

    def _clear(self):
        p = self.win.player
        run_bg(lambda: p.get("/ui/clearUsageHistory", {"queued": 1}),
               lambda _r: (self.reload(), self.win.home.reload()), self.win.error)


class WebPage(Gtk.Box):
    """Player-served web pages, shown inside the app like the official controller does."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        back.connect("clicked", lambda _b: self.view and self.view.can_go_back() and self.view.go_back())
        self.title = Gtk.Label(xalign=0, hexpand=True)
        self.title.add_css_class("title-2")
        ext = Gtk.Button(icon_name="adw-external-link-symbolic", tooltip_text="Open in browser")
        ext.connect("clicked", lambda _b: self.view and Gtk.UriLauncher.new(self.view.get_uri()).launch(
            self.win, None, None, None))
        for w in (back, self.title, ext):
            bar.append(w)
        self.append(bar)
        self.view = None
        try:
            # WebKitGTK's web process aborts on NVIDIA's EGL ("Could not create default EGL display").
            # These only affect child processes spawned from here on (GTK already has its GL context),
            # and the player's settings pages are simple enough for software rendering.
            os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
            os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
            os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
            mesa = "/usr/share/glvnd/egl_vendor.d/50_mesa.json"
            if os.path.exists(mesa):
                os.environ.setdefault("__EGL_VENDOR_LIBRARY_FILENAMES", mesa)
            gi.require_version("WebKit", "6.0")
            from gi.repository import WebKit
            self.view = WebKit.WebView(vexpand=True, hexpand=True)
            self.view.set_background_color(Gdk.RGBA(red=0.12, green=0.13, blue=0.14, alpha=1))
            self.append(self.view)
        except (ValueError, ImportError):
            self.append(Adw.StatusPage(icon_name="web-browser-symbolic", title="Web view not available",
                                       description="Install gir1.2-webkit-6.0, or use “Open in browser”."))

    def open(self, url, title):
        self.title.set_label(title)
        if self.view:
            self.view.load_uri(url)
        else:
            Gtk.UriLauncher.new(url).launch(self.win, None, None, None)


class GroupPopover(Gtk.Popover):
    """Group this player with others (doc 8: /AddSlave, /RemoveSlave)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_bottom=10,
                           margin_start=10, margin_end=10)
        self.set_child(self.box)
        self.box.append(Gtk.Spinner(spinning=True))
        p = win.player
        run_bg(p.sync_status, self._fill, lambda e: self._fill(None))

    def _fill(self, info):
        while (c := self.box.get_first_child()):
            self.box.remove(c)
        w = self.win
        head = Gtk.Label(label="Play on these players together", xalign=0)
        head.add_css_class("heading")
        self.box.append(head)
        me = w.player.key
        if info and info.get("master"):
            self.box.append(Gtk.Label(label=f"This player is grouped under {info['master']}.\n"
                                            "Pick the primary player to change the group.", xalign=0))
            leave = Gtk.Button(label="Leave group")
            ip, _, port = info["master"].partition(":")
            leave.connect("clicked", lambda _b: (self.popdown(), w.cmd(
                lambda _p: Player(ip, port or 11000).remove_slaves([(w.player.ip, w.player.port)]),
                toast="Left the group")))
            self.box.append(leave)
            return
        slaves = set((info or {}).get("slaves", []))
        others = [k for k in w.players if k != me]
        if not others:
            self.box.append(Gtk.Label(label="No other BluOS players found on your network.", xalign=0))
            return
        self.checks = {}
        for k in others:
            cb = Gtk.CheckButton(label=w.players[k].get("name", k), active=k in slaves)
            self.checks[k] = cb
            self.box.append(cb)
        apply = Gtk.Button(label="Apply")
        apply.add_css_class("suggested-action")
        apply.connect("clicked", lambda _b: self._apply(slaves))
        self.box.append(apply)

    def _apply(self, current):
        w = self.win
        want = {k for k, cb in self.checks.items() if cb.get_active()}
        add = [tuple(k.split(":")) for k in want - current]
        rem = [tuple(k.split(":")) for k in current - want]
        self.popdown()

        def work(p):
            if rem:
                p.remove_slaves([(ip, int(port)) for ip, port in rem])
            if add:
                p.add_slaves([(ip, int(port)) for ip, port in add])
        w.cmd(work, toast="Group updated")


class PlayerBar(Gtk.Box):
    """Bottom now-playing strip, like the official app."""

    def __init__(self, win):
        super().__init__(spacing=14)
        self.add_css_class("playerbar")
        self.win = win

        left = Gtk.Button(hexpand=True)
        left.add_css_class("flat")
        left.set_tooltip_text("Show now playing")
        left.connect("clicked", lambda _b: win.show_page("now"))
        lb = Gtk.Box(spacing=12)
        self.art = thumb(76)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.t1 = Gtk.Label(xalign=0, ellipsize=3, max_width_chars=40)
        self.t2 = Gtk.Label(xalign=0, ellipsize=3, max_width_chars=40)
        self.t2.add_css_class("caption")
        self.t2.add_css_class("dim-label")
        tb.append(self.t1)
        tb.append(self.t2)
        lb.append(self.art)
        lb.append(tb)
        left.set_child(lb)
        self.append(left)

        mid = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.prev = Gtk.Button(icon_name="media-skip-backward-symbolic")
        self.play = Gtk.Button(icon_name="media-playback-pause-symbolic")
        self.next = Gtk.Button(icon_name="media-skip-forward-symbolic")
        self.shuf = Gtk.Button(icon_name="media-playlist-shuffle-symbolic", tooltip_text="Shuffle")
        self.rep = Gtk.Button(icon_name="media-playlist-repeat-symbolic", tooltip_text="Repeat")
        self.shuf.connect("clicked", lambda _b: win.cmd(lambda p: p.shuffle(win.now.status.get("shuffle") != 1)))
        self.rep.connect("clicked", lambda _b: win.now._on_repeat(None))
        for b in (self.shuf, self.prev, self.play, self.next, self.rep):
            b.add_css_class("flat")
            b.add_css_class("circular")
            mid.append(b)
        self.play.remove_css_class("flat")
        self.play.add_css_class("bar-round")
        self.prev.connect("clicked", lambda _b: win.now._on_back(None))
        self.play.connect("clicked", lambda _b: win.now._on_playpause(None))
        self.next.connect("clicked", lambda _b: win.now._on_next(None))
        mid.set_halign(Gtk.Align.CENTER)

        # seek bar under the transport buttons
        seek = Gtk.Box(spacing=8)
        self.pos = Gtk.Label(label="--:--", width_chars=7, xalign=1)
        self.len = Gtk.Label(label="--:--", width_chars=7, xalign=0)
        for t in (self.pos, self.len):
            t.add_css_class("caption")
            t.add_css_class("numeric")
        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.set_draw_value(False)
        self.seek.set_size_request(420, -1)
        self._seek_lock = False
        self.seek.connect("change-value", self._on_seek)
        seek.append(self.pos)
        seek.append(self.seek)
        seek.append(self.len)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, margin_top=4, margin_bottom=4)
        center.append(mid)
        center.append(seek)
        self.append(center)
        GLib.timeout_add_seconds(1, self._tick)

        right = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER, hexpand=True, halign=Gtk.Align.END, margin_end=12)
        self.player_lbl = Gtk.MenuButton(icon_name="audio-speakers-symbolic")
        self.player_lbl.add_css_class("pill")
        self.player_lbl.set_tooltip_text("Group players")
        self.player_lbl.set_create_popup_func(lambda mb: mb.set_popover(GroupPopover(win)))
        right.append(self.player_lbl)

        self.vol_btn = Gtk.MenuButton(icon_name="audio-volume-high-symbolic", tooltip_text="Volume")
        self.vol_btn.add_css_class("circular")
        pop = Gtk.Popover()
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, 0, 100, 1)
        self.vol.set_inverted(True)
        self.vol.set_size_request(-1, 180)
        self._vol_lock = False
        self._vol_timer = None
        self.vol.connect("value-changed", self._on_vol)
        self.fixed = Gtk.Label(label="Fixed volume\n(use your amp)", justify=Gtk.Justification.CENTER)
        pb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        up = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="+2 dB")
        down = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="−2 dB")
        up.connect("clicked", lambda _b: win.cmd(lambda p: p.volume_step(2, self.group.get_active())))
        down.connect("clicked", lambda _b: win.cmd(lambda p: p.volume_step(-2, self.group.get_active())))
        self.mute = Gtk.ToggleButton(icon_name="audio-volume-muted-symbolic", tooltip_text="Mute")
        self.mute.connect("clicked", lambda b: win.cmd(lambda p: p.set_mute(b.get_active())))
        self.group = Gtk.CheckButton(label="Whole group", tooltip_text="Change volume on all grouped players",
                                     visible=False)
        pb.append(up)
        pb.append(self.vol)
        pb.append(down)
        pb.append(self.mute)
        pb.append(self.group)
        pb.append(self.fixed)
        pop.set_child(pb)
        self.vol_btn.set_popover(pop)
        right.append(self.vol_btn)

        q = Gtk.Button(icon_name="view-list-symbolic", tooltip_text="Queue")
        q.add_css_class("circular")
        q.connect("clicked", lambda _b: win.show_page("queue"))
        right.append(q)
        self.append(right)

    def _tick(self):
        st = self.win.now.status
        if not st:
            return True
        cur, tot = self.win.now._cur_secs(), st.get("totlen")
        self.pos.set_label(fmt_time(cur))
        self.len.set_label(fmt_time(tot) if tot else ("LIVE" if st.get("streamUrl") else "--:--"))
        self.seek.set_sensitive(bool(st.get("canSeek")) and bool(tot))
        self._seek_lock = True
        self.seek.set_range(0, max(tot or 1, 1))
        self.seek.set_value(cur if (cur is not None and tot) else 0)
        self._seek_lock = False
        return True

    def _on_seek(self, _scale, _scroll, value):
        if self._seek_lock or not self.win.now.status.get("canSeek"):
            return False
        self.win.cmd(lambda p: p.play(seek=max(0, int(value))))
        return False

    def set_player_name(self, name):
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image.new_from_icon_name("audio-speakers-symbolic"))
        box.append(Gtk.Label(label=name))
        self.player_lbl.set_child(box)

    def update(self, s):
        self.t1.set_label(s.get("title1") or "Nothing playing")
        self.t2.set_label(s.get("title2") or "")
        img = s.get("image") or s.get("stationImage")
        IMAGES.set(self.art, self.win.player.url(img) if img and self.win.player else None)
        playing = s.get("state") in ("play", "stream", "connecting")
        self.play.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        stream = bool(s.get("streamUrl"))
        self.shuf.set_sensitive(not stream)
        self.rep.set_sensitive(not stream)
        self.shuf.set_opacity(1.0 if s.get("shuffle") == 1 else 0.5)
        self.rep.set_opacity(0.5 if s.get("repeat") in (2, None) else 1.0)
        self.rep.set_icon_name("media-playlist-repeat-song-symbolic" if s.get("repeat") == 1
                               else "media-playlist-repeat-symbolic")
        self.prev.set_sensitive(self.win.now.back_btn.get_sensitive())
        self.next.set_sensitive(self.win.now.next_btn.get_sensitive())
        vol = s.get("volume")
        fixed = vol is None or vol < 0
        self.vol.set_visible(not fixed)
        self.fixed.set_visible(fixed)
        if not fixed and self._vol_timer is None:
            self._vol_lock = True
            self.vol.set_value(vol)
            self._vol_lock = False
        self.vol_btn.set_icon_name("audio-volume-muted-symbolic" if s.get("mute") == 1 else "audio-volume-high-symbolic")
        self.group.set_visible(bool(s.get("groupName")))
        self._tick()

    def _on_vol(self, scale):
        if self._vol_lock:
            return
        if self._vol_timer:
            GLib.source_remove(self._vol_timer)

        def send():
            self._vol_timer = None
            level = int(scale.get_value())
            grp = self.group.get_active()
            self.win.cmd(lambda p: p.set_volume_group(level) if grp else p.set_volume(level))
            return False
        self._vol_timer = GLib.timeout_add(150, send)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Nodeplay", default_width=1400, default_height=950)
        self.cfg = load_config()
        self.players = dict(self.cfg.get("players", {}))
        self.player = None
        self._poll_gen = 0
        self._keys = []
        self._last_pid = None
        self._home_timer = None
        self.set_size_request(360, 560)

        # ---- sidebar
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        side.add_css_class("sidebar-pane")
        title = Gtk.Label(label="NODEPLAY")
        title.add_css_class("brand")
        side.append(title)
        self.brand = Gtk.Label(label="for BluOS players")  # becomes "for <player's brand>" once it answers
        self.brand.add_css_class("brand-sub")
        side.append(self.brand)
        self.search = Gtk.SearchEntry(placeholder_text="Search", margin_start=10, margin_end=10)
        self.search.connect("activate", self._on_search)
        side.append(self.search)
        self.model = Gtk.StringList()
        self.dropdown = Gtk.DropDown(model=self.model, tooltip_text="Choose player", margin_start=10, margin_end=10)
        self.dropdown.connect("notify::selected", self._on_pick)
        side.append(self.dropdown)
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, vexpand=True)
        self.nav.add_css_class("navigation-sidebar")
        self.nav.connect("row-activated", self._on_nav)
        nsc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        nsc.set_child(self.nav)
        side.append(nsc)
        tools = Gtk.Box(spacing=4, margin_start=10, margin_end=10, margin_bottom=10)
        for icon, tip, cb in (("view-refresh-symbolic", "Find players", self.rescan),
                              ("list-add-symbolic", "Add player by IP", self._ask_ip),
                              ("help-about-symbolic", "About", self._about)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, cb=cb: cb())
            tools.append(b)
        side.append(tools)

        # ---- pages
        self.now = NowPlayingPage(self)
        self.home = HomePage(self)
        self.presets = PresetsPage(self)
        self.browse = BrowsePage(self)
        self.queue = QueuePage(self)
        self.settings = SettingsPage(self)
        self.recent = RecentPage(self)
        self.web = WebPage(self)
        self.pages = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        for name, w in (("home", self.home), ("now", self.now), ("presets", self.presets),
                        ("browse", self.browse), ("queue", self.queue), ("settings", self.settings), ("recent", self.recent), ("web", self.web)):
            self.pages.add_named(w, name)

        self.searching = Adw.StatusPage(icon_name="network-wireless-symbolic", title="Looking for players…",
                                        description="Searching your network for BluOS devices.")
        self.pages.add_named(self.searching, "search")
        self.none = Adw.StatusPage(icon_name="network-wireless-offline-symbolic", title="No players found",
                                   description="Make sure the player is on and on the same network.")
        nb = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        for label, cb in (("Search again", self.rescan), ("Enter IP", self._ask_ip)):
            b = Gtk.Button(label=label)
            b.add_css_class("pill")
            b.connect("clicked", lambda _b, cb=cb: cb())
            nb.append(b)
        self.none.set_child(nb)
        self.pages.add_named(self.none, "none")

        self.bar = PlayerBar(self)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.pages)

        self.split = Adw.OverlaySplitView(min_sidebar_width=240, max_sidebar_width=300)
        self.split.set_sidebar(side)
        self.split.set_content(content)

        header = Adw.HeaderBar()
        self.toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Sources", visible=False)
        self.toggle.bind_property("active", self.split, "show-sidebar",
                                  gi.repository.GObject.BindingFlags.BIDIRECTIONAL)
        header.pack_start(self.toggle)

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        self.offline_banner = Adw.Banner(title="Player offline — reconnecting…", button_label="Find players")
        self.offline_banner.connect("button-clicked", lambda _b: self.rescan())
        tv.add_top_bar(self.offline_banner)
        self.update_banner = Adw.Banner(title="A firmware update is available for this player",
                                        button_label="Update…")
        self.update_banner.connect("button-clicked", lambda _b: (
            self.update_banner.set_revealed(False),
            self.open_web(f"http://{self.player.ip}/upgrade?noheader=1", "Player update")))
        tv.add_top_bar(self.update_banner)
        tv.set_content(self.split)
        tv.add_bottom_bar(self.bar)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 800sp"))
        bp.add_setter(self.split, "collapsed", True)
        bp.add_setter(self.toggle, "visible", True)
        self.add_breakpoint(bp)

        self._build_nav([])
        if self.players:
            self._refresh_dropdown(select=self.cfg.get("last"))
        else:
            self.pages.set_visible_child_name("search")
        self.rescan(quiet=bool(self.players))

    # ---- navigation
    def _build_nav(self, sources):
        """Sidebar grouped like the official app: home | library + inputs | services | settings."""
        self.nav.remove_all()
        self._nav = []

        def add(label, icon, target, image=None):
            row = Gtk.ListBoxRow()
            b = Gtk.Box(spacing=14)
            if image and self.player:
                img = Gtk.Image(pixel_size=22)
                img.set_from_icon_name(icon)
                IMAGES.set(img, self.player.url(image))
            else:
                img = Gtk.Image.new_from_icon_name(icon)
            b.append(img)
            b.append(Gtk.Label(label=label, xalign=0))
            row.set_child(b)
            self.nav.append(row)
            self._nav.append(target)

        def gap():
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            row.add_css_class("nav-gap")
            self.nav.append(row)
            self._nav.append(None)

        add("Home", "go-home-symbolic", ("page", "home"))
        add("Favorites", "starred-symbolic", ("page", "presets"))
        gap()
        local = [it for it in sources if (it.get("browseKey") or "").startswith("LocalMusic") or it.get("playURL")]
        services = [it for it in sources if it not in local]
        if sources and not any((it.get("browseKey") or "").startswith("LocalMusic") for it in sources):
            add("Library", "folder-music-symbolic", ("shares",))  # player hides Library until a share exists
        icons = {"bluetooth": "bluetooth-symbolic", "arc": "video-display-symbolic",
                 "spdif": "audio-card-symbolic", "analog": "audio-input-microphone-symbolic"}
        for it in local:
            if it.get("browseKey"):
                add(it.get("text", ""), "folder-music-symbolic", ("browse", it["browseKey"], it.get("text", "")),
                    it.get("image"))
            else:
                add(it.get("text", ""), icons.get(it.get("inputType"), "audio-input-microphone-symbolic"),
                    ("play", it["playURL"], it.get("text", "")), it.get("image"))
        if services:
            gap()
        for it in services:
            add(it.get("text", ""), "folder-music-symbolic", ("browse", it["browseKey"], it.get("text", "")),
                it.get("image"))
        add("Music Services", "list-add-symbolic", ("services",))
        gap()
        add("Settings", "emblem-system-symbolic", ("settings",))
        add("Help", "help-browser-symbolic", ("help",))
        self.nav.select_row(self.nav.get_row_at_index(0))

    def _on_nav(self, _lb, row):
        t = self._nav[row.get_index()]
        if t is None:
            return
        if t[0] == "services":
            self.open_web(self.player.url("/redirectToCp?href=%2Fservices%3Fnoheader%3D1%26schemaVersion%3D35"),
                          "Music Services")
        elif t[0] == "help":
            Gtk.UriLauncher.new("https://support.bluos.net").launch(self, None, None, None)
        elif t[0] == "page":
            self.show_page(t[1], select=False)
        elif t[0] == "browse":
            self.browse.open(t[1], t[2], reset=True)
            self.pages.set_visible_child_name("browse")
        elif t[0] == "shares":
            self.open_shares()
            self.toast("No music library yet — add a folder below")
        elif t[0] == "settings":
            self.settings.reset()
            self.pages.set_visible_child_name("settings")
        elif t[0] == "play":  # an input (Bluetooth, HDMI ARC, optical…): the player now listens to it
            self.cmd(lambda p: p.action(t[1]))
            if "bluetooth" in t[1].lower():  # open the Bluetooth page: phone pairing + headphones
                self.settings.path = [("Settings", None, None, None)]
                self.settings.open_special("Bluetooth", "__bt__")
                self.pages.set_visible_child_name("settings")
            else:
                self.show_page("now", select=False)
                self.toast(f"Now listening to {t[2]}")
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def show_page(self, name, select=True):
        self.pages.set_visible_child_name(name)
        if select:
            for i, t in enumerate(self._nav):
                if t == ("page", name):
                    self.nav.select_row(self.nav.get_row_at_index(i))

    def _on_search(self, entry):
        q = entry.get_text().strip()
        if q:
            self.browse.history = [(None, "Sources", None)]
            self.browse.open(None, f"Search: {q}", query=q)
            self.pages.set_visible_child_name("browse")
            self.nav.unselect_all()

    # ---- players
    def rescan(self, quiet=False):
        if not quiet:
            self.pages.set_visible_child_name("search")

        def find():
            return discover(timeout=3) or scan_subnet()

        def done(found):
            new = any(k not in self.players for k in found)
            self.players.update(found)
            if new or not quiet:
                self._refresh_dropdown(select=self.player.key if self.player else self.cfg.get("last"))
            if not self.players:
                self.pages.set_visible_child_name("none")
            elif not quiet and self.player:
                self.pages.set_visible_child_name("home")
        run_bg(find, done, self.error)

    def _refresh_dropdown(self, select=None):
        keys = list(self.players)
        self._keys = keys
        self.model.splice(0, self.model.get_n_items(), [self.players[k].get("name", k) for k in keys])
        self.cfg["players"] = self.players
        save_config(self.cfg)
        if not keys:
            return
        idx = keys.index(select) if select in keys else 0
        self.dropdown.set_selected(idx)
        self._on_pick()

    def _on_pick(self, *_a):
        i = self.dropdown.get_selected()
        if i == Gtk.INVALID_LIST_POSITION or i >= len(self._keys):
            return
        key = self._keys[i]
        if self.player and self.player.key == key:
            return
        info = self.players[key]
        self.player = Player(info["ip"], info.get("port", 11000))
        self.cfg["last"] = key
        save_config(self.cfg)
        self.bar.set_player_name(info.get("name", key))
        self.presets.prid = None
        self.queue.pid = None
        self._last_pid = None
        self.show_page("home")
        self.home.reload()
        p = self.player
        run_bg(lambda: p.browse(), lambda r: self._build_nav(r["sections"][0][1] if r["sections"] else []),
               self.error)
        run_bg(p.sync_status, lambda i: self.brand.set_label(f"for {i.get('brand') or 'BluOS players'}"), lambda e: None)
        self.offline_banner.set_revealed(False)
        self.update_banner.set_revealed(False)
        run_bg(p.update_available, lambda up: self.update_banner.set_revealed(up and self.player is p),
               lambda e: None)
        self._start_polling()

    def open_web(self, url, title):
        self.web.open(url, title)
        self.pages.set_visible_child_name("web")

    def browse_to(self, key, title):
        self.browse.open(key, title, reset=True)
        self.pages.set_visible_child_name("browse")
        self.nav.unselect_all()

    def show_settings(self):
        self.settings.reset()
        self.show_page("settings", select=False)

    def show_recent(self):
        self.recent.reload()
        self.pages.set_visible_child_name("recent")
        self.nav.unselect_all()

    def refresh_sources(self):
        p = self.player
        run_bg(lambda: p.browse(), lambda r: self._build_nav(r["sections"][0][1] if r["sections"] else []),
               lambda e: None)

    def open_shares(self):
        self.settings.path = [("Settings", None, None, None)]  # no reload: avoid racing the shares render
        self.settings.open_shares()
        self.pages.set_visible_child_name("settings")

    def rename_current(self):
        """Room name changed in Settings: refresh dropdown + bar labels."""
        p = self.player

        def ok(info):
            self.players[p.key]["name"] = info.get("name", p.ip)
            self.bar.set_player_name(info.get("name", p.ip))
            self._refresh_dropdown(select=p.key)
        run_bg(p.sync_status, ok, lambda e: None)

    def _ask_ip(self):
        dlg = Adw.MessageDialog(transient_for=self, heading="Add player",
                                body="Enter the player's IP address (BluOS app → Settings → Player → Diagnostics).")
        entry = Gtk.Entry(placeholder_text="192.168.1.140", activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("add", "Add")
        dlg.set_default_response("add")
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "add":
                return
            text = entry.get_text().strip()
            ip, _, port = text.partition(":")
            port = int(port) if port.isdigit() else 11000
            p = Player(ip, port)

            def ok(info):
                self.players[p.key] = {"ip": ip, "port": port, "name": info.get("name", ip),
                                       "model": info.get("modelName", "")}
                self._refresh_dropdown(select=p.key)
                self.toast(f"Added {info.get('name', ip)}")
            run_bg(p.sync_status, ok, lambda e: self.toast(f"No BluOS player at {text}"))
        dlg.connect("response", resp)
        dlg.present()

    def _about(self):
        Adw.AboutWindow(transient_for=self, application_name="Nodeplay", application_icon=APP_ID,
                        version=__version__, developer_name="Aggelos Basdekis",
                        comments="Unofficial Linux controller for BluOS players (Bluesound, NAD, DALI…).\n"
                                 "Talks directly to your player on your home network using the BluOS API "
                                 "published by Lenbrook — the same local interface the official apps use. "
                                 "No cloud, no account.",
                        license_type=Gtk.License.MIT_X11).present()

    # ---- status long-poll
    def _start_polling(self):
        self._poll_gen += 1
        gen, player = self._poll_gen, self.player

        def loop():
            etag = None
            fails = 0
            while gen == self._poll_gen:
                started = time.monotonic()
                try:
                    s = player.status(etag=etag, timeout=60)
                    etag = s.get("etag")
                    if fails:  # back online (e.g. after a reboot or firmware update)
                        GLib.idle_add(self._player_back, gen)
                    fails = 0
                    GLib.idle_add(self._apply_status, gen, s)
                except BluOSError:
                    etag = None
                    fails += 1
                    if fails == 2:
                        GLib.idle_add(self.offline_banner.set_revealed, True)
                    time.sleep(3)
                time.sleep(max(0.0, 1.0 - (time.monotonic() - started)))  # API: ≥1s between requests
        threading.Thread(target=loop, daemon=True).start()

    def _player_back(self, gen):
        """Player answered again after being offline: hide the banner, reload what may have changed."""
        if gen != self._poll_gen:
            return
        self.offline_banner.set_revealed(False)
        self.toast("Player is back online")
        self.refresh_sources()
        self.home.reload()
        p = self.player
        run_bg(p.update_available, lambda up: self.update_banner.set_revealed(up and self.player is p),
               lambda e: None)

    def _apply_status(self, gen, s):
        if gen != self._poll_gen:
            return
        if self.offline_banner.get_revealed():
            self.offline_banner.set_revealed(False)
        self.now.update(s)
        self.bar.update(s)
        self.presets.maybe_reload(s.get("prid"))
        self.queue.maybe_reload(s.get("pid"), s.get("song"))
        sig = (s.get("pid"), s.get("streamUrl"), s.get("prid"))
        if sig != self._last_pid:
            first = self._last_pid is None
            self._last_pid = sig
            if not first:  # player updates its history a moment after a change
                if self._home_timer:
                    GLib.source_remove(self._home_timer)
                self._home_timer = GLib.timeout_add_seconds(4, self._refresh_home)

    def _refresh_home(self):
        self._home_timer = None
        self.home.reload()
        return False

    def kick(self):
        gen, p = self._poll_gen, self.player
        run_bg(p.status, lambda s: self._apply_status(gen, s), lambda e: None)

    # ---- commands
    def cmd(self, fn, toast=None):
        if not self.player:
            return
        p = self.player

        def done(_r):
            if toast:
                self.toast(toast)
            self.kick()
        run_bg(lambda: fn(p), done, self.error)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=2))

    def error(self, e):
        self.toast(f"⚠ {e}")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            win = MainWindow(self)
            if os.environ.get("BLUOS_SNAPSHOT"):  # dev aid: render window to PNG after N seconds
                path, _, delay = os.environ["BLUOS_SNAPSHOT"].partition(",")
                GLib.timeout_add_seconds(int(delay or 8), _snapshot, win, path)
        win.present()


def _snapshot(win, path):
    paintable = Gtk.WidgetPaintable.new(win)
    w, h = win.get_width(), win.get_height()
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    tex = win.get_native().get_renderer().render_texture(node, None)
    tex.save_to_png(path)
    return False


def main():
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
