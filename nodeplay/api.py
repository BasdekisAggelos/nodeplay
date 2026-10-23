"""Thin client for the BluOS HTTP/XML API (port 11000). Stdlib only."""
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PORT = 11000


class BluOSError(Exception):
    pass


class Player:
    def __init__(self, ip, port=DEFAULT_PORT):
        self.ip = ip
        self.port = int(port)
        self.base = f"http://{ip}:{self.port}"

    @property
    def key(self):
        return f"{self.ip}:{self.port}"

    # ---- low level -------------------------------------------------------
    def url(self, path):
        """Absolute URL for a player-relative path (artwork, playURL, ...)."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base + (path if path.startswith("/") else "/" + path)

    def get_raw(self, path, params=None, timeout=8):
        url = self.url(path)
        if params:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except OSError as e:
            raise BluOSError(f"{self.ip}: {e}") from e

    def get(self, path, params=None, timeout=8):
        data = self.get_raw(path, params, timeout)
        try:
            return ET.fromstring(data)
        except ET.ParseError:
            return None  # some commands return nothing

    # ---- status ----------------------------------------------------------
    def status(self, etag=None, timeout=100):
        """Playback status. With etag this long-polls up to `timeout` seconds."""
        params = {"timeout": timeout, "etag": etag} if etag else None
        root = self.get("/Status", params, timeout=(timeout + 10) if etag else 8)
        return _status_dict(root)

    def sync_status(self):
        root = self.get("/SyncStatus")
        d = dict(root.attrib)
        m = root.find("master")
        d["master"] = f"{m.text}:{m.get('port', DEFAULT_PORT)}" if m is not None else None
        d["slaves"] = [f"{s.get('id')}:{s.get('port', DEFAULT_PORT)}" for s in root.findall("slave")]
        bt = root.find("bluetoothOutput")  # present only on players that can send to BT headphones
        d["bluetoothOutput"] = dict(bt.attrib) if bt is not None else None
        return d

    # ---- playback --------------------------------------------------------
    def play(self, seek=None):
        return self.get("/Play", {"seek": int(seek)} if seek is not None else None)

    def pause(self, toggle=False):
        return self.get("/Pause", {"toggle": 1} if toggle else None)

    def stop(self):
        return self.get("/Stop")

    def skip(self):
        return self.get("/Skip")

    def back(self):
        return self.get("/Back")

    def shuffle(self, on):
        return self.get("/Shuffle", {"state": 1 if on else 0})

    def repeat(self, state):
        """0 = repeat queue, 1 = repeat track, 2 = off."""
        return self.get("/Repeat", {"state": state})

    def action(self, url):
        """Run an action/playURL/actionURL path given by the player."""
        return self.get(url)

    # ---- volume ----------------------------------------------------------
    def set_volume(self, level):
        return self.get("/Volume", {"level": int(level)})

    def set_mute(self, on):
        return self.get("/Volume", {"mute": 1 if on else 0})

    def volume_step(self, db, tell_slaves=False):
        """Relative change in dB (doc 3.2/3.3, typical ±2)."""
        params = {"db": db}
        if tell_slaves:
            params["tell_slaves"] = 1
        return self.get("/Volume", params)

    def set_volume_group(self, level):
        return self.get("/Volume", {"level": int(level), "tell_slaves": 1})

    def play_url(self, stream_url):
        """Play any http(s) audio stream/file (doc 4.1)."""
        return self.get("/Play", {"url": stream_url})

    def play_input(self, type_index):
        """Select an input by type-index, e.g. 'spdif-1', 'arc-1' (doc 11.2, firmware >= 4.2)."""
        return self.get("/Play", {"inputTypeIndex": type_index})

    def inputs(self):
        """Active inputs (doc 11.1): [dict(text, URL, typeIndex, inputType, image)]."""
        root = self.get("/RadioBrowse", {"service": "Capture"})
        return [dict(i.attrib) for i in root.iter("item")] + \
               [dict(i.attrib) for i in root.iter("remoteitem")] if root is not None else []

    # ---- queue / presets / browse ----------------------------------------
    def queue(self, start=0, end=199):
        root = self.get("/Playlist", {"start": start, "end": end})
        songs = []
        if root is not None:
            for s in root.findall("song"):
                songs.append({"id": int(s.get("id", 0)),
                              "title": s.findtext("title", ""),
                              "artist": s.findtext("art", ""),
                              "album": s.findtext("alb", "")})
        return songs

    def play_queue_track(self, track_id):
        return self.get("/Play", {"id": track_id})

    def delete_track(self, track_id):
        return self.get("/Delete", {"id": track_id})

    def move_track(self, old, new):
        return self.get("/Move", {"new": new, "old": old})

    def save_queue(self, name):
        root = self.get("/Save", {"name": name})
        return int(root.findtext("entries", "0")) if root is not None else 0

    def clear_queue(self):
        return self.get("/Clear")

    def presets(self):
        root = self.get("/Presets")
        return [dict(p.attrib) for p in root.findall("preset")] if root is not None else []

    def load_preset(self, preset_id):
        return self.get("/Preset", {"id": preset_id})

    def preset_step(self, direction):
        """Next (+1) or previous (-1) preset (doc 6.2)."""
        return self.get("/Preset", {"id": "+1" if direction > 0 else "-1"})

    def browse(self, key=None, query=None, context_menus=False):
        params = {}
        if key:
            params["key"] = key
        if query:
            params["q"] = query
        if context_menus:
            params["withContextMenuItems"] = 1
        root = self.get("/Browse", params or None)
        if root is None:
            raise BluOSError("empty browse response")
        if root.tag == "error":
            raise BluOSError(root.findtext("message", "browse error"))
        result = dict(root.attrib)
        sections = []  # [(heading or None, [item dict])]
        def item(el):
            d = dict(el.attrib)
            cm = el.find("contextMenu")
            if cm is not None:
                d["contextMenu"] = [dict(c.attrib) for c in cm.findall("item")]
            return d
        loose = [item(i) for i in root.findall("item")]
        if loose:
            sections.append((None, loose))
        for cat in root.findall("category"):
            sections.append((cat.get("text"), [item(i) for i in cat.findall("item")]))
        result["sections"] = sections
        return result


    # ---- grouping (doc 8) ----------------------------------------------------
    def add_slaves(self, players, group_name=None):
        """Make self the primary and attach players [(ip, port)]."""
        params = {"slaves": ",".join(ip for ip, _ in players),
                  "ports": ",".join(str(port) for _, port in players)}
        if group_name:
            params["group"] = group_name
        return self.get("/AddSlave", params)

    def remove_slaves(self, players):
        return self.get("/RemoveSlave", {"slaves": ",".join(ip for ip, _ in players),
                                         "ports": ",".join(str(port) for _, port in players)})

    # ---- misc (doc 10, 12) -----------------------------------------------------
    def doorbell(self):
        return self.get("/Doorbell", {"play": 1})

    def bluetooth_mode(self, mode):
        """0 manual, 1 automatic, 2 guest, 3 disabled."""
        return self.get("/audiomodes", {"bluetoothAutoplay": mode})

    # ---- device settings (schema-driven, same as the official app) -------
    SETTINGS_SCHEMA = 34

    def settings(self, page_id=None):
        """Settings tree as served by the player (/Settings redirects to port 11001)."""
        params = {"schemaVersion": self.SETTINGS_SCHEMA}
        if page_id:
            params["id"] = page_id
        return self.get("/Settings", params)

    def set_setting(self, url, name, value):
        """Write one setting: POST name=value to the setting's url on the main port."""
        data = urllib.parse.urlencode({name: value}).encode()
        req = urllib.request.Request(self.url(url), data=data, method="POST",
                                     headers={"Accept": "text/javascript; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.read()
        except OSError as e:
            raise BluOSError(f"{self.ip}: {e}") from e

    def sleep_timer(self):
        """Advance the sleep timer one step (off→15→30→45→60→90→off); returns minutes or None."""
        root = self.get("/Sleep")
        txt = (root.text or "").strip() if root is not None else ""
        return int(txt) if txt.isdigit() else None

    # ---- network shares (music library folders) — the player's own web form on port 80
    def _web_post(self, path, fields, timeout=30):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(f"http://{self.ip}{path}", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except OSError as e:
            raise BluOSError(f"{self.ip}: {e}") from e

    def shares(self):
        """Current library shares: [(share_path, label)]."""
        import html
        import re
        try:
            with urllib.request.urlopen(f"http://{self.ip}/sharecfg?noheader=1", timeout=8) as r:
                page = r.read().decode("utf-8", "replace")
        except OSError as e:
            raise BluOSError(f"{self.ip}: {e}") from e
        out = []
        for m in re.finditer(r'<input name="([^"]+)" id="(checkbox\d+)" type="checkbox"', page):
            label = re.search(r'<label for="%s">\s*(.*?)\s*</label>' % m.group(2), page, re.S)
            out.append((html.unescape(m.group(1)), html.unescape(label.group(1).strip()) if label else m.group(1)))
        return out

    def add_share(self, share, username="", password=""):
        """share like \\\\Computer\\Music. Returns the player's reply text (stripped of HTML)."""
        import re
        page = self._web_post("/manshare", {"noheader": 1, "sharename": share,
                                            "username": username, "password": password}, timeout=60)
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style|head).*?</\1>", " ", page))
        return " ".join(text.split())

    def remove_share(self, share):
        self._web_post("/findremoveshares?noheader=1", {share: "on", "remove": "Remove selected shares"})

    def reindex(self):
        """Rescan the music library shares."""
        return self.get("/Reindex")

    # ---- alarms (same calls as the official app) ---------------------------
    def alarms(self, tz):
        root = self.get("/Alarms", {"tz": tz})
        out = []
        for a in root.findall("alarm") if root is not None else []:
            d = dict(a.attrib)
            for k in ("id", "hour", "minute", "duration", "volume"):
                d[k] = int(d.get(k) or 0)
            d["fadein"] = d.get("fadein") == "1"
            d["enable"] = d.get("enable") == "1"
            d["days"] = [c == "1" for c in d.get("days", "0000000")]  # Sun..Sat
            out.append(d)
        return out

    def save_alarm(self, a, tz):
        """Create (id 0) or update an alarm. a: dict as returned by alarms()."""
        return self.get("/Alarms", {
            "id": a.get("id", 0), "days": "".join("1" if x else "0" for x in a["days"]),
            "duration": a["duration"], "hour": f"{a['hour']:02d}", "minute": f"{a['minute']:02d}",
            "fadein": 1 if a["fadein"] else 0, "volume": a["volume"], "source": a.get("source", ""),
            "service": a.get("service", ""), "image": a.get("image", ""), "url": a.get("url", ""),
            "enable": 1 if a["enable"] else 0, "tz": tz})

    def delete_alarm(self, alarm_id):
        return self.get("/Alarms", {"delete": 1, "id": alarm_id})

    def alarm_sources(self):
        """What an alarm can play: current queue, presets, inputs → [dict(source, service, url, image)]."""
        out = [{"source": "Current play queue or station", "service": "", "url": "", "image": ""}]
        for q in ({"service": "Alarms", "url": "presets"}, {"service": "Capture", "url": "presets"}):
            root = self.get("/RadioBrowse", q)
            if root is None:
                continue
            for it in root.iter("item"):
                if it.get("type") != "audio":
                    continue
                url = urllib.parse.unquote(it.get("URL", ""))
                service = it.get("service") or root.get("service") or \
                    urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("service", [""])[0]
                out.append({"source": it.get("text", ""), "service": service, "url": url,
                            "image": it.get("image", "")})
        return out

    # ---- bluetooth headphones (players with <bluetoothOutput>) -------------
    def bt_devices(self, **action):
        """action: refresh=1 (scan), connect=id, disconnect=1, unpair=id, or none to just list."""
        root = self.get("/BTDevices", action or None, timeout=15)
        return [dict(d.attrib) for d in root.iter("device")] if root is not None else []

    def update_available(self):
        """Ask the player whether a firmware update is waiting (its own /upgrade page, port 80)."""
        try:
            with urllib.request.urlopen(f"http://{self.ip}/upgrade?noheader=1", timeout=20) as r:
                page = r.read().decode("utf-8", "replace")
        except OSError as e:
            raise BluOSError(f"{self.ip}: {e}") from e
        return "no update available" not in page.lower()

    def reboot(self):
        req = urllib.request.Request(f"http://{self.ip}/reboot", data=b"yes=1", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.read()
        except OSError as e:
            raise BluOSError(f"{self.ip}: {e}") from e


def _status_dict(root):
    if root is None:
        raise BluOSError("empty status")
    d = {child.tag: (child.text or "") for child in root if len(child) == 0}
    d["etag"] = root.get("etag")
    d["actions"] = [dict(a.attrib) for a in root.iter("action")]
    for k in ("secs", "totlen", "volume", "shuffle", "repeat", "mute", "song", "canSeek"):
        try:
            d[k] = int(d[k]) if k in d and d[k] != "" else None
        except ValueError:
            d[k] = None
    return d


def scan_subnet(timeout=0.5):
    """Fallback discovery: probe port 11000 on every host of our /24. Returns {key: info}."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        local = s.getsockname()[0]
    except OSError:
        return {}
    finally:
        s.close()
    prefix = local.rsplit(".", 1)[0]

    def probe(i):
        ip = f"{prefix}.{i}"
        try:
            socket.create_connection((ip, DEFAULT_PORT), timeout).close()
            info = Player(ip).sync_status()
            return {"ip": ip, "port": DEFAULT_PORT, "name": info.get("name", ip),
                    "model": info.get("modelName", "")}
        except (OSError, BluOSError, AttributeError):
            return None

    with ThreadPoolExecutor(64) as ex:
        return {f"{r['ip']}:{r['port']}": r for r in ex.map(probe, range(1, 255)) if r}
