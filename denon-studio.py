#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DENON STUDIO — control panel for the Denon AVR-X1000 with analog VU meters.
Plain Python (standard library), nothing to install.

Run:      python3 ~/Desktop/denon-studio/denon-studio.py
Open:     http://localhost:8766      (phone: http://<computer-IP>:8766)

- persistent telnet connection to the Denon (fast, live status)
- all inputs the receiver itself reports (SSFUN / SSSOD)
- "Now playing" / browse list for net sources (NSE)
- YTuner (internet radio server) start/stop from the page
- stream proxy for the VU meters: the page analyses the SAME station the Denon is playing
"""

import os, re, ssl, pwd, json, time, socket, signal, sqlite3, hashlib, threading, subprocess, collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urljoin, urlencode
from urllib.request import Request, urlopen

# ---------------------------------------------------------------- settings
# Everything can be set in ~/.config/denon-studio/config.json, e.g.
#   {"denon_ip": "192.168.1.50", "port": 8766, "ytuner_dir": "~/YTuner/ytuner-1.2.6-x86_64-linux"}
# or with environment variables DENON_IP / DENON_STUDIO_PORT / YTUNER_DIR.
# Without a denon_ip the receiver is found on the network automatically (SSDP) and remembered.
HERE        = os.path.dirname(os.path.abspath(__file__))
CONF_DIR    = os.path.join(os.path.expanduser("~"), ".config", "denon-studio")
CONF_FILE   = os.path.join(CONF_DIR, "config.json")

def _conf():
    try: return json.load(open(CONF_FILE, encoding="utf-8"))
    except Exception: return {}

def _conf_set(**kw):
    c = _conf(); c.update(kw)
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        json.dump(c, open(CONF_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except OSError: pass

def discover_denon(timeout=3.0):
    """find a Denon / Marantz receiver on the LAN with an SSDP (UPnP) search"""
    msg = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\n'
           'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2); s.settimeout(0.5)
        s.sendto(msg.encode(), ("239.255.255.250", 1900))
        end, seen = time.time() + timeout, set()
        while time.time() < end:
            try: data, addr = s.recvfrom(4096)
            except socket.timeout: continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if not m or addr[0] in seen: continue
            seen.add(addr[0])
            try: desc = urlopen(m.group(1).decode(), timeout=2).read(30000).decode("utf-8", "replace")
            except Exception: continue
            if re.search(r"<manufacturer>\s*(Denon|Marantz)", desc, re.I):
                return addr[0]
    except OSError: pass
    finally: s.close()
    return None

def _find_ytuner():
    d = os.environ.get("YTUNER_DIR") or _conf().get("ytuner_dir")
    if d: return os.path.expanduser(d)
    import glob
    c = sorted(glob.glob(os.path.expanduser("~/YTuner/ytuner-*")))
    return c[-1] if c else os.path.expanduser("~/YTuner")

DENON_IP = os.environ.get("DENON_IP") or _conf().get("denon_ip")
if not DENON_IP:
    DENON_IP = discover_denon()
    if DENON_IP: _conf_set(denon_ip=DENON_IP)
    else:
        DENON_IP = "192.168.0.100"
        print("!! Denon nije pronađen na mreži — upiši IP u %s (\"denon_ip\") ili pokreni sa DENON_IP=..." % CONF_FILE)
DENON_PORT  = 23
LISTEN_PORT = int(os.environ.get("DENON_STUDIO_PORT") or _conf().get("port") or 8766)
YTUNER_DIR  = _find_ytuner()
YTUNER_BIN  = os.path.join(YTUNER_DIR, "ytuner")

# ---------------------------------------------------------------- Denon state
_send_lock  = threading.Lock()
_state_lock = threading.Lock()
_sock = None

# all sources the X1000 protocol knows, in panel order (code, default name, group)
ALL_INPUTS = [
    ("SAT/CBL", "CBL/SAT", "av"), ("DVD", "DVD", "av"), ("BD", "Blu-ray", "av"),
    ("GAME", "Game", "av"), ("MPLAY", "Media Player", "av"), ("TV", "TV Audio", "av"),
    ("AUX1", "AUX", "av"), ("CD", "CD", "av"), ("TUNER", "Tuner", "radio"),
    ("IRADIO", "Internet Radio", "net"), ("NET", "Online Music", "net"),
    ("SERVER", "Media Server", "net"), ("USB/IPOD", "iPod/USB", "net"),
    ("FAVORITES", "Favorites", "net"), ("SPOTIFY", "Spotify", "net"),
    ("LASTFM", "Last.fm", "net"), ("FLICKR", "Flickr", "net"), ("PANDORA", "Pandora", "net"),
    ("SIRIUSXM", "SiriusXM", "net"), ("BT", "Bluetooth", "net"),
]
NET_SOURCES = {c for c, _, g in ALL_INPUTS if g == "net"} | {"IRP", "USB", "IPD"}

STATE = {
    "connected": False, "power": None, "zm": None, "mute": None,
    "volume": None, "volmax": -4.5, "input": None, "input_raw": None,
    "surround": None, "dynEq": None, "multEq": None,
    "fun": {},          # renamed sources (SSFUN)
    "sod": {},          # source used/deleted (SSSOD)
    "nse": [""] * 9,    # NSE lines 0..8 (now playing / browse list)
    "nseFlags": [0] * 9,
    "nseTime": 0,
}

def _vol(v):
    return (int(v) / 10.0 if len(v) == 3 else float(int(v))) - 80

def apply_line(l):
    with _state_lock:
        st = STATE
        if l == "PWON": st["power"] = True
        elif l == "PWSTANDBY": st["power"] = False
        elif l == "ZMON": st["zm"] = True
        elif l == "ZMOFF": st["zm"] = False
        elif l == "MUON": st["mute"] = True
        elif l == "MUOFF": st["mute"] = False
        elif l.startswith("MVMAX "):
            try: st["volmax"] = _vol(l.split()[1])
            except Exception: pass
        elif l.startswith("MV") and l[2:].isdigit():
            st["volume"] = _vol(l[2:])
        elif l.startswith("SI") and not l.startswith("SIGNAL"):
            st["input_raw"] = l[2:].strip()
        elif l.startswith("MS") and not l.startswith("MSQUICK"):
            st["surround"] = l[2:].strip()
        elif l.startswith("PSDYNEQ "):
            st["dynEq"] = l[8:].strip() == "ON"
        elif l.startswith("PSMULTEQ:"):
            st["multEq"] = l[9:].strip()
        elif l.startswith("SSFUN"):
            m = re.match(r"SSFUN(\S+)\s+(.*)", l)
            if m: st["fun"][m.group(1)] = m.group(2).strip()
        elif l.startswith("SSSOD"):
            m = re.match(r"SSSOD(\S+)\s+(\S+)", l)
            if m: st["sod"][m.group(1)] = m.group(2)
        elif l.startswith("NSE") and len(l) >= 4 and l[3].isdigit():
            i = int(l[3]); txt = l[4:]
            flags = 0
            if 1 <= i <= 7 and txt and ord(txt[0]) < 32:
                flags = ord(txt[0]); txt = txt[1:]
            st["nse"][i] = "".join(ch for ch in txt if ord(ch) >= 32).rstrip()
            st["nseFlags"][i] = flags
            st["nseTime"] = time.time()
        raw = st["input_raw"]
        if raw:
            names = dict((c, n) for c, n, _ in ALL_INPUTS)
            st["input"] = st["fun"].get(raw) or names.get(raw, raw.title())

QUERY = ["PW?", "ZM?", "MU?", "MV?", "SI?", "MS?", "PSDYNEQ ?", "PSMULTEQ: ?", "SSFUN ?", "SSSOD ?"]

def reader_loop():
    global _sock
    while True:
        buf = b""
        try:
            s = socket.create_connection((DENON_IP, DENON_PORT), timeout=4)
            s.settimeout(1.0)
            with _send_lock: _sock = s
            with _state_lock: STATE["connected"] = True
            for c in QUERY:
                s.sendall((c + "\r").encode()); time.sleep(0.05)
            last_poll = last_nse = time.time()
            while True:
                try:
                    data = s.recv(8192)
                    if not data: raise IOError("closed")
                    buf += data
                    while b"\r" in buf:
                        line, buf = buf.split(b"\r", 1)
                        line = line.decode("utf-8", errors="replace").strip("\n ")
                        if line: apply_line(line)
                except socket.timeout:
                    pass
                now = time.time()
                if now - last_poll > 8:
                    with _send_lock: s.sendall(b"PW?\rMV?\rSI?\rMS?\rMU?\r")
                    last_poll = now
                with _state_lock:
                    net = STATE["input_raw"] in NET_SOURCES and STATE["power"]
                if net and now - last_nse > 2.5:
                    with _send_lock: s.sendall(b"NSE\r")
                    last_nse = now
        except Exception:
            with _send_lock: _sock = None
            with _state_lock: STATE["connected"] = False
            time.sleep(2)

def send_cmd(cmds):
    global _sock
    with _send_lock:
        if not _sock: return False
        try:
            for c in cmds:
                _sock.sendall((c + "\r").encode())
                time.sleep(0.03)
            if any(c.startswith(("NS9", "SI", "MN")) for c in cmds):
                _sock.sendall(b"NSE\r")
            return True
        except Exception:
            try: _sock.close()
            except Exception: pass
            _sock = None
            return False

def input_list():
    with _state_lock:
        fun, sod = dict(STATE["fun"]), dict(STATE["sod"])
    out = []
    for code, name, grp in ALL_INPUTS:
        if sod:
            if sod.get(code) != "USE": continue
        elif code in ("PANDORA", "SIRIUSXM", "BT", "CD"):
            continue
        out.append({"code": code, "name": fun.get(code, name), "group": grp})
    return out

# ---------------------------------------------------------------- stations (YTuner)
_stations = []          # [{name, url, logo, cat, src}]
_allowed_urls = set()
_st_lock = threading.Lock()

def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def load_stations():
    lst = []
    ini = os.path.join(YTUNER_DIR, "config", "stations.ini")
    try:
        cat = ""
        for raw in open(ini, encoding="utf-8", errors="replace"):
            raw = raw.strip()
            if not raw or raw.startswith(";"): continue
            if raw.startswith("[") and raw.endswith("]"):
                cat = raw[1:-1]; continue
            if "=" in raw:
                name, rest = raw.split("=", 1)
                parts = rest.split("|")
                lst.append({"name": name.strip(), "url": parts[0].strip(),
                            "logo": parts[1].strip() if len(parts) > 1 else "",
                            "cat": cat, "src": "my"})
    except OSError:
        pass
    bm = os.path.join(YTUNER_DIR, "config", "bookmark.xml")
    try:
        x = open(bm, encoding="utf-8", errors="replace").read()
        for blk in re.findall(r"<Station>(.*?)</Station>", x, re.S):
            n = re.search(r"<StationName>(.*?)</StationName>", blk, re.S)
            u = re.search(r"<StationUrl>(.*?)</StationUrl>", blk, re.S)
            if n and u:
                lst.append({"name": n.group(1).strip(), "url": u.group(1).strip(),
                            "logo": "", "cat": "Bookmark", "src": "bm"})
    except OSError:
        pass
    db = os.path.join(YTUNER_DIR, "db", "rb.db")
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        for name, url, ures, fav in c.execute(
                "select name,url,url_resolved,favicon from stations"):
            lst.append({"name": name, "url": ures or url, "logo": fav or "",
                        "cat": "Radio-browser", "src": "rb"})
        c.close()
    except Exception:
        pass
    with _st_lock:
        _stations[:] = lst
        _allowed_urls.clear()
        _allowed_urls.update(s["url"] for s in lst)
        _allowed_urls.update(s["url"] for s in _rb_known.values())

def match_station(lines):
    """Finds the station whose name appears in the NSE lines (iRadio)."""
    with _st_lock:
        sts = list(_stations)
    cand = [l for l in lines if l and len(_norm(l)) >= 3]
    for l in cand:                                   # exact match
        n = _norm(l)
        for s in sts:
            if _norm(s["name"]) == n: return s
    for l in cand:                                   # one contains the other
        n = _norm(l)
        best = None
        for s in sts:
            sn = _norm(s["name"])
            if len(sn) >= 4 and (sn in n or n in sn):
                if not best or len(sn) > len(_norm(best["name"])): best = s
        if best: return best
    for l in cand:                                   # radio-browser stations seen so far
        s = _rb_known.get(_norm(l))
        if s: return s
    rb_lookup_bg(cand[:3])
    return None

# ---------------------------------------------------------------- Radio-browser (live catalogue, ~50k stations)
# The same catalogue YTuner shows on the Denon. Queried live; results are cached.
RB_HOSTS = ["de1.api.radio-browser.info", "de2.api.radio-browser.info", "fi1.api.radio-browser.info"]
_rb_cache = {}           # query -> (time, data)
_rb_known = {}           # normalised name -> station (everything the panel has seen)
_rb_tried = set()        # NSE lines already looked up by name
_rb_lock = threading.Lock()
_rb_by_url = {}          # stream url -> station (for logos)

def rb_get(path, params, ttl=900):
    key = path + "?" + urlencode(sorted(params.items()))
    with _rb_lock:
        hit = _rb_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    err = None
    for h in RB_HOSTS:
        try:
            req = Request("https://%s/json/%s?%s" % (h, path, urlencode(params)),
                          headers={"User-Agent": "DenonStudio/1.0"})
            data = json.loads(urlopen(req, timeout=8).read().decode("utf-8", "replace"))
            with _rb_lock:
                if len(_rb_cache) > 400: _rb_cache.clear()
                _rb_cache[key] = (time.time(), data)
            return data
        except Exception as e:
            err = e
    raise IOError(str(err))

def rb_slim(x):
    return {"name": (x.get("name") or "").strip(),
            "url": (x.get("url_resolved") or x.get("url") or "").strip(),
            "logo": x.get("favicon") or "", "cat": x.get("country") or "Radio-browser",
            "cc": x.get("countrycode") or "", "tags": x.get("tags") or "",
            "codec": x.get("codec") or "", "br": x.get("bitrate") or 0,
            "votes": x.get("votes") or 0, "home": x.get("homepage") or "", "src": "rbl"}

def rb_remember(lst):
    with _st_lock:
        if len(_rb_known) > 30000: _rb_known.clear()
        for s in lst:
            if not s["url"].startswith(("http://", "https://")): continue
            _allowed_urls.add(s["url"])
            _rb_known.setdefault(_norm(s["name"]), s)
            _rb_by_url.setdefault(s["url"], s)

def rb_search(name="", cc="", tag="", order="votes", offset=0, limit=60):
    p = {"hidebroken": "true", "limit": str(limit), "offset": str(max(0, offset)),
         "order": order if order in ("votes", "clickcount", "name", "bitrate", "random") else "votes",
         "reverse": "false" if order == "name" else "true"}
    if name: p["name"] = name
    if cc: p["countrycode"] = cc
    if tag: p["tag"] = tag; p["tagExact"] = "true"
    lst = [rb_slim(x) for x in rb_get("stations/search", p)]
    lst = [s for s in lst if s["url"]]
    rb_remember(lst)
    return lst

def rb_lookup_bg(lines):
    """Station name shown on the Denon that we don't know yet -> look it up by name."""
    todo = [l.strip() for l in lines if l and len(_norm(l)) >= 3 and l.strip() not in _rb_tried]
    if not todo: return
    for l in todo: _rb_tried.add(l)
    if len(_rb_tried) > 5000: _rb_tried.clear()
    def run():
        for l in todo:
            try: rb_search(name=l, limit=10)
            except Exception: pass
    threading.Thread(target=run, daemon=True).start()

# ---------------------------------------------------------------- station logos
# Radio-browser has a logo for only ~1/3 of the stations and some of them are dead,
# so: favicon from radio-browser -> icons on the station's homepage -> /favicon.ico.
# Found logos are kept in ./logos (disk), misses are remembered for a day.
LOGO_DIR = os.path.join(HERE, "logos")
_logo_ctx = ssl.create_default_context()
_logo_ctx.check_hostname = False; _logo_ctx.verify_mode = ssl.CERT_NONE   # many radio sites have bad certs
_logo_miss = {}
_logo_locks = collections.defaultdict(threading.Lock)

def _fetch(url, limit):
    r = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DenonStudio/1.0"}),
                timeout=6, context=_logo_ctx)
    return r.geturl(), (r.headers.get("Content-Type") or "").split(";")[0].strip().lower(), r.read(limit)

def _img(url):
    try:
        if not url.startswith(("http://", "https://")): return None
        _, ct, d = _fetch(url, 3_000_000)
        if len(d) < 100 or len(d) >= 3_000_000: return None
        if not ct.startswith("image/"):
            if d[:4] == b"\x89PNG": ct = "image/png"
            elif d[:3] == b"\xff\xd8\xff": ct = "image/jpeg"
            elif d[:4] == b"\x00\x00\x01\x00": ct = "image/x-icon"
            else: return None
        return ct, d
    except Exception:
        return None

def _home_icons(home):
    try:
        base, ct, b = _fetch(home, 600_000)
    except Exception:
        return [urljoin(home, "/favicon.ico")]
    h = b.decode("utf-8", "replace"); cands = []
    for tag in re.findall(r"<link\b[^>]*>", h, re.I):
        rel = re.search(r"rel=[\"']([^\"']+)", tag, re.I); href = re.search(r"href=[\"']([^\"']+)", tag, re.I)
        if rel and href and "icon" in rel.group(1).lower():
            sz = re.search(r"sizes=[\"'](\d+)", tag)
            score = int(sz.group(1)) if sz else (180 if "apple" in rel.group(1).lower() else 16)
            if href.group(1).lower().endswith(".svg"): score = 5
            cands.append((score, urljoin(base, href.group(1))))
    m = re.search(r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)", h, re.I)
    if m: cands.append((100, urljoin(base, m.group(1))))
    cands.append((1, urljoin(base, "/favicon.ico")))
    return [u for _, u in sorted(cands, key=lambda c: -c[0])]

def station_logo(st):
    key = hashlib.sha1((st.get("logo", "") + "|" + st.get("home", "") + "|" + st["url"]).encode()).hexdigest()
    path = os.path.join(LOGO_DIR, key)
    with _logo_locks[key]:
        try:
            ct, d = open(path + ".type").read().strip(), open(path, "rb").read()
            return ct, d
        except OSError:
            pass
        if time.time() - _logo_miss.get(key, 0) < 86400: return None
        res = _img(st.get("logo", ""))
        if not res and st.get("home", "").startswith(("http://", "https://")):
            dead, tries = set(), 0
            for u in _home_icons(st["home"]):
                folder = u.rsplit("/", 1)[0]
                if folder in dead: continue      # sites often list 10 icons in one missing folder
                if tries >= 6: break
                tries += 1
                res = _img(u)
                if res: break
                dead.add(folder)
        if not res:
            _logo_miss[key] = time.time(); return None
        try:
            os.makedirs(LOGO_DIR, exist_ok=True)
            open(path, "wb").write(res[1]); open(path + ".type", "w").write(res[0])
        except OSError:
            pass
        return res

MY_SECTION = "Denon Studio"
def add_to_ytuner(name, url, logo=""):
    """Writes the station to YTuner stations.ini -> Denon: Internet Radio > My Stations."""
    ini = os.path.join(YTUNER_DIR, "config", "stations.ini")
    name = re.sub(r"[=|\[\]\r\n;]+", " ", name).strip()[:80] or "Stanica"
    lines = open(ini, encoding="utf-8", errors="replace").read().splitlines()
    if any(l.split("=", 1)[1].split("|")[0].strip() == url for l in lines if "=" in l):
        return False, "već postoji u YTuneru"
    entry = "%s=%s%s" % (name, url, "|" + logo if logo and "|" not in logo else "")
    hdr = "[%s]" % MY_SECTION
    if hdr in [l.strip() for l in lines]:
        i = [l.strip() for l in lines].index(hdr) + 1
        while i < len(lines) and not lines[i].strip().startswith("["): i += 1
        while i > 0 and not lines[i - 1].strip(): i -= 1
        lines.insert(i, entry)
    else:
        if lines and lines[-1].strip(): lines.append("")
        lines += [hdr, entry]
    open(ini, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    load_stations()
    return True, "dodato u YTuner (My Stations → %s)" % MY_SECTION

# ---------------------------------------------------------------- YTuner process
_yt = {"proc": None, "log": collections.deque(maxlen=200), "started_by_us": False}

def ytuner_pid():
    for p in os.listdir("/proc"):
        if not p.isdigit(): continue
        try:
            if open("/proc/%s/comm" % p).read().strip() == "ytuner":
                return int(p)
        except OSError:
            pass
    return None

def _port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.4).close(); return True
    except OSError:
        return False

def _pump(proc):
    for line in iter(proc.stdout.readline, b""):
        _yt["log"].append(line.decode("utf-8", "replace").rstrip())
    rc = proc.wait()
    why = {126: "lozinka nije uneta / prozor zatvoren", 127: "pogrešna lozinka ili nema prozora za lozinku"}.get(rc, "")
    _yt["log"].append("— YTuner zaustavljen (kod %s%s) —" % (rc, (": " + why) if why else ""))

def ytuner_start():
    if ytuner_pid(): return True, "već radi"
    if not os.path.exists(YTUNER_BIN): return False, "ytuner nije nađen: " + YTUNER_BIN
    if os.geteuid() == 0:
        cmd = [YTUNER_BIN]
    else:
        # ports 80/53 need root -> system password dialog (polkit)
        cmd = ["pkexec", "/bin/sh", "-c", "cd '%s' && exec ./ytuner" % YTUNER_DIR]
    try:
        p = subprocess.Popen(cmd, cwd=YTUNER_DIR, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        return False, str(e)
    _yt["proc"] = p; _yt["started_by_us"] = True
    _yt["log"].append("— pokrećem YTuner (%s) —" % ("root" if os.geteuid() == 0 else "pkexec"))
    threading.Thread(target=_pump, args=(p,), daemon=True).start()
    return True, "pokrećem YTuner…"

def ytuner_stop():
    pid = ytuner_pid()
    if not pid: return True, "nije pokrenut"
    try:
        os.kill(pid, signal.SIGTERM); return True, "zaustavljen"
    except PermissionError:
        subprocess.Popen(["pkexec", "kill", str(pid)])
        return True, "gasim (traži lozinku)"
    except ProcessLookupError:
        return True, "nije pokrenut"

def ytuner_status():
    pid = ytuner_pid()
    return {"running": bool(pid), "pid": pid, "web": _port_open(80),
            "log": list(_yt["log"])[-14:]}

# ---------------------------------------------------------------- stream proxy (for VU)
PLAYLIST_TYPES = ("audio/x-scpls", "audio/scpls", "audio/x-mpegurl", "audio/mpegurl",
                  "application/pls+xml", "application/x-mpegurl")

class HLSStream(IOError):
    """the station is HLS (.m3u8 segments): no single stream to relay -> ffmpeg reads it (see _transcode)"""
    def __init__(self, url):
        IOError.__init__(self, "HLS"); self.url = url

def probe_hls(url):
    """codec / kbps / kHz / channels of an HLS station via ffprobe"""
    try:
        j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-of", "json", "-show_entries",
            "stream=codec_type,codec_name,sample_rate,channels,bit_rate:format=bit_rate", url],
            capture_output=True, text=True, timeout=12).stdout or "{}")
    except Exception:
        return {}
    a = next((x for x in j.get("streams", []) if x.get("codec_type") == "audio"), None)
    if not a: return {}
    codec = (a.get("codec_name") or "").lower()
    br = int(a.get("bit_rate") or (j.get("format") or {}).get("bit_rate") or 0) // 1000
    return {"codec": {"aac": "AAC", "mp3": "MP3", "opus": "OPUS", "vorbis": "OGG"}.get(codec, codec.upper()),
            "br": br or None, "sr": int(a.get("sample_rate") or 0) or None, "ch": a.get("channels")}

def open_stream(url, depth=0, info=None):
    if depth > 6: raise IOError("too many redirects")
    u = urlparse(url)
    if u.scheme not in ("http", "https"): raise IOError("bad scheme")
    port = u.port or (443 if u.scheme == "https" else 80)
    s = socket.create_connection((u.hostname, port), timeout=8)
    if u.scheme == "https":
        s = ssl.create_default_context().wrap_socket(s, server_hostname=u.hostname)
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    req = ("GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: DenonStudio/1.0\r\n"
           "Icy-MetaData: 0\r\nAccept: */*\r\nConnection: close\r\n\r\n") % (path, u.netloc)
    s.sendall(req.encode())
    head = b""
    while b"\r\n\r\n" not in head and b"\n\n" not in head:
        d = s.recv(4096)
        if not d: raise IOError("no response")
        head += d
        if len(head) > 32768: raise IOError("header too big")
    sep = b"\r\n\r\n" if b"\r\n\r\n" in head else b"\n\n"
    hdr, body = head.split(sep, 1)
    lines = hdr.decode("latin-1").splitlines()
    m = re.match(r"(?:HTTP/\d\.\d|ICY)\s+(\d+)", lines[0])
    code = int(m.group(1)) if m else 0
    h = {}
    for l in lines[1:]:
        if ":" in l:
            k, v = l.split(":", 1); h[k.strip().lower()] = v.strip()
    if code in (301, 302, 303, 307, 308) and "location" in h:
        s.close()
        return open_stream(urljoin(url, h["location"]), depth + 1, info)
    if code != 200: s.close(); raise IOError("HTTP %d" % code)
    ctype = h.get("content-type", "audio/mpeg").split(";")[0].strip().lower()
    low = u.path.lower()
    if ctype in PLAYLIST_TYPES or low.endswith((".pls", ".m3u")) or ctype.startswith("text/"):
        data = body
        while len(data) < 65536:
            d = s.recv(8192)
            if not d: break
            data += d
        s.close()
        if b"#EXT-X-" in data[:4096]:                  # an HLS playlist served as plain m3u
            raise HLSStream(url)
        m = re.search(rb"(https?://[^\s\"'<>]+)", data)
        if not m: raise IOError("empty playlist")
        return open_stream(m.group(1).decode(), depth + 1, info)
    if ctype in ("application/vnd.apple.mpegurl",) or low.endswith(".m3u8"):
        s.close(); raise HLSStream(url)
    if info is not None: info.update(h)
    return s, body, ctype

_last = {"station": None}

# ---------------------------------------------------------------- stream quality (codec / kbps / kHz / channels)
# Read from the stream itself: MP3 frame headers, AAC ADTS headers, then ICY headers, then radio-browser data.
_quality = {}            # url -> dict, or None while the probe is running
_q_lock = threading.Lock()
_MP3_BR = {1: [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320],
           2: [0,8,16,24,32,40,48,56,64,80,96,112,128,144,160]}
_MP3_SR = {3: [44100,48000,32000], 2: [22050,24000,16000], 0: [11025,12000,8000]}
_AAC_SR = [96000,88200,64000,48000,44100,32000,24000,22050,16000,12000,11025,8000,7350]

def _mp3_info(b):
    for i in range(min(len(b) - 4, 20000)):
        if b[i] != 0xFF or (b[i+1] & 0xE0) != 0xE0: continue
        ver = (b[i+1] >> 3) & 3; layer = (b[i+1] >> 1) & 3
        if ver == 1 or layer != 1: continue
        bri = (b[i+2] >> 4) & 15; sri = (b[i+2] >> 2) & 3
        if bri in (0, 15) or sri == 3: continue
        br = _MP3_BR[1 if ver == 3 else 2][bri]; sr = _MP3_SR[ver][sri]
        flen = (144 if ver == 3 else 72) * br * 1000 // sr + ((b[i+2] >> 1) & 1)
        j = i + flen
        if j + 1 >= len(b) or b[j] != 0xFF or (b[j+1] & 0xE0) != 0xE0: continue
        return {"codec": "MP3", "br": br, "sr": sr, "ch": 1 if (b[i+3] >> 6) == 3 else 2}
    return None

def _adts_info(b):
    for i in range(min(len(b) - 7, 20000)):
        if b[i] != 0xFF or (b[i+1] & 0xF6) != 0xF0: continue
        sri = (b[i+2] >> 2) & 15; ch = ((b[i+2] & 1) << 2) | (b[i+3] >> 6)
        flen = ((b[i+3] & 3) << 11) | (b[i+4] << 3) | (b[i+5] >> 5)
        if sri >= len(_AAC_SR) or flen < 7: continue
        j = i + flen
        if j + 1 >= len(b) or b[j] != 0xFF or (b[j+1] & 0xF6) != 0xF0: continue
        n = tot = 0; k = i                       # average frame size -> bitrate
        while k + 6 < len(b) and b[k] == 0xFF and (b[k+1] & 0xF6) == 0xF0:
            fl = ((b[k+3] & 3) << 11) | (b[k+4] << 3) | (b[k+5] >> 5)
            if fl < 7 or k + fl > len(b): break
            n += 1; tot += fl; k += fl
        sr = _AAC_SR[sri]
        br = round(tot / n * 8 * sr / 1024 / 1000) if n >= 3 else 0
        return {"codec": "AAC", "br": br, "sr": sr, "ch": ch or 2}
    return None

def _quality_from(h, ctype, data, rb=None):
    ct = (ctype or "").lower()
    q = None
    if "mpeg" in ct or "mp3" in ct: q = _mp3_info(data)
    elif "aac" in ct or "mp4" in ct: q = _adts_info(data)
    q = dict(q or {})
    if not q.get("codec"):
        q["codec"] = {"audio/mpeg": "MP3", "audio/aac": "AAC", "audio/aacp": "AAC+", "audio/ogg": "OGG",
                      "application/ogg": "OGG", "audio/flac": "FLAC", "audio/x-flac": "FLAC",
                      "audio/opus": "OPUS"}.get(ct, (ct.split("/")[-1] or "?").upper()[:6])
    if ct == "audio/aacp" and q.get("codec") == "AAC" and q.get("sr") and q["sr"] <= 24000:
        q["codec"] = "HE-AAC"; q["sr"] *= 2                  # SBR doubles the core sample rate
    ai = dict(kv.split("=", 1) for kv in (h.get("ice-audio-info") or h.get("icy-audio-info") or "").split(";") if "=" in kv)
    def num(v):
        m = re.match(r"\s*(\d+)", str(v or "")); return int(m.group(1)) if m else 0
    if not q.get("br"): q["br"] = num(h.get("icy-br")) or num(ai.get("ice-bitrate") or ai.get("bitrate")) or num((rb or {}).get("br"))
    if not q.get("sr"):
        sr = num(h.get("icy-sr")) or num(ai.get("ice-samplerate") or ai.get("samplerate"))
        if sr: q["sr"] = sr
    if not q.get("ch"):
        c = num(ai.get("ice-channels") or ai.get("channels"))
        if c: q["ch"] = c
    return q

def _read_some(s, first, n=16000, t=3.0):
    data = first or b""; end = time.time() + t
    try:
        s.settimeout(1.5)
        while len(data) < n and time.time() < end:
            d = s.recv(8192)
            if not d: break
            data += d
    except Exception:
        pass
    return data

def probe_quality(url, rb=None):
    h = {}
    try:
        try:
            s, first, ctype = open_stream(url, info=h)
        except HLSStream as e:
            q = probe_hls(e.url)
            with _q_lock: _quality[url] = q
            return q
        data = _read_some(s, first)
        s.close()
        q = _quality_from(h, ctype, data, rb)
    except Exception:
        q = {}
    with _q_lock:
        _quality[url] = q
    return q

def quality_get(url):
    with _q_lock:
        if url in _quality: return _quality[url]
        _quality[url] = None
    with _st_lock:
        rb = _rb_by_url.get(url)
    threading.Thread(target=probe_quality, args=(url, rb), daemon=True).start()
    return None

# ---------------------------------------------------------------- edit "My stations" (YTuner stations.ini)
def edit_my(url, new_name=None):
    """new_name=None -> delete the station; otherwise rename it. Keeps a copy as stations.ini.prev."""
    ini = os.path.join(YTUNER_DIR, "config", "stations.ini")
    text = open(ini, encoding="utf-8", errors="replace").read()
    lines = text.splitlines(); out = []; found = False
    for l in lines:
        t = l.strip()
        if "=" in t and not t.startswith((";", "[")) and t.split("=", 1)[1].split("|")[0].strip() == url:
            found = True
            if new_name is None: continue
            nm = re.sub(r"[=|\[\]\r\n;]+", " ", new_name).strip()[:80]
            if not nm: return False, "ime ne može biti prazno"
            l = nm + "=" + t.split("=", 1)[1]
        out.append(l)
    if not found: return False, "stanica nije nađena u stations.ini"
    clean = []                                   # drop sections that became empty
    for i, l in enumerate(out):
        t = l.strip()
        if t.startswith("[") and t.endswith("]"):
            j, has = i + 1, False
            while j < len(out) and not out[j].strip().startswith("["):
                if "=" in out[j] and not out[j].strip().startswith(";"): has = True; break
                j += 1
            if not has: continue
        clean.append(l)
    res = re.sub(r"\n{3,}", "\n\n", "\n".join(clean).strip("\n")) + "\n"
    open(ini + ".prev", "w", encoding="utf-8").write(text)
    open(ini, "w", encoding="utf-8").write(res)
    load_stations()
    return True, ("obrisano iz Mojih stanica" if new_name is None else "preimenovano")

# ---------------------------------------------------------------- DLNA: play any station on the Denon
# The AVR-X1000 is a DLNA MediaRenderer (port 8080). We give it the URL of OUR /stream proxy,
# so https streams, playlists and redirects work too (the Denon only gets plain http/mp3).
AVT_CTRL = "http://%s:8080/AVTransport/ctrl" % DENON_IP
_dlna = {"station": None}

def _xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))

def avt(action, args=""):
    body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            '<u:%s xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID>%s</u:%s>'
            '</s:Body></s:Envelope>') % (action, args, action)
    r = Request(AVT_CTRL, body.encode(), {"Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#%s"' % action})
    return urlopen(r, timeout=8).read().decode("utf-8", "replace")

def my_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((DENON_IP, 9)); return s.getsockname()[0]
    finally: s.close()

def dlna_play(name, url, ctype="audio/mpeg"):
    # Over DLNA the X1000 takes AAC only inside MP4 files, not live AAC/AAC+ (ADTS) streams,
    # so everything that is not MP3 is converted to MP3 on the fly (ffmpeg) by our /stream proxy.
    # (no "&" in this URL: the Denon un-escapes the DIDL one time too few and would get "&amp;")
    path = "/stream" if ctype in ("audio/mpeg", "audio/mp3", "audio/x-mpeg") else "/mp3stream"
    ctype = "audio/mpeg"
    proxied = "http://%s:%d%s?%s" % (my_ip(), LISTEN_PORT, path, urlencode({"u": url}))
    didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="1" parentID="0" restricted="1"><dc:title>%s</dc:title>'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            '<res protocolInfo="http-get:*:%s:*">%s</res></item></DIDL-Lite>') % (
            _xml(name), ctype, _xml(proxied))
    avt("SetAVTransportURI", "<CurrentURI>%s</CurrentURI><CurrentURIMetaData>%s</CurrentURIMetaData>"
        % (_xml(proxied), _xml(didl)))
    avt("Play", "<Speed>1</Speed>")

# ---------------------------------------------------------------- MUSIC: local files on the Denon over DLNA
# The page picks a music folder (folder dialog), the server serves the files at /music/<path>
# and tells the Denon to play them one after another (GetTransportInfo polling -> next track).
AUDIO_EXT = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac",
             ".wav": "audio/wav", ".wma": "audio/x-ms-wma"}
USER_HOME = os.path.expanduser("~")
BROWSE_BASES = [USER_HOME, "/media", "/mnt", "/run/media"]
MUSIC_CONF = os.path.join(USER_HOME, ".config", "denon-studio", "music.json")
_mus_lock = threading.Lock()
_mus = {"active": False, "queue": [], "idx": -1, "dir": "", "shuffle": False, "want": "stop",
        "state": "STOPPED", "pos": 0.0, "dur": 0.0, "t": 0.0, "gen": 0, "seen": False, "stopn": 0,
        "started": 0.0, "lastpos": 0.0, "meta": None}
_meta_cache = {}

def music_conf():
    try: return json.load(open(MUSIC_CONF, encoding="utf-8"))
    except Exception: return {"root": "", "recent": []}

def music_conf_save(c):
    try:
        os.makedirs(os.path.dirname(MUSIC_CONF), exist_ok=True)
        json.dump(c, open(MUSIC_CONF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except OSError: pass

def _inside(path, base):
    path, base = os.path.realpath(path), os.path.realpath(base)
    return path == base or path.startswith(base.rstrip("/") + "/")

def browsable(p):
    return bool(p) and os.path.isdir(p) and any(_inside(p, b) for b in BROWSE_BASES if os.path.isdir(b))

def music_root():
    r = music_conf().get("root") or ""
    return r if browsable(r) else ""

def music_path(rel):
    """rel path inside the music folder -> absolute path, or None if it leaves the folder."""
    root = music_root()
    if not root: return None
    p = os.path.realpath(os.path.join(root, (rel or "").lstrip("/")))
    return p if _inside(p, root) else None

def _is_audio(name):
    return os.path.splitext(name)[1].lower() in AUDIO_EXT and not name.startswith(".")

_JUNK = re.compile(r"\s*[\(\[][^\)\]]*(official|video|audio|lyric|visuali[sz]er|\bhd\b|\bhq\b|4k|remaster)[^\)\]]*[\)\]]", re.I)

def split_name(stem):
    """'Artist - Title (Official Video)' -> ('Artist', 'Title')"""
    s = _JUNK.sub("", stem.replace("_", " ")).strip()
    s = re.sub(r"\s+(HQ|HD|4K)$", "", s, flags=re.I).strip()
    s = re.sub(r"^\d{1,3}\s*[-.)]\s*", "", s)                # leading track number
    m = re.search(r"\s+[-–—]\s*|\s*[-–—]\s+", s)
    if m and m.start() > 0 and m.end() < len(s):
        return s[:m.start()].strip(), s[m.end():].strip()
    return "", s

def scan_dir(path):
    dirs, files = [], []
    try:
        for e in os.scandir(path):
            if e.name.startswith("."): continue
            try:
                if e.is_dir(): dirs.append(e)
                elif e.is_file() and _is_audio(e.name): files.append(e)
            except OSError: pass
    except OSError: pass
    key = lambda e: e.name.lower()
    return sorted(dirs, key=key), sorted(files, key=key)

def count_audio(path):
    d, f = scan_dir(path)
    return len(f), len(d)

def fs_roots():
    roots = [{"name": "Lični folder", "path": USER_HOME, "kind": "home"}]
    names = {}
    try:
        out = subprocess.run(["lsblk", "-rno", "MOUNTPOINT,SIZE,LABEL"], capture_output=True, text=True, timeout=3).stdout
        for ln in out.splitlines():
            p = ln.split(" ")
            if len(p) >= 2 and p[0]:
                mp = p[0].encode().decode("unicode_escape")
                lab = " ".join(p[2:]).encode().decode("unicode_escape").strip()
                names[mp] = (lab or "Disk") + " · " + p[1].replace(",", ".")
    except Exception: pass
    for base in ("/media/" + os.path.basename(USER_HOME), "/media", "/mnt", "/run/media/" + os.path.basename(USER_HOME)):
        try:
            for e in sorted(os.scandir(base), key=lambda e: e.name):
                if base == "/media" and not os.path.ismount(e.path):
                    try: pwd.getpwnam(e.name); continue          # /media/<user> folder, not a disk
                    except KeyError: pass
                if e.is_dir() and not e.name.startswith(".") and e.path != "/media/" + os.path.basename(USER_HOME) \
                        and not any(r["path"] == e.path for r in roots) and os.access(e.path, os.R_OK | os.X_OK):
                    roots.append({"name": names.get(e.path, e.name), "path": e.path, "kind": "disk"})
        except OSError: pass
    return roots

def fs_list(p):
    p = os.path.realpath(p or USER_HOME)
    if not browsable(p): p = USER_HOME
    dirs, files = scan_dir(p)
    out = []
    for e in dirs[:400]:
        n, nd = count_audio(e.path)
        out.append({"name": e.name, "path": e.path, "n": n, "nd": nd})
    parent = os.path.dirname(p)
    return {"ok": True, "path": p, "parent": parent if browsable(parent) and parent != p else "",
            "dirs": out, "n": len(files), "roots": fs_roots(),
            "recent": [r for r in music_conf().get("recent", []) if browsable(r)], "root": music_root()}

def music_list(rel):
    root = music_root()
    if not root: return {"ok": False, "noroot": True}
    p = music_path(rel)
    if not p or not os.path.isdir(p): p = root
    rel = os.path.relpath(p, root); rel = "" if rel == "." else rel
    dirs, files = scan_dir(p)
    tracks = []
    for e in files:
        a, t = split_name(os.path.splitext(e.name)[0])
        tracks.append({"f": os.path.join(rel, e.name) if rel else e.name, "artist": a, "title": t,
                       "ext": os.path.splitext(e.name)[1][1:].upper()})
    return {"ok": True, "root": root, "rootName": os.path.basename(root) or root, "rel": rel,
            "dirs": [dict(zip(("name", "n", "nd"), (e.name,) + count_audio(e.path)), rel=os.path.join(rel, e.name) if rel else e.name)
                     for e in dirs],
            "tracks": tracks}

def scan_duration(p):
    """exact length: let ffmpeg walk all audio packets without decoding (~0.2 s per song)"""
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "quiet", "-stats", "-i", p, "-map", "0:a", "-c", "copy",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=20)
        t = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if t: h, mi, sec = t[-1]; return int(h) * 3600 + int(mi) * 60 + float(sec)
    except Exception: pass
    return 0.0

def track_meta(rel):
    p = music_path(rel)
    try: key = (p, os.path.getmtime(p))
    except (OSError, TypeError): key = None
    if key in _meta_cache: return _meta_cache[key]
    a, t = split_name(os.path.splitext(os.path.basename(rel))[0])
    m = {"f": rel, "artist": a, "title": t, "album": "", "dur": 0.0, "q": None}
    try:
        j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-of", "json", "-show_entries",
            "format=duration,bit_rate:format_tags=title,artist,album:stream=codec_type,codec_name,sample_rate,channels,bit_rate",
            p], capture_output=True, text=True, timeout=6).stdout or "{}")
        fmt = j.get("format", {}); tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        aus = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), {})
        if not a:                       # no "Artist - Title" in the file name -> trust the tags
            m["artist"] = (tags.get("artist") or "").strip()
            m["title"] = (tags.get("title") or "").strip() or t
        m["album"] = (tags.get("album") or "").strip()
        if m["album"].startswith("["): m["album"] = ""
        m["dur"] = float(fmt.get("duration") or 0)
        exact = scan_duration(p)          # header/bitrate guesses are often way off for VBR mp3
        if exact: m["dur"] = exact
        codec = (aus.get("codec_name") or "").lower()
        br = int(aus.get("bit_rate") or fmt.get("bit_rate") or 0) // 1000
        m["q"] = {"codec": "WAV" if codec.startswith("pcm") else {"mp3": "MP3", "flac": "FLAC", "aac": "AAC",
                  "alac": "ALAC", "wmav2": "WMA", "wmav1": "WMA"}.get(codec, codec.upper()),
                  "br": br or None, "sr": int(aus.get("sample_rate") or 0) or None, "ch": aus.get("channels")}
    except Exception: pass
    if key: _meta_cache[key] = m
    return m

COVER_DIR = os.path.join(USER_HOME, ".cache", "denon-studio", "covers")
_cover_sem = threading.Semaphore(3)
_net_lock = threading.Lock()          # one online lookup at a time (Deezer allows ~10/s, we stay far below)
_net_last = [0.0]
_served = {}                          # path -> last time the Denon/page read it (don't rewrite a playing file)
_embed_todo = {}                      # path -> picture waiting to be written into the mp3

def _thumb(src_args, data=None):
    r = subprocess.run(["ffmpeg", "-v", "error"] + src_args + ["-an", "-frames:v", "1", "-vf",
                        "scale=160:160:force_original_aspect_ratio=increase,crop=160:160",
                        "-c:v", "mjpeg", "-q:v", "4", "-f", "image2", "pipe:1"], input=data, capture_output=True, timeout=15)
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8" else None

def _fold(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower().replace("đ", "dj"))
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()

def _same_artist(a, b):
    a, b = _fold(a), _fold(b)
    if not a or not b: return False
    if a in b or b in a: return True
    wa, wb = set(a.split()) - {"the", "and", "feat", "ft"}, set(b.split()) - {"the", "and", "feat", "ft"}
    return bool(wa & wb)

def _song_tags(p):
    """artist + title the same way the music list shows them (file name first, then tags)"""
    a, t = split_name(os.path.splitext(os.path.basename(p))[0])
    if a: return a, t
    try:
        import mutagen
        f = mutagen.File(p, easy=True)
        if f and f.tags:
            return ((f.tags.get("artist") or [""])[0].strip(), (f.tags.get("title") or [t])[0].strip() or t)
    except Exception: pass
    return "", t

def find_cover_online(artist, title):
    """album cover from Deezer, else iTunes; only if the artist matches. Returns jpg bytes or None.
       Raises on network trouble, so a missing internet connection is not remembered as 'no cover'."""
    if not artist or not title: return None
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip() or title
    def get(url):
        with _net_lock:
            time.sleep(max(0, _net_last[0] + 0.3 - time.time())); _net_last[0] = time.time()
            return urlopen(Request(url, headers={"User-Agent": "DenonStudio/1.0"}), timeout=8).read()
    pic = None
    for it in json.loads(get("https://api.deezer.com/search?" + urlencode({"q": artist + " " + title, "limit": 5}))).get("data") or []:
        if _same_artist(artist, (it.get("artist") or {}).get("name")):
            pic = (it.get("album") or {}).get("cover_xl") or (it.get("album") or {}).get("cover_big"); break
    if not pic:
        for it in json.loads(get("https://itunes.apple.com/search?" + urlencode(
                {"term": artist + " " + title, "entity": "song", "limit": 5}))).get("results") or []:
            if _same_artist(artist, it.get("artistName")) and it.get("artworkUrl100"):
                pic = it["artworkUrl100"].replace("100x100bb", "600x600bb"); break
    if not pic: return None
    data = get(pic)
    return data if data[:2] == b"\xff\xd8" else None

def embed_cover(p, img):
    """write the picture into the mp3 (ID3 APIC, front cover). The file is rebuilt as a copy and swapped in,
       keeping its date, so a half-written file can never be left behind."""
    tmp = os.path.join(os.path.dirname(p), ".%s.cover-tmp" % os.path.basename(p))
    st = os.stat(p)
    try:
        try:
            from mutagen.id3 import ID3, APIC, ID3NoHeaderError
            import shutil
            shutil.copyfile(p, tmp)
            try: tags = ID3(tmp); v = 4 if tags.version[1] == 4 else 3
            except ID3NoHeaderError: tags, v = ID3(), 3
            if tags.getall("APIC"): os.remove(tmp); return True
            if v == 3: tags.update_to_v23()
            tags.add(APIC(encoding=3 if v == 4 else 1, mime="image/jpeg", type=3, desc="Cover", data=img))
            tags.save(tmp, v2_version=v)
        except ImportError:                         # no mutagen: let ffmpeg do it
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", p, "-f", "image2pipe", "-i", "pipe:0",
                                "-map", "0:a", "-map", "1", "-c", "copy", "-id3v2_version", "3",
                                "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
                                "-f", "mp3", tmp], input=img, capture_output=True, timeout=60)
            if r.returncode != 0: raise OSError(r.stderr.decode(errors="replace")[-200:])
        os.utime(tmp, (st.st_atime, st.st_mtime))
        try: os.chown(tmp, st.st_uid, st.st_gid)
        except OSError: pass
        os.replace(tmp, p)
        return True
    except Exception as e:
        print("cover: could not write into %s: %s" % (p, e))
        try: os.remove(tmp)
        except OSError: pass
        return False

def _try_embed(p):
    img = _embed_todo.get(p)
    if img is None or time.time() - _served.get(p, 0) < 600: return     # playing now -> later
    _embed_todo.pop(p, None)
    embed_cover(p, img)

def cover_for(rel):
    """album art for a song: picture inside the file, else cover/folder.jpg next to it,
       else found online by artist + title (and then written into the mp3). Cached as small jpg."""
    p = music_path(rel)
    if not p or not os.path.isfile(p): return None
    key = hashlib.sha1(("%s|%s" % (p, os.path.getmtime(p))).encode()).hexdigest()
    jpg = os.path.join(COVER_DIR, key + ".jpg")
    if os.path.exists(jpg):
        if p in _embed_todo: _try_embed(p)
        return open(jpg, "rb").read()
    if os.path.exists(jpg + ".none2"): return None
    online = False
    with _cover_sem:
        data = _thumb(["-i", p])
        if not data:
            d = os.path.dirname(p)
            pic = next((os.path.join(d, n) for n in sorted(os.listdir(d))
                        if n.lower() in ("cover.jpg", "folder.jpg", "front.jpg", "cover.png", "folder.png", "albumart.jpg")), None)
            if pic: data = _thumb(["-i", pic])
        if not data:
            try: big = find_cover_online(*_song_tags(p))
            except Exception as e:
                print("cover: online lookup failed: %s" % e); return None     # offline -> try again next time
            if big:
                data = _thumb(["-f", "image2pipe", "-i", "pipe:0"], big)
                if data and p.lower().endswith(".mp3"):
                    _embed_todo[p] = big; online = True
    try:
        os.makedirs(COVER_DIR, exist_ok=True)
        if data: open(jpg, "wb").write(data)
        else: open(jpg + ".none2", "w").close()
    except OSError: pass
    if online: _try_embed(p)
    return data

def _tag(xml, name):
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), xml, re.S)
    return m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&") if m else ""

def _secs(t):
    try:
        h, mi, s = t.split(":"); return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception: return 0.0

def _hms(s):
    s = max(0, int(s)); return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)

def music_url(rel):
    from urllib.parse import quote
    return "http://%s:%d/music/%s" % (my_ip(), LISTEN_PORT, quote(rel, safe="/"))

SIDE_LEN = 45 * 60          # a C-90 mixtape: 45 minutes per side, then auto-reverse to the other side

def music_start(idx, fresh=False):
    """send queue[idx] to the Denon (call with _mus_lock NOT held: it talks to the network)"""
    with _mus_lock:
        if not (0 <= idx < len(_mus["queue"])): return False, "nema pesme"
        rel = _mus["queue"][idx]
        if fresh:
            _mus.update(side="A", side_acc=0.0)
        elif _mus["active"] and _mus["meta"]:
            _mus["side_acc"] = _mus.get("side_acc", 0.0) + music_pos()     # what the previous song used of the side
            if _mus["side_acc"] >= SIDE_LEN:                                # side full -> turn the tape over
                _mus.update(side="B" if _mus.get("side") == "A" else "A", side_acc=0.0,
                            flip_n=_mus.get("flip_n", 0) + 1)
    meta = track_meta(rel)
    p = music_path(rel)
    if not p or not os.path.isfile(p): return False, "fajl ne postoji"
    ctype = AUDIO_EXT.get(os.path.splitext(p)[1].lower(), "audio/mpeg")
    url = music_url(rel)
    pn = "DLNA.ORG_PN=MP3;" if ctype == "audio/mpeg" else ""
    proto = "http-get:*:%s:%sDLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000" % (ctype, pn)
    dur = ' duration="%s.000"' % _hms(meta["dur"]) if meta["dur"] else ""
    didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="m%d" parentID="0" restricted="1"><dc:title>%s</dc:title><dc:creator>%s</dc:creator>'
            '<upnp:artist>%s</upnp:artist><upnp:album>%s</upnp:album>'
            '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            '<res protocolInfo="%s" size="%d"%s>%s</res></item></DIDL-Lite>') % (
            idx, _xml(meta["title"]), _xml(meta["artist"]), _xml(meta["artist"]), _xml(meta["album"]),
            proto, os.path.getsize(p), dur, _xml(url))
    if not STATE.get("power"):
        send_cmd(["PWON"]); time.sleep(2.5)
    with _mus_lock:
        _mus["gen"] += 1
        _mus.update(active=True, idx=idx, want="play", state="TRANSITIONING", pos=0.0, dur=meta["dur"], url=url, eof_t=0.0, path=p,
                    t=time.time(), seen=False, stopn=0, started=time.time(), lastpos=0.0, meta=meta)
    _dlna["station"] = None
    avt("SetAVTransportURI", "<CurrentURI>%s</CurrentURI><CurrentURIMetaData>%s</CurrentURIMetaData>"
        % (_xml(url), _xml(didl)))
    avt("Play", "<Speed>1</Speed>")
    return True, "▶ " + (meta["artist"] + " — " if meta["artist"] else "") + meta["title"]

def music_play(rel_dir, rel_file, shuffle):
    p = music_path(rel_dir)
    if not p or not os.path.isdir(p): return False, "fascikla ne postoji"
    _, files = scan_dir(p)
    rd = os.path.relpath(p, music_root()); rd = "" if rd == "." else rd
    q = [os.path.join(rd, e.name) if rd else e.name for e in files]
    if not q: return False, "u fascikli nema pesama"
    first = q.index(rel_file) if rel_file in q else 0
    if shuffle:
        import random
        rest = q[:first] + q[first + 1:] if rel_file in q else q[:]
        random.shuffle(rest)
        q = ([q[first]] if rel_file in q else []) + rest; first = 0
    with _mus_lock:
        _mus.update(queue=q, dir=rd, shuffle=bool(shuffle))
    return music_start(first, fresh=True)

def music_ctl(a, t=0.0):
    with _mus_lock:
        st, idx, n, pos = _mus["state"], _mus["idx"], len(_mus["queue"]), _mus["pos"]
        active = _mus["active"]
    if not active and a != "stop": return False, "ništa ne svira"
    if a == "pause":
        avt("Pause")
        with _mus_lock: _mus.update(want="pause", state="PAUSED_PLAYBACK", pos=music_pos(), t=time.time()); _mus["gen"] += 1
        return True, "❚❚ pauza"
    if a == "play":
        if st in ("STOPPED", "NO_MEDIA_PRESENT") or _mus["want"] == "stop":
            return music_start(max(0, idx))
        avt("Play", "<Speed>1</Speed>")
        with _mus_lock: _mus.update(want="play", state="PLAYING", t=time.time()); _mus["gen"] += 1
        return True, "▶ nastavljam"
    if a == "next":
        if idx + 1 >= n: return False, "ovo je poslednja pesma"
        return music_start(idx + 1)
    if a == "prev":
        if music_pos() > 4 or idx == 0:
            a, t = "seek", 0.0
        else:
            return music_start(idx - 1)
    if a == "seek":
        avt("Seek", "<Unit>REL_TIME</Unit><Target>%s</Target>" % _hms(t))
        with _mus_lock: _mus.update(pos=float(int(t)), t=time.time(), stopn=0); _mus["gen"] += 1
        return True, ("⇥ %d:%02d" % (t // 60, t % 60)) if t >= 1 else "⇤ od početka"
    if a == "stop":
        try: avt("Stop")
        except Exception: pass
        with _mus_lock: _mus.update(active=False, want="stop", state="STOPPED"); _mus["gen"] += 1
        return True, "⏏ kaseta izbačena"
    if a == "shuffle":
        import random
        with _mus_lock:
            cur = _mus["queue"][idx] if 0 <= idx < n else None
            if _mus["shuffle"]:
                p = music_path(_mus["dir"]); _, files = scan_dir(p) if p else ([], [])
                d = _mus["dir"]; q = [os.path.join(d, e.name) if d else e.name for e in files]
                _mus.update(queue=q, shuffle=False)
            else:
                q = _mus["queue"][:]; rest = [x for x in q if x != cur]; random.shuffle(rest)
                _mus.update(queue=([cur] if cur else []) + rest, shuffle=True)
            _mus["idx"] = _mus["queue"].index(cur) if cur in _mus["queue"] else 0
            return True, "🔀 nasumično" if _mus["shuffle"] else "redom, kao u fascikli"
    return False, "?"

def music_pos():
    m = _mus
    pos = m["pos"] + (time.time() - m["t"] if m["state"] == "PLAYING" else 0)
    return min(pos, m["dur"]) if m["dur"] else pos

def music_status():
    with _mus_lock:
        m = _mus
        if not m["active"] or not m["meta"]: return None
        meta = m["meta"]
        return {"active": True, "state": m["state"], "want": m["want"], "pos": round(music_pos(), 2),
                "dur": m["dur"], "idx": m["idx"], "n": len(m["queue"]), "f": meta["f"], "dir": m["dir"],
                "title": meta["title"], "artist": meta["artist"], "album": meta["album"], "q": meta["q"],
                "shuffle": m["shuffle"], "side": m.get("side", "A"), "side_pos": round(m.get("side_acc", 0.0) + music_pos(), 1),
                "side_len": SIDE_LEN, "flip_n": m.get("flip_n", 0)}

def music_loop():
    """follows the Denon: position, end of track -> next one, user switched input -> done"""
    while True:
        time.sleep(1.0)
        if not _mus["active"]: continue
        gen = _mus["gen"]
        try:
            ti = avt("GetTransportInfo"); pi = avt("GetPositionInfo")
        except Exception:
            continue
        state = _tag(ti, "CurrentTransportState") or "STOPPED"
        pos, dur = _secs(_tag(pi, "RelTime")), _secs(_tag(pi, "TrackDuration"))
        uri = _tag(pi, "TrackURI").replace("&amp;", "&")
        advance = False
        with _mus_lock:
            m = _mus
            if gen != m["gen"] or not m["active"]: continue      # a command came in meanwhile
            # right after SetAVTransportURI the Denon still reports the OLD track/position for a moment
            if time.time() - m["started"] < 4: continue
            if uri and m.get("url") and uri != m["url"] and state == "PLAYING": continue
            if state == "TRANSITIONING" and m["want"] == "play": state = "PLAYING" if m["seen"] else state
            m["state"] = state
            if state in ("PLAYING", "PAUSED_PLAYBACK"):
                m["pos"], m["t"] = pos, time.time()
                # the Denon's own length is a guess from the bitrate (2:14 or 17:21 for a 3:32 song):
                # use it only when we could not measure the file ourselves
                if dur > 1 and not (m["meta"] and m["meta"]["dur"]): m["dur"] = dur
            if state == "PLAYING":
                m["seen"] = True; m["lastpos"] = pos; m["stopn"] = 0
                if m["want"] == "pause": m["want"] = "play"          # resumed from the remote
                # at the end of a file the X1000 often hangs in PLAYING (clock runs on, no sound):
                # once its clock is past the (measured) length the song is over
                over = m["dur"] and pos > m["dur"] + 1.5
                if m["want"] == "play" and over:
                    if m["idx"] + 1 < len(m["queue"]): advance = True
                    else: m.update(want="stop", pos=0.0, idx=0, state="STOPPED")
                    print(time.strftime("%H:%M:%S"), "MUSIC end of", m["meta"]["f"] if m["meta"] else "?",
                          "at %.0fs/%.0fs (past end)" % (pos, m["dur"]),
                          "-> next" if advance else "-> stop", flush=True)
            elif state == "PAUSED_PLAYBACK":
                m["want"] = "pause"
            elif state in ("STOPPED", "NO_MEDIA_PRESENT") and m["want"] == "play" and m["seen"]:
                m["stopn"] += 1
                if m["stopn"] >= 2:
                    if m["lastpos"] > 5:                                 # it played and then ended by itself
                        if m["idx"] + 1 < len(m["queue"]): advance = True
                        else: m.update(want="stop", pos=0.0, idx=0)     # end of the tape
                    else:
                        m.update(want="stop")                            # it never really played
                    print(time.strftime("%H:%M:%S"), "MUSIC end of", m["meta"]["f"] if m["meta"] else "?",
                          "at %.0fs/%.0fs" % (m["lastpos"], m["dur"]), "-> next" if advance else "-> stop", flush=True)
            elif state in ("STOPPED", "NO_MEDIA_PRESENT") and m["want"] == "play" and \
                    time.time() - m["started"] > 15:
                m.update(want="stop")                                    # never started
            if STATE.get("power") is False or (STATE.get("input_raw") not in (None, "SERVER")
                                               and time.time() - m["started"] > 8):
                m.update(active=False, want="stop")                      # Denon off / other input
            nxt = m["idx"] + 1
        if advance:
            try: music_start(nxt)
            except Exception: pass

# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        p = u.path
        if p in ("/", "/index.html"):
            try:
                self._send(200, open(os.path.join(HERE, "index.html"), "rb").read(),
                           "text/html; charset=utf-8")
            except OSError:
                self._send(500, "nedostaje index.html", "text/plain")
        elif p == "/api/status":
            with _state_lock:
                st = json.loads(json.dumps(STATE))
            st["inputs"] = input_list()
            st["isNet"] = st["input_raw"] in NET_SOURCES
            stn = None
            if st["input_raw"] in ("IRADIO", "IRP", "FAVORITES", "NET") and st["power"]:
                m = match_station(st["nse"][1:6])
                if m:
                    stn = {"name": m["name"], "url": m["url"], "logo": m["logo"]}
                    _last["station"] = stn
                else:
                    # the Denon display keeps changing (station -> song -> empty):
                    # keep the last recognised station while we stay on the radio
                    stn = _last["station"]
            elif st["input_raw"] == "SERVER" and st["power"] and _dlna["station"]:
                stn = _dlna["station"]          # we sent this station over DLNA
            else:
                _last["station"] = None
            if st["input_raw"] != "SERVER" and time.time() - _dlna.get("t", 0) > 10:
                _dlna["station"] = None         # user switched the Denon to something else
            st["station"] = dict(stn, q=quality_get(stn["url"])) if stn else None
            st["ytuner"] = ytuner_status()
            st["music"] = music_status()
            self._json({"ok": st["connected"], "st": st})
        elif p == "/api/cmd":
            cmds = [c for c in q.get("c", []) if c and len(c) < 40]
            if not cmds: return self._json({"ok": False, "err": "no cmd"}, 400)
            self._json({"ok": send_cmd(cmds)})
        elif p == "/api/stations":
            with _st_lock:
                my = [s for s in _stations if s["src"] in ("my", "bm")]
            self._json({"stations": my})
        elif p == "/api/rb/search":
            g = lambda k, d="": (q.get(k) or [d])[0].strip()
            try:
                off = int(g("offset", "0") or 0)
                lst = rb_search(name=g("name")[:80], cc=g("cc")[:2].upper(), tag=g("tag")[:60],
                                order=g("order", "votes"), offset=off)
                self._json({"ok": True, "stations": lst, "offset": off})
            except Exception as e:
                self._json({"ok": False, "err": "Radio-browser nije dostupan: %s" % e}, 502)
        elif p == "/api/rb/meta":
            try:
                st = rb_get("stats", {}, ttl=3600)
                co = rb_get("countries", {"hidebroken": "true", "order": "name"}, ttl=86400)
                tg = rb_get("tags", {"hidebroken": "true", "order": "stationcount",
                                     "reverse": "true", "limit": "200"}, ttl=86400)
                self._json({"ok": True, "total": st.get("stations"),
                            "countries": [{"cc": c.get("iso_3166_1"), "name": c.get("name"),
                                           "n": c.get("stationcount")} for c in co
                                          if c.get("iso_3166_1") and c.get("stationcount")],
                            "tags": [{"name": t.get("name"), "n": t.get("stationcount")} for t in tg
                                     if t.get("name")]})
            except Exception as e:
                self._json({"ok": False, "err": "Radio-browser nije dostupan: %s" % e}, 502)
        elif p == "/api/rb/add":
            url = (q.get("u") or [""])[0]; name = (q.get("n") or [""])[0]
            with _st_lock:
                known = url in _allowed_urls
                logo = next((s["logo"] for s in _rb_known.values() if s["url"] == url), "")
            if not known:
                return self._json({"ok": False, "msg": "nepoznata stanica"}, 403)
            try: ok, msg = add_to_ytuner(name, url, logo)
            except OSError as e: ok, msg = False, "ne mogu da upišem stations.ini: %s" % e
            self._json({"ok": ok, "msg": msg})
        elif p == "/api/play":
            url = (q.get("u") or [""])[0]; name = (q.get("n") or [""])[0][:120] or "Radio"
            with _st_lock:
                known = url in _allowed_urls
                rb = _rb_by_url.get(url) or next((x for x in _stations if x["url"] == url), None)
            if not known:
                return self._json({"ok": False, "msg": "nepoznata stanica"}, 403)
            try:
                hdr = {}
                s, first, ctype = open_stream(url, info=hdr)   # check that it plays + learn the format
                data = _read_some(s, first)
                s.close()
                with _q_lock: _quality[url] = _quality_from(hdr, ctype, data, rb)
            except HLSStream as e:
                q = probe_hls(e.url)                          # HLS: ffmpeg will turn it into MP3
                if not q: return self._json({"ok": False, "msg": "stanica ne radi (HLS se ne otvara)"}, 502)
                with _q_lock: _quality[url] = q
                ctype = "application/vnd.apple.mpegurl"
            except Exception as e:
                return self._json({"ok": False, "msg": "stanica ne radi: %s" % e}, 502)
            try:
                if not STATE.get("power"):
                    send_cmd(["PWON"]); time.sleep(2.5)
                dlna_play(name, url, ctype)
            except Exception as e:
                return self._json({"ok": False, "msg": "Denon ne prihvata: %s" % e}, 502)
            with _mus_lock: _mus.update(active=False, want="stop"); _mus["gen"] += 1
            _dlna["station"] = {"name": name, "url": url, "logo": (rb or {}).get("logo", "")}
            _dlna["t"] = time.time()
            self._json({"ok": True, "msg": "▶ " + name + " — svira na Denonu"})
        elif p in ("/api/my/del", "/api/my/rename"):
            url = (q.get("u") or [""])[0]; name = (q.get("n") or [""])[0]
            try: ok, msg = edit_my(url, None if p.endswith("del") else name)
            except OSError as e: ok, msg = False, "ne mogu da izmenim stations.ini: %s" % e
            self._json({"ok": ok, "msg": msg})
        elif p == "/api/quit":
            # the app window was closed: stop the music on the Denon, then shut the server down
            msgs = []
            try: avt("Stop"); msgs.append("DLNA stop")
            except Exception: pass
            _dlna["station"] = None
            with _mus_lock: _mus.update(active=False, want="stop"); _mus["gen"] += 1
            if STATE.get("power") and STATE.get("input_raw") in NET_SOURCES:
                send_cmd(["NS9C"]); msgs.append("NET stop")
            self._json({"ok": True, "msg": ", ".join(msgs) or "ništa nije sviralo"})
            threading.Timer(0.5, lambda: os._exit(0)).start()
        elif p == "/api/stop":
            try: avt("Stop"); ok = True
            except Exception: ok = False
            _dlna["station"] = None
            with _mus_lock: _mus.update(active=False, want="stop"); _mus["gen"] += 1
            self._json({"ok": ok, "msg": "zaustavljeno" if ok else "greška"})
        elif p == "/api/ytuner":
            a = (q.get("a") or ["status"])[0]
            if a == "start": ok, msg = ytuner_start()
            elif a == "stop": ok, msg = ytuner_stop()
            elif a == "reload": load_stations(); ok, msg = True, "stanice ponovo učitane"
            else: ok, msg = True, ""
            self._json({"ok": ok, "msg": msg, "yt": ytuner_status()})
        elif p == "/api/fs":
            self._json(fs_list((q.get("p") or [""])[0]))
        elif p == "/api/music/root":
            path = os.path.realpath((q.get("p") or [""])[0] or "/nonexistent")
            if not browsable(path):
                return self._json({"ok": False, "msg": "ta fascikla nije dostupna"}, 403)
            c = music_conf(); c["root"] = path
            c["recent"] = ([path] + [r for r in c.get("recent", []) if r != path])[:6]
            music_conf_save(c)
            n, nd = count_audio(path)
            self._json({"ok": True, "msg": "📁 %s — %d pesama%s" % (os.path.basename(path) or path, n,
                        (", %d fascikli" % nd) if nd else "")})
        elif p == "/api/music/list":
            self._json(music_list((q.get("d") or [""])[0]))
        elif p == "/api/music/play":
            g = lambda k: (q.get(k) or [""])[0]
            try: ok, msg = music_play(g("d"), g("f"), g("sh") == "1")
            except Exception as e: ok, msg = False, "Denon ne prihvata: %s" % e
            self._json({"ok": ok, "msg": msg, "music": music_status()})
        elif p == "/api/music/ctl":
            a = (q.get("a") or [""])[0]
            try: t = float((q.get("t") or ["0"])[0])
            except ValueError: t = 0.0
            try: ok, msg = music_ctl(a, t)
            except Exception as e: ok, msg = False, "Denon ne prihvata: %s" % e
            self._json({"ok": ok, "msg": msg, "music": music_status()})
        elif p.startswith("/music/"):
            self._file(p)
        elif p == "/cover":
            try: data = cover_for((q.get("f") or [""])[0])
            except Exception: data = None
            if not data: return self._send(404, "", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=604800")
            self.end_headers()
            self.wfile.write(data)
        elif p == "/api/jslog":
            import sys
            print(time.strftime("%H:%M:%S"), "PAGE ERROR:", (q.get("m") or [""])[0][:1500], file=sys.stderr, flush=True)
            self._json({"ok": True})
        elif p == "/logo":
            url = (q.get("u") or [""])[0]
            with _st_lock:
                st = _rb_by_url.get(url) or next((x for x in _stations if x["url"] == url), None)
            res = station_logo(st) if st else None
            if not res: return self._send(404, "", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", res[0])
            self.send_header("Content-Length", str(len(res[1])))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(res[1])
        elif p in ("/stream", "/mp3stream"):
            url = (q.get("u") or [""])[0]
            with _st_lock:
                allowed = url in _allowed_urls
            if not allowed:
                return self._send(403, "Dozvoljene su samo YTuner stanice", "text/plain")
            try:
                s, first, ctype = open_stream(url)
            except HLSStream as e:
                return self._transcode(None, b"", src=e.url)     # browser and Denon both get MP3
            except Exception as e:
                return self._send(502, "Greška strima: %s" % e, "text/plain")
            if p == "/mp3stream" and ctype not in ("audio/mpeg", "audio/mp3", "audio/x-mpeg"):
                return self._transcode(s, first)
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if first: self.wfile.write(first)
                s.settimeout(20)
                while True:
                    d = s.recv(16384)
                    if not d: break
                    self.wfile.write(d)
            except Exception:
                pass
            finally:
                try: s.close()
                except Exception: pass
        else:
            self._json({"ok": False, "err": "not found"}, 404)

    def _transcode(self, s, first, src=None):
        """AAC/AAC+/OGG station (piped in) or HLS station (src= URL, ffmpeg reads it) -> MP3 320k for the Denon"""
        inp = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-i", src] if src else ["-i", "pipe:0"]
        try:
            proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error"] + inp + ["-vn",
                                     "-c:a", "libmp3lame", "-b:a", "320k", "-ar", "44100", "-ac", "2",
                                     "-id3v2_version", "0", "-write_xing", "0", "-flush_packets", "1",
                                     "-f", "mp3", "pipe:1"],
                                    stdin=subprocess.DEVNULL if src else subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            if s: s.close()
            return self._send(502, "nema ffmpeg-a", "text/plain")
        def feed():
            try:
                if first: proc.stdin.write(first)
                s.settimeout(20)
                while proc.poll() is None:
                    d = s.recv(16384)
                    if not d: break
                    proc.stdin.write(d)
            except Exception: pass
            finally:
                try: proc.stdin.close()
                except Exception: pass
        if not src: threading.Thread(target=feed, daemon=True).start()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                d = proc.stdout.read1(16384)
                if not d: break
                self.wfile.write(d)
        except Exception:
            pass
        finally:
            try: proc.kill()
            except Exception: pass
            try:
                if s: s.close()
            except Exception: pass
            proc.wait()

    def do_HEAD(self):
        p = urlparse(self.path).path
        if p.startswith("/music/"): self._file(p, head=True)
        else: self._send(404, "", "text/plain")

    def _file(self, p, head=False):
        """music file for the Denon / the page, with Range (seek) support"""
        from urllib.parse import unquote
        path = music_path(unquote(p[len("/music/"):]))
        if not path or not os.path.isfile(path) or not _is_audio(os.path.basename(path)):
            return self._send(404, "", "text/plain")
        _served[path] = time.time()
        size = os.path.getsize(path); start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1); code = 206
            if start > end:
                self.send_response(416); self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(code)
        self.send_header("Content-Type", AUDIO_EXT.get(os.path.splitext(path)[1].lower(), "audio/mpeg"))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206: self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.end_headers()
        if head: return
        try:
            with open(path, "rb") as f:
                f.seek(start); left = end - start + 1
                while left > 0:
                    d = f.read(min(65536, left))
                    if not d: break
                    self.wfile.write(d); left -= len(d)
            # the whole rest of the current song went to the Denon (not just a tiny tag probe)
            if left == 0 and end == size - 1 and end - start > 65536 and path == _mus.get("path"):
                _mus["eof_t"] = time.time()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass

if __name__ == "__main__":
    load_stations()
    print("=" * 58)
    print(" DENON STUDIO")
    print(" Denon:      %s:%d" % (DENON_IP, DENON_PORT))
    print(" Otvori:     http://localhost:%d" % LISTEN_PORT)
    print(" Telefon:    http://<IP-računara>:%d" % LISTEN_PORT)
    print(" Stanica:    %d (YTuner)" % len(_stations))
    print(" Zaustavi:   Ctrl+C")
    print("=" * 58)
    threading.Thread(target=reader_loop, daemon=True).start()
    threading.Thread(target=music_loop, daemon=True).start()
    time.sleep(1.0)
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), H)
    srv.daemon_threads = True
    srv.serve_forever()
