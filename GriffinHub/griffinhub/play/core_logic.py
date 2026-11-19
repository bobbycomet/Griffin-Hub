#!/usr/bin/env python3
"""Griffin Play - one-click Linux game fixes driven by a community JSON database."""
import hashlib, json, os, platform, re, shlex, shutil, subprocess, tarfile, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
import urllib.request as ur
from pathlib import Path

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent
HOME = Path.home()
CACHE = HOME / ".cache/griffin-play"
_OLD_CACHE = HOME / ".cache/protonfix"  # legacy ProtonFix cache
PREF = CACHE / "prefs.json"

# Live database (override with GRIFFIN_PLAY_DB or legacy PROTONFIX_DB)
DB_URL = os.environ.get(
    "GRIFFIN_PLAY_DB",
    os.environ.get(
        "PROTONFIX_DB",
        "https://raw.githubusercontent.com/bobbycomet/ProtonFix/main/games/games.json",
    ),
)
REPO = "bobbycomet/ProtonFix"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
ISSUES_NEW = f"https://github.com/{REPO}/issues/new"
GRIFFIN_UPDATER_URL = "https://github.com/bobbycomet/GriffinUpdater"
UA = {"User-Agent": f"griffin-play/{__version__}"}

FAM = {"ubuntu": "ubuntu", "debian": "debian", "arch": "arch", "fedora": "fedora", "rhel": "fedora",
       "suse": "opensuse", "opensuse": "opensuse", "steamos": "steamos"}
PM = {"ubuntu": ["apt-get", "install", "-y"], "debian": ["apt-get", "install", "-y"],
      "arch": ["pacman", "-S", "--needed", "--noconfirm"], "fedora": ["dnf", "install", "-y"],
      "opensuse": ["zypper", "--non-interactive", "install"],
      "suse": ["zypper", "--non-interactive", "install"]}
VALVE = {"experimental": 1493710, "hotfix": 2180100}  # Steam app IDs of Valve's Proton tools

# ---------- prefs ----------
HISTORY = CACHE / "history.json"  # durable last-known-good launch opts per appid

def _ensure_cache():
    """Create cache dir; copy legacy ProtonFix cache files once if present."""
    CACHE.mkdir(parents=True, exist_ok=True)
    if _OLD_CACHE.is_dir():
        for name in ("prefs.json", "history.json", "games.json", "etag", "protondb.json"):
            src, dst = _OLD_CACHE / name, CACHE / name
            if src.exists() and not dst.exists():
                try:
                    dst.write_bytes(src.read_bytes())
                except OSError:
                    pass

def prefs_load():
    _ensure_cache()
    try:
        return json.loads(PREF.read_text())
    except Exception:
        return {}

def prefs_save(d):
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        PREF.write_text(json.dumps(d, indent=2))
    except OSError:
        pass

def history_load():
    _ensure_cache()
    try:
        return json.loads(HISTORY.read_text())
    except Exception:
        return {}

def history_save(d):
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps(d, indent=2))
    except OSError:
        pass

def history_record(appid, launch, tool=None, source="apply"):
    h = history_load()
    h[str(appid)] = {
        "launch": launch,
        "tool": tool,
        "source": source,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    history_save(h)

# ---------- VDF (Steam config format) ----------
def vparse(text):
    """Parse a subset of Steam VDF. Raises ValueError with context on failure."""
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    pos = 0

    def block(depth=0):
        nonlocal pos
        d = {}
        while pos < len(toks):
            s, b = toks[pos]
            if b == "}":
                pos += 1
                return d
            if b == "{":
                raise ValueError(f"unexpected '{{' at token {pos} (depth {depth})")
            pos += 1
            if pos >= len(toks):
                raise ValueError(f"truncated VDF after key {s!r}")
            s2, b2 = toks[pos]
            pos += 1
            if b2 == "{":
                d[s] = block(depth + 1)
            elif b2 == "}":
                raise ValueError(f"unexpected '}}' after key {s!r}")
            else:
                d[s] = s2
        if depth:
            raise ValueError(f"unclosed VDF block at depth {depth}")
        return d

    try:
        return block()
    except (ValueError, IndexError) as e:
        raise ValueError(f"VDF parse failed: {e}") from e

def vdump(d, i=0):
    t, out = "\t" * i, ""
    for k, v in d.items():
        if isinstance(v, dict):
            out += f'{t}"{k}"\n{t}{{\n{vdump(v, i + 1)}{t}}}\n'
        else:
            out += f'{t}"{k}"\t\t"{v}"\n'
    return out

def vsub(d, *path):  # case-insensitive get-or-create
    for p in path:
        d = d.setdefault(next((x for x in d if x.lower() == p.lower()), p), {})
    return d

# ---------- Database ----------
def load_db():
    _ensure_cache()
    cf, ef = CACHE / "games.json", CACHE / "etag"
    try:
        rq = ur.Request(DB_URL, headers=UA)
        if cf.exists() and ef.exists(): rq.add_header("If-None-Match", ef.read_text())
        with ur.urlopen(rq, timeout=4) as r:
            cf.write_bytes(r.read()); ef.write_text(r.headers.get("ETag", ""))
    except Exception:
        pass  # offline / 304 / bad URL -> cache, then bundled copy
    for p in (cf, HERE / "games.json"):
        try: return json.loads(p.read_text())
        except Exception: continue
    return {"games": [], "categories": [], "distro_profiles": {}}

# ---------- ProtonDB (unofficial public API, cached 24h) ----------
PDB_URL = "https://www.protondb.com/api/v1/reports/summaries/{}.json"
PDB_COUNTS = "https://www.protondb.com/data/counts.json"
PDB_CACHE = CACHE / "protondb.json"
PDB_REP_CACHE = CACHE / "pdb_reports"

def pdb_load():
    try:
        return json.loads(PDB_CACHE.read_text())
    except Exception:
        return {}

def pdb_fetch(appids, cache):
    """Refresh stale summary entries; returns True if any request was made."""
    now = time.time()
    todo = [a for a in appids if now - cache.get(a, {}).get("t", 0) > 86400]

    def one(a):
        try:
            with ur.urlopen(ur.Request(PDB_URL.format(a), headers=UA), timeout=8) as r:
                d = json.load(r)
            return a, {"tier": d.get("tier"), "best": d.get("bestReportedTier"),
                       "trending": d.get("trendingTier"), "score": d.get("score"),
                       "total": d.get("total"), "t": now}
        except Exception as e:
            code = getattr(e, "code", 0)
            if code == 404:
                return a, {"tier": "pending", "t": now}
            return a, None

    with ThreadPoolExecutor(6) as ex:
        for a, v in ex.map(one, todo):
            if v:
                cache[a] = v
    if todo:
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            PDB_CACHE.write_text(json.dumps(cache))
        except OSError:
            pass
    return bool(todo)

# ProtonDB obfuscated report URL hash (from their frontend JS)
def _pdb_I(e):
    a = 0
    for ch in str(e) + "m":
        a = ((a << 5) - a + ord(ch)) & 0xFFFFFFFF
        if a >= 0x80000000:
            a -= 0x100000000
    return abs(a)

def _pdb_R(e, t, n):
    return f"{t}p{int(e) * (int(t) % int(n))}"

def _pdb_hash(appid, reports, timestamp, page=1):
    # JS concatenates undefined -> the literal string "undefined"
    return _pdb_I("p" + _pdb_R(appid, reports, timestamp) + "*vRT"
                  + _pdb_R(page, appid, timestamp) + "undefined")

def pdb_counts():
    try:
        with ur.urlopen(ur.Request(PDB_COUNTS, headers=UA), timeout=6) as r:
            return json.load(r)
    except Exception:
        return None

def pdb_fetch_reports(appid, device="pc", page=1):
    """Fetch one page of community reports for an app. Returns list or []."""
    PDB_REP_CACHE.mkdir(parents=True, exist_ok=True)
    cpath = PDB_REP_CACHE / f"{appid}_{device}_{page}.json"
    try:
        if cpath.exists() and time.time() - cpath.stat().st_mtime < 86400:
            return json.loads(cpath.read_text()).get("reports", [])
    except Exception:
        pass
    counts = pdb_counts()
    if not counts:
        return []
    h = _pdb_hash(int(appid), counts["reports"], counts["timestamp"], page)
    url = f"https://www.protondb.com/data/reports/{device}/app/{h}.json"
    try:
        with ur.urlopen(ur.Request(url, headers={**UA, "User-Agent": "Mozilla/5.0 griffin-play"}), timeout=12) as r:
            d = json.load(r)
        cpath.write_text(json.dumps(d))
        return d.get("reports", [])
    except Exception:
        return []

def detect_gpu():
    """Best-effort GPU string for similarity matching (full lspci product text)."""
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            if "VGA" in line or "3D" in line or "Display" in line:
                return line.split(":", 2)[-1].strip()
    except Exception:
        pass
    return ""

def gpu_display_name(full):
    """Readable GPU model for the UI (prefer marketing name inside [...]).

    'NVIDIA Corporation TU106 [GeForce RTX 2070] (rev a1)' -> 'GeForce RTX 2070'
    'Advanced Micro Devices, Inc. [AMD/ATI] Navi 22 [Radeon RX 6700 XT]' -> 'Radeon RX 6700 XT'
    """
    s = (full or "").strip()
    if not s:
        return ""
    brackets = re.findall(r"\[([^\]]+)\]", s)
    if brackets:
        for b in reversed(brackets):
            bl = b.lower().strip()
            if bl in ("amd/ati", "amd", "intel", "nvidia", "ati"):
                continue
            return b.strip()
        return brackets[-1].strip()
    name = re.sub(
        r"^(NVIDIA Corporation|Advanced Micro Devices,\s*Inc\.|Intel Corporation|"
        r"Intel Corp\.|AMD|ATI)\s*",
        "",
        s,
        flags=re.I,
    )
    name = re.sub(r"\s*\(rev\s*[^)]+\)\s*$", "", name, flags=re.I).strip()
    return name or s

def _gpu_vendor(s):
    s = (s or "").lower()
    if any(x in s for x in ("nvidia", "geforce", "rtx", "gtx")):
        return "nvidia"
    if any(x in s for x in ("amd", "radeon", "rx ")):
        return "amd"
    if any(x in s for x in ("intel", "arc ", "uhd", "iris")):
        return "intel"
    return "other"

def detect_display():
    """Detect session type (wayland/x11), compositor, and desktop environment.

    Uses XDG_* env vars plus light process/socket checks so ranking can prefer
    reports from similar display setups (e.g. Wayland+KWin vs X11+Xfwm).
    """
    session = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("XDG_SESSION_DESKTOP")
               or os.environ.get("DESKTOP_SESSION") or "").strip()
    # Normalize multi-value desktops (e.g. "ubuntu:GNOME")
    desktop_main = desktop.split(":")[0].strip() if desktop else ""

    wayland_display = os.environ.get("WAYLAND_DISPLAY") or ""
    x11_display = os.environ.get("DISPLAY") or ""

    if not session:
        if wayland_display or os.environ.get("WAYLAND_SOCKET"):
            session = "wayland"
        elif x11_display:
            session = "x11"
        else:
            session = "unknown"

    # Compositor / WM detection (env first, then common process names)
    compositor = (os.environ.get("XDG_SESSION_COMPOSITOR")
                  or os.environ.get("DESKTOP_SESSION")
                  or "").strip().lower()
    # Explicit compositor env used by some WMs
    for key in ("SWAYSOCK", "HYPRLAND_INSTANCE_SIGNATURE", "WLR_BACKENDS"):
        if os.environ.get(key):
            if "sway" in key.lower() or os.environ.get("SWAYSOCK"):
                compositor = compositor or "sway"
            if "HYPRLAND" in key:
                compositor = "hyprland"

    # Map desktop names to known compositors when still vague
    dl = (desktop or "").lower()
    if not compositor or compositor in (dl, desktop_main.lower()):
        if "sway" in dl:
            compositor = "sway"
        elif "hyprland" in dl:
            compositor = "hyprland"
        elif "kde" in dl or "plasma" in dl:
            compositor = "kwin"
        elif "gnome" in dl:
            compositor = "mutter"
        elif "xfce" in dl:
            compositor = "xfwm4"
        elif "cinnamon" in dl:
            compositor = "muffin"
        elif "mate" in dl:
            compositor = "marco"
        elif "budgie" in dl:
            compositor = "budgie-wm"
        elif "lxqt" in dl:
            compositor = "kwin" if session == "wayland" else "openbox"
        elif "i3" in dl:
            compositor = "i3"
        elif "river" in dl:
            compositor = "river"
        elif "wayfire" in dl:
            compositor = "wayfire"

    # Process sniff as last resort (short timeout)
    if not compositor or compositor in ("", "unknown"):
        try:
            ps = subprocess.run(["ps", "-eo", "comm="], capture_output=True, text=True, timeout=2).stdout.lower()
            for name in ("sway", "hyprland", "kwin_wayland", "kwin_x11", "kwin", "mutter",
                         "gnome-shell", "xfwm4", "muffin", "marco", "wayfire", "river", "i3", "openbox"):
                if name in ps.split():
                    compositor = name.replace("kwin_wayland", "kwin").replace("kwin_x11", "kwin")
                    if compositor == "gnome-shell":
                        compositor = "mutter"
                    break
        except Exception:
            pass

    compositor = (compositor or "unknown").lower()
    # Session family for ranking: wayland / x11 / mir / other
    if session in ("wayland", "x11"):
        session_family = session
    elif "wayland" in session:
        session_family = "wayland"
    elif session in ("tty", "unspecified", ""):
        session_family = "unknown"
    else:
        session_family = session

    return dict(
        session=session or "unknown",
        session_family=session_family,
        compositor=compositor,
        desktop=desktop or desktop_main or "unknown",
        wayland=bool(wayland_display) or session_family == "wayland",
        x11=bool(x11_display) or session_family == "x11",
    )

def _report_display_hints(resp, hw):
    """Infer display stack hints from a ProtonDB report (WM field, notes, launch opts)."""
    wm = (hw.get("xWindowManager") or hw.get("windowManager") or "").lower()
    lo = (resp.get("launchOptions") or "").lower()
    notes = (resp.get("concludingNotes") or "").lower()
    if isinstance(resp.get("notes"), dict):
        notes += " " + " ".join(str(v).lower() for v in resp["notes"].values() if v)
    blob = f"{wm} {lo} {notes}"

    session = "unknown"
    if any(x in blob for x in ("wayland", "proton_enable_wayland", "sdl_vidiodriver=wayland",
                                "sdl_videodriver=wayland", "gamescope")):
        session = "wayland"
    elif any(x in blob for x in ("x11", "xorg", "sdl_videodriver=x11", "display=:")):
        session = "x11"
    elif wm:
        # Many WMs imply X11 unless known Wayland-native
        if any(x in wm for x in ("sway", "hyprland", "wayfire", "river", "kwin", "mutter")):
            # kwin/mutter can be either; leave unknown unless wayland markers exist
            if any(x in wm for x in ("sway", "hyprland", "wayfire", "river")):
                session = "wayland"
            else:
                session = "unknown"
        else:
            session = "x11"

    compositor = "unknown"
    for name in ("sway", "hyprland", "kwin", "mutter", "xfwm", "muffin", "marco",
                 "wayfire", "river", "i3", "openbox", "gamescope"):
        if name in wm or name in blob:
            compositor = "xfwm4" if name == "xfwm" else name
            break
    if compositor == "unknown" and wm:
        compositor = wm.split()[0][:32]

    return session, compositor, wm

def rank_similar_reports(reports, env, gpu_str, limit=5):
    """Score reports by how close they are to this machine; prefer ones with launchOptions."""
    fam = (env.get("family") or "").lower()
    my_vendor = _gpu_vendor(gpu_str)
    my_kernel = (env.get("kernel") or "").split("-")[0]
    my_session = (env.get("session_family") or env.get("session") or "").lower()
    my_comp = (env.get("compositor") or "").lower()
    scored = []
    for rep in reports:
        resp = rep.get("responses") or {}
        if resp.get("verdict") != "yes":
            continue
        hw = ((rep.get("device") or {}).get("inferred") or {}).get("steam") or {}
        os_s = (hw.get("os") or "").lower()
        gpu_s = hw.get("gpu") or ""
        kernel = (hw.get("kernel") or "").split("-")[0]
        rep_session, rep_comp, rep_wm = _report_display_hints(resp, hw)
        score = 0
        # distro / family
        if fam and fam in os_s:
            score += 4
        if any(x in os_s for x in ("arch", "cachy", "endeavouros", "manjaro")) and fam == "arch":
            score += 3
        if any(x in os_s for x in ("ubuntu", "mint", "pop")) and fam == "ubuntu":
            score += 3
        if "fedora" in os_s and fam == "fedora":
            score += 3
        if "steam" in os_s and fam == "steamos":
            score += 4
        # GPU vendor match is strong signal
        if my_vendor != "other" and _gpu_vendor(gpu_s) == my_vendor:
            score += 5
        # Display server + compositor (Wayland vs X11 matters for many launch opts)
        if my_session in ("wayland", "x11") and rep_session == my_session:
            score += 4
        elif my_session in ("wayland", "x11") and rep_session not in ("unknown", "") and rep_session != my_session:
            score -= 2  # penalize opposite display server
        if my_comp and my_comp not in ("unknown", "") and rep_comp not in ("unknown", ""):
            if my_comp in rep_comp or rep_comp in my_comp:
                score += 3
        # rough kernel major.minor
        if my_kernel and kernel and my_kernel[:3] == kernel[:3]:
            score += 1
        lo = (resp.get("launchOptions") or "").strip()
        if lo:
            score += 3  # prefer actionable reports
            # small bonus if launch opts align with our session
            lol = lo.lower()
            if my_session == "wayland" and any(x in lol for x in ("wayland", "gamescope")):
                score += 1
            if my_session == "x11" and "sdl_videodriver=x11" in lol:
                score += 1
        notes = resp.get("concludingNotes") or ""
        if isinstance(resp.get("notes"), dict):
            notes = notes or resp["notes"].get("verdict") or ""
        # Confidence label from score + key matches
        conf = "Low match"
        if score >= 12:
            conf = "High match"
        elif score >= 7:
            conf = "Medium match"
        bits = []
        if my_vendor != "other" and _gpu_vendor(gpu_s) == my_vendor:
            bits.append("same GPU family")
        if my_session in ("wayland", "x11") and rep_session == my_session:
            bits.append(my_session.title())
        if fam and fam in os_s:
            bits.append("similar distro")
        conf_detail = conf + ((" – " + " + ".join(bits)) if bits else "")
        # Prefer recent reports
        age_bonus = 0
        ts = rep.get("timestamp") or 0
        if ts:
            try:
                age_days = max(0, (time.time() - int(ts)) / 86400)
                if age_days < 90:
                    score += 2
                elif age_days < 365:
                    score += 1
            except Exception:
                pass
        scored.append({
            "score": score,
            "confidence": conf_detail,
            "os": hw.get("os") or "?",
            "gpu": gpu_s or "?",
            "kernel": hw.get("kernel") or "?",
            "session": rep_session,
            "compositor": rep_comp or rep_wm or "?",
            "proton": resp.get("customProtonVersion") or resp.get("protonVersion") or resp.get("variant") or "?",
            "variant": resp.get("variant") or "",
            "launchOptions": lo,
            "notes": (notes or "")[:240],
            "timestamp": rep.get("timestamp") or 0,
        })
    scored.sort(key=lambda x: (-x["score"], -x["timestamp"]))
    # de-dupe similar launch options
    seen, out = set(), []
    for s in scored:
        key = s["launchOptions"] or s["notes"][:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out

# ---------- Detection ----------
def detect():
    o = {}
    try:
        for l in open("/etc/os-release"):
            if "=" in l:
                k, v = l.strip().split("=", 1)
                o[k] = v.strip('"')
    except OSError:
        pass
    ids = [o.get("ID", "")] + o.get("ID_LIKE", "").split()
    fam = next((FAM[i] for i in ids if i in FAM), "unknown")
    gpu = detect_gpu()
    disp = detect_display()
    return dict(
        name=o.get("PRETTY_NAME", "Linux"), family=fam, kernel=platform.release(),
        immutable=fam == "steamos" or os.path.exists("/run/ostree-booted"),
        gpu=gpu, gpu_short=gpu_display_name(gpu), gpu_vendor=_gpu_vendor(gpu),
        **disp,
    )

ROOTS = [".local/share/Steam", ".steam/steam", ".steam/root",
         ".var/app/com.valvesoftware.Steam/data/Steam", "snap/steam/common/.local/share/Steam"]

def _safe_is_dir(path: Path) -> bool:
    """True if path is a directory; False on missing or I/O errors (bad mounts, NTFS, etc.)."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        try:
            return path.absolute()
        except OSError:
            return None


def steam_roots():
    seen, out = set(), []
    for r in ROOTS:
        p = HOME / r
        if not _safe_is_dir(p / "steamapps"):
            continue
        rp = _safe_resolve(p)
        if rp is None or rp in seen:
            continue
        seen.add(rp)
        out.append(rp)
    return out


def libraries(root):
    """Return Steam library roots. Skips paths that raise OSError (Errno 74 Bad message, etc.)."""
    if root is None:
        return []
    libs = [root]
    try:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        d = next(iter(vparse(vdf.read_text(errors="ignore")).values()))
        for v in d.values():
            if isinstance(v, dict) and "path" in v:
                libs.append(Path(v["path"]))
            elif isinstance(v, str) and v.startswith("/"):
                libs.append(Path(v))
    except Exception:
        pass
    out = []
    seen = set()
    for l in libs:
        try:
            if not _safe_is_dir(l / "steamapps"):
                continue
            key = str(_safe_resolve(l) or l)
            if key in seen:
                continue
            seen.add(key)
            out.append(l)
        except OSError as e:
            # e.g. [Errno 74] Bad message on flaky external/NTFS Steam libraries
            print(f"Griffin Play: skipping Steam library {l}: {e}", flush=True)
    return out


def installed_apps(libs):
    apps = {}
    for l in libs:
        steamapps = l / "steamapps"
        try:
            manifests = list(steamapps.glob("appmanifest_*.acf"))
        except OSError as e:
            print(f"Griffin Play: cannot list {steamapps}: {e}", flush=True)
            continue
        for f in manifests:
            try:
                a = next(iter(vparse(f.read_text(errors="ignore")).values()))
                apps[a["appid"]] = a.get("name", "")
            except Exception:
                pass
    return apps

def steam_running():
    return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0

# ---------- Polkit / privileged commands ----------
# Shared with Griffin Hub: prefer the unified helper installed by Hub.
# Fallback order:
#   1) /usr/local/bin/griffinhub-pkexec-helper  (Hub unified)
#   2) /usr/local/libexec/griffin-play-admin   (legacy Play-only)
#   3) plain pkexec / sudo
POLKIT_ACTION = "com.griffinhub.pkexec.run"
POLKIT_POLICY_ID = "com.griffinhub.pkexec.policy"
HUB_HELPER = Path("/usr/local/bin/griffinhub-pkexec-helper")
ADMIN_HELPER = Path("/usr/local/libexec/griffin-play-admin")  # legacy

def polkit_agent_running():
    """True if a common Polkit authentication agent appears to be running."""
    try:
        ps = subprocess.run(["ps", "-eo", "comm="], capture_output=True, text=True, timeout=2).stdout.lower()
    except Exception:
        return False
    agents = (
        "polkit-gnome-authentication-agent", "polkit-kde-authentication-agent",
        "polkit-mate-authentication-agent", "lxqt-policykit-agent",
        "xfce-polkit", "pantheon-agent-polkit", "policykit-1-gnome",
        "polkit-agent", "authentication-agent",
    )
    return any(a in ps for a in agents)

def polkit_status():
    """Human-readable Polkit / pkexec readiness (Hub + Play shared)."""
    has_pkexec = bool(shutil.which("pkexec"))
    has_policy = (
        Path("/usr/share/polkit-1/actions/com.griffinhub.pkexec.policy").exists()
        or Path("/usr/share/polkit-1/actions/com.griffin.play.policy").exists()
    )
    has_helper = (
        (HUB_HELPER.exists() and os.access(HUB_HELPER, os.X_OK))
        or (ADMIN_HELPER.exists() and os.access(ADMIN_HELPER, os.X_OK))
    )
    agent = polkit_agent_running()
    return dict(pkexec=has_pkexec, policy=has_policy, helper=has_helper, agent=agent)

def admin_cmd(shell_cmd=None, op=None, args=None):
    """Build an argv list that elevates via Polkit (pkexec).

    Shared with Griffin Hub. Prefer structured ops:
      admin_cmd(op="packages", args=[family, pkg1, pkg2, ...])
      admin_cmd(op="sysctl", args=[key, value])
    """
    args = list(args or [])
    # Resolve shell command for packages/sysctl if needed
    if not shell_cmd and op == "packages" and len(args) >= 2:
        fam, pkgs = args[0], args[1:]
        if fam in PM:
            shell_cmd = shlex.join(PM[fam] + pkgs)
    if not shell_cmd and op == "sysctl" and len(args) >= 2:
        k, v = args[0], args[1]
        shell_cmd = (
            f"sysctl -w {k}={v} && "
            f"echo '{k} = {v}' > /etc/sysctl.d/99-griffin-play-{k}.conf"
        )
    # Legacy Play helper supports structured ops
    if ADMIN_HELPER.exists() and os.access(ADMIN_HELPER, os.X_OK) and op:
        return [str(ADMIN_HELPER), op, *args]
    if ADMIN_HELPER.exists() and os.access(ADMIN_HELPER, os.X_OK) and shell_cmd:
        return [str(ADMIN_HELPER), "shell", shell_cmd]
    if not shell_cmd:
        return None
    pk = shutil.which("pkexec")
    if not pk:
        sudo = shutil.which("sudo")
        return [sudo, "sh", "-c", shell_cmd] if sudo else None
    # Unified Hub helper: pkexec helper -- sh -c '...'
    if HUB_HELPER.exists() and os.access(HUB_HELPER, os.X_OK):
        return [pk, str(HUB_HELPER), "sh", "-c", shell_cmd]
    return [pk, "--disable-internal-agent", "sh", "-c", shell_cmd]

# ---------- Actions ----------
def vkey(s): return tuple(int(x) for x in re.findall(r"\d+", s))
def ge_installed(root):
    if not root:
        return []
    return sorted((p.name for p in (root / "compatibilitytools.d").glob("GE-Proton*")), key=vkey)

def _ge_release(tag=None):
    """Fetch a GE-Proton GitHub release (latest or specific tag)."""
    if tag:
        url = f"https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/tags/{tag}"
    else:
        url = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
    with ur.urlopen(ur.Request(url, headers=UA), timeout=15) as r:
        return json.load(r)

def install_ge(root, log, dry, pin=None):
    """Install GE-Proton. pin=None -> latest; pin='GE-Proton9-20' installs that tag if missing.

    Reuses an already-installed copy when possible so we do not redownload every Apply.
    """
    have = ge_installed(root)
    if pin:
        # Accept short forms like "9-20" or full "GE-Proton9-20"
        candidates = [pin]
        if not pin.startswith("GE-Proton"):
            candidates.append(f"GE-Proton{pin}")
        for c in candidates:
            if c in have:
                log(f"Pinned {c} already installed — reusing")
                return c
        tag = candidates[-1] if candidates[-1].startswith("GE-Proton") else f"GE-Proton{pin}"
        log(f"Fetching pinned release {tag}…")
        try:
            rel = _ge_release(tag)
        except Exception as e:
            raise RuntimeError(f"Could not fetch pinned GE release {tag}: {e}") from e
    else:
        rel = _ge_release(None)
        tag = rel["tag_name"]
        if tag in have:
            log(f"Proton {tag} already installed — reusing (not re-downloading)")
            return tag
        # Prefer newest already installed if offline-ish preference: still install latest when missing
        if have:
            log(f"Latest is {tag}; local builds present: {', '.join(have[-3:])}")

    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    tarball = assets.get(f"{tag}.tar.gz")
    checksum = assets.get(f"{tag}.sha512sum")
    if not tarball:
        for name, url in assets.items():
            if name.endswith(".tar.gz") and tag.replace("GE-Proton", "").replace("-", "") in name.replace("-", ""):
                tarball = url
                break
        if not tarball:
            for name, url in assets.items():
                if name.endswith(".tar.gz"):
                    tarball = url
                    log(f"Using alternate asset name: {name}")
                    break
    if not checksum:
        for name, url in assets.items():
            if "sha512" in name.lower():
                checksum = url
                break
    if not tarball:
        raise RuntimeError(f"No tarball found in GE-Proton release {tag}")
    log(f"Installing {tag} into {root / 'compatibilitytools.d'}")
    if dry:
        return tag
    dest = root / "compatibilitytools.d"
    dest.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / f"{tag}.tar.gz"
        ur.urlretrieve(tarball, f)
        if checksum:
            want = ur.urlopen(ur.Request(checksum, headers=UA)).read().decode().split()[0]
            if hashlib.sha512(f.read_bytes()).hexdigest() != want:
                raise RuntimeError("Checksum mismatch, aborting")
        else:
            log("! No checksum asset found; skipping verify")
        with tarfile.open(f) as tf:
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                tf.extractall(dest)
    log(f"Installed {tag}")
    return tag

def write_steam(root, appid, launch, tool, log, force=False):
    if steam_running():
        raise RuntimeError("Steam is running. Close it and Apply again (or paste launch options manually).")
    wrote = False
    for lc in (root / "userdata").glob("[0-9]*/config/localconfig.vdf"):
        bak = str(lc) + ".griffin-play.bak"
        if not Path(bak).exists():
            shutil.copy(lc, bak)
        try:
            t = vparse(lc.read_text(errors="ignore"))
        except ValueError as e:
            if not force:
                log(f"! Skipping unreadable localconfig {lc}: {e}")
                log("  Enable 'Force rewrite Steam configs' to attempt a minimal write.")
                continue
            log(f"! localconfig parse failed ({e}); force-writing LaunchOptions only")
            t = {}
        vsub(t, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", appid)["LaunchOptions"] = launch.replace('"', '\\"')
        lc.write_text(vdump(t))
        log(f"Wrote launch options -> {lc}")
        wrote = True
    if not wrote:
        log("! No userdata localconfig.vdf found — launch Steam once, then Make it work again")
    cv = root / "config/config.vdf"
    if tool and cv.exists():
        bak = str(cv) + ".griffin-play.bak"
        if not Path(bak).exists():
            shutil.copy(cv, bak)
        try:
            t = vparse(cv.read_text(errors="ignore"))
        except ValueError as e:
            if not force:
                log(f"! Skipping unreadable config.vdf: {e}")
                return
            log(f"! config.vdf parse failed ({e}); force-writing CompatToolMapping only")
            t = {}
        vsub(t, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", appid).update(
            name=tool, config="", priority="250")
        cv.write_text(vdump(t))
        log(f"Forced compatibility tool '{tool}'")

def list_backups(root):
    """Return list of (path, kind) for Griffin Play backups under a Steam root."""
    out = []
    if not root:
        return out
    for pattern in ("*.vdf.griffin-play.bak", "*.vdf.protonfix.bak"):
        pass
    for lc in list((root / "userdata").glob("[0-9]*/config/localconfig.vdf.griffin-play.bak")) + \
             list((root / "userdata").glob("[0-9]*/config/localconfig.vdf.protonfix.bak")):
        out.append((lc, "localconfig"))
    for name in ("config.vdf.griffin-play.bak", "config.vdf.protonfix.bak"):
        cv = root / "config" / name
        if cv.exists():
            out.append((cv, "config"))
    return out

def restore_backups(root, log):
    if steam_running():
        raise RuntimeError("Steam is running. Close it before restoring backups.")
    restored = 0
    for bak, kind in list_backups(root):
        target = Path(str(bak).removesuffix(".griffin-play.bak").removesuffix(".protonfix.bak"))
        shutil.copy(bak, target)
        log(f"Restored {kind} <- {bak.name}")
        restored += 1
    if not restored:
        log("No Griffin Play backups found to restore.")
    return restored

def griffin_updater_installed():
    """Best-effort detection of Griffin Updater on PATH or common install locations."""
    if shutil.which("griffin-updater") or shutil.which("GriffinUpdater"):
        return True
    for p in (
        HOME / ".local/bin/griffin-updater",
        HOME / ".local/bin/GriffinUpdater",
        HOME / ".local/opt/griffin-updater",
        Path("/usr/bin/griffin-updater"),
        Path("/usr/local/bin/griffin-updater"),
    ):
        if p.exists():
            return True
    return False

def check_latest_release():
    """Return (tag, html_url) of latest GitHub release, or (None, None)."""
    try:
        with ur.urlopen(ur.Request(RELEASES_API, headers=UA), timeout=6) as r:
            d = json.load(r)
        tag = (d.get("tag_name") or "").lstrip("v")
        return tag or None, d.get("html_url") or RELEASES_PAGE
    except Exception:
        return None, None


# ---------- Status labels ----------
def is_native_linux(tier=None, proton=None):
    """True when the title runs as a native Linux binary (not via Proton/Wine).

    ProtonDB tier "native" means a native Linux build. Our JSON proton=="native"
    is the curated equivalent. Either signal is enough.
    """
    t = (tier or "").lower()
    return t == "native" or (proton or "").lower() == "native"


def status_label(tier, blocked=False, proton=None):
    """Human-readable status for the game list.

    Native Linux is always explicit (not conflated with Proton tiers).
    When a native Linux build exists, that wins the list label even if
    ProtonDB also has Windows/Proton ratings.
    """
    if blocked:
        return "Blocked"
    if is_native_linux(tier, proton):
        return "Native Linux"
    t = (tier or "unknown").lower()
    return {
        "platinum": "Platinum", "gold": "Gold", "silver": "Silver",
        "bronze": "Bronze", "borked": "Borked", "pending": "Pending",
        "unknown": "Unknown",
    }.get(t, t.title())


def merge_library(db, installed):
    """Build the in-memory game map: curated JSON + every installed Steam app.

    ProtonDB supplies live tiers online. JSON supplies launch options, blocked
    flags, packages, notes, and offline tier hints. Installed apps missing from
    JSON appear as lightweight entries so the tool can list the whole library.
    """
    defaults = dict(db.get("defaults") or {})
    games = {}
    for g in db.get("games") or []:
        a = str(g.get("appid", ""))
        if not a:
            continue
        games[a] = {**defaults, **g, "appid": int(g["appid"]) if str(g["appid"]).isdigit() else g["appid"]}
        games[a]["_source"] = "json"
    # Fold in installed Steam titles not already curated
    for appid, name in (installed or {}).items():
        a = str(appid)
        if a in games:
            # Prefer Steam's installed name only if JSON name empty
            if name and not games[a].get("name"):
                games[a]["name"] = name
            games[a]["_installed"] = True
            continue
        games[a] = {
            **defaults,
            "appid": int(appid) if str(appid).isdigit() else appid,
            "name": name or f"App {appid}",
            "categories": ["Installed"],
            "proton": defaults.get("proton", "ge"),
            "notes": "Not in curated DB — using ProtonDB live data + defaults.",
            "anticheat": "none",
            "_source": "steam",
            "_installed": True,
        }
    return games

