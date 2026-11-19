"""Steam shortcuts.vdf binary read/write and high-level API for Griffin Game Tweaks."""

from __future__ import annotations

import os
import shutil
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from griffin.config import normalize_path_for_steam
from griffin.logging_utils import log_error, log_install, log_info, log_warning
from griffin.steam.compat import remove_compat_tool, set_compat_tool
from griffin.steam.paths import find_steam_path, find_userdata_path, get_steam_accounts


def generate_random_app_id() -> int:
    return (int.from_bytes(os.urandom(4), "little") | 0x80000000) & 0xFFFFFFFF


def steam_shortcut_app_id(exe_path: str, app_name: str) -> int:
    payload = (exe_path + app_name).encode("utf-8", errors="replace")
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return (crc | 0x80000000) & 0xFFFFFFFF


def detect_extra_mounts() -> list[str]:
    try:
        from griffin.steam.paths import steam_compat_mounts
        return steam_compat_mounts()
    except Exception:
        mounts: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            path = path.rstrip("/") or path
            if not path or path in seen:
                return
            if Path(path).is_dir():
                seen.add(path)
                mounts.append(path)

        for extra in ("/mnt", "/media", "/run/media"):
            _add(extra)
            try:
                root = Path(extra)
                if root.is_dir():
                    for child in root.iterdir():
                        if child.is_dir() and not child.name.startswith("."):
                            _add(str(child))
            except OSError:
                pass
        mounts.sort()
        return mounts


def generate_launch_options(
    is_electron: bool = False,
    *,
    extra: str = "",
    proton_use_wined3d: bool = False,
    proton_no_esync: bool = False,
    proton_no_fsync: bool = False,
) -> str:
    """Build Steam launch options string.

    Order: env vars, then %command%, then electron/extra args after the command.
    """
    mounts = detect_extra_mounts()
    env_parts: list[str] = []
    if mounts:
        env_parts.append(f"STEAM_COMPAT_MOUNTS={':'.join(mounts)}")
        log_info(f"STEAM_COMPAT_MOUNTS ({len(mounts)} paths)")
    if proton_use_wined3d:
        env_parts.append("PROTON_USE_WINED3D=1")
    if proton_no_esync:
        env_parts.append("PROTON_NO_ESYNC=1")
    if proton_no_fsync:
        env_parts.append("PROTON_NO_FSYNC=1")

    after: list[str] = []
    if is_electron:
        after.extend(["--disable-gpu", "--no-sandbox", "--disable-gpu-sandbox"])
    if extra and extra.strip():
        after.append(extra.strip())

    body = " ".join(env_parts)
    if body:
        body = body + " %command%"
    else:
        body = "%command%"
    if after:
        body = body + " " + " ".join(after)
    return body.strip()


def is_steam_running() -> bool:
    try:
        import subprocess
        out = subprocess.check_output(["ps", "-eo", "comm="], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            name = line.strip().lower()
            if name in ("steam", "steamwebhelper", "steamservice"):
                return True
    except Exception:
        pass
    try:
        for pid in Path("/proc").iterdir():
            if not pid.name.isdigit():
                continue
            try:
                comm = (pid / "comm").read_text(encoding="utf-8", errors="replace").strip().lower()
            except OSError:
                continue
            if comm in ("steam", "steamwebhelper"):
                return True
    except OSError:
        pass
    return False


@dataclass
class Shortcut:
    appid: int = 0
    app_name: str = ""
    exe: str = ""
    start_dir: str = ""
    icon: str = ""
    shortcut_path: str = ""
    launch_options: str = ""
    is_hidden: bool = False
    allow_desktop_config: bool = True
    allow_overlay: bool = True
    openvr: bool = False
    devkit: bool = False
    devkit_game_id: str = ""
    devkit_override_app_id: int = 0
    last_play_time: int = 0
    flatpak_app_id: str = ""
    tags: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, app_name: str, exe_path: str, start_dir: str) -> "Shortcut":
        exe_quoted = exe_path if exe_path.startswith('"') else f'"{exe_path}"'
        dir_quoted = start_dir if start_dir.startswith('"') else f'"{start_dir}"'
        appid = steam_shortcut_app_id(exe_quoted, app_name)
        return cls(
            appid=appid,
            app_name=app_name,
            exe=exe_quoted,
            start_dir=dir_quoted,
        )

    def with_tag(self, tag: str) -> "Shortcut":
        if tag not in self.tags:
            self.tags.append(tag)
        return self

    def with_launch_options(self, options: str) -> "Shortcut":
        self.launch_options = options
        return self

    def with_icon(self, icon_path: str) -> "Shortcut":
        self.icon = icon_path
        return self


class ShortcutsVdf:
    def __init__(self, shortcuts: Optional[list[Shortcut]] = None):
        self.shortcuts: list[Shortcut] = shortcuts or []

    @staticmethod
    def path_for_userdata(userdata: Path) -> Path:
        return userdata / "config" / "shortcuts.vdf"

    @staticmethod
    def path() -> Optional[Path]:
        ud = find_userdata_path()
        if not ud:
            return None
        return ShortcutsVdf.path_for_userdata(ud)

    @classmethod
    def load(cls, path: Optional[Path] = None, *, strict: bool = False) -> "ShortcutsVdf":
        """Load shortcuts.vdf.

        With ``strict=True`` an unreadable file raises instead of returning an empty
        list — callers that go on to *write* must use this, otherwise a parse failure
        would silently wipe the user's other non-Steam shortcuts.
        """
        path = path or cls.path()
        if not path or not path.exists() or path.stat().st_size == 0:
            return cls()
        try:
            data = path.read_bytes()
            parsed = cls._parse(data)
            log_info(f"Loaded {len(parsed.shortcuts)} Steam shortcut(s) from {path}")
            return parsed
        except Exception as e:
            log_warning(f"Failed to parse shortcuts.vdf ({path}): {e}")
            try:
                bak = path.with_suffix(".vdf.griffin-broken")
                shutil.copy2(path, bak)
            except OSError:
                pass
            if strict:
                raise RuntimeError(
                    f"Could not read {path} ({e}). Nothing was changed; a copy was saved as "
                    f"{path.name}.griffin-broken. Fix or remove the file and try again."
                ) from e
            return cls()

    @classmethod
    def _parse(cls, data: bytes) -> "ShortcutsVdf":
        shortcuts: list[Shortcut] = []
        pos = 0
        n = len(data)

        def read_cstring(i: int) -> tuple[str, int]:
            start = i
            while i < n and data[i] != 0:
                i += 1
            s = data[start:i].decode("utf-8", errors="replace")
            return s, i + 1

        def read_int(i: int) -> tuple[int, int]:
            if i + 4 > n:
                return 0, i
            return struct.unpack_from("<I", data, i)[0], i + 4

        if pos < n and data[pos] == 0x00:
            pos += 1
            _, pos = read_cstring(pos)

        while pos < n:
            if data[pos] == 0x08:
                break
            if data[pos] != 0x00:
                pos += 1
                continue
            pos += 1
            _, pos = read_cstring(pos)
            sc = Shortcut()
            while pos < n and data[pos] != 0x08:
                vtype = data[pos]
                pos += 1
                if vtype == 0x00:
                    key, pos = read_cstring(pos)
                    if key.lower() == "tags":
                        while pos < n and data[pos] != 0x08:
                            if data[pos] == 0x01:
                                pos += 1
                                _, pos = read_cstring(pos)
                                tag, pos = read_cstring(pos)
                                if tag:
                                    sc.tags.append(tag)
                            elif data[pos] == 0x08:
                                break
                            else:
                                pos += 1
                        if pos < n and data[pos] == 0x08:
                            pos += 1
                    else:
                        depth = 1
                        while pos < n and depth:
                            if data[pos] == 0x00:
                                depth += 1
                                pos += 1
                            elif data[pos] == 0x08:
                                depth -= 1
                                pos += 1
                            elif data[pos] == 0x01:
                                pos += 1
                                _, pos = read_cstring(pos)
                                _, pos = read_cstring(pos)
                            elif data[pos] == 0x02:
                                pos += 1
                                _, pos = read_cstring(pos)
                                pos += 4
                            else:
                                pos += 1
                elif vtype == 0x01:
                    key, pos = read_cstring(pos)
                    val, pos = read_cstring(pos)
                    kl = key.lower()
                    if kl == "appname":
                        sc.app_name = val
                    elif kl == "exe":
                        sc.exe = val
                    elif kl == "startdir":
                        sc.start_dir = val
                    elif kl == "icon":
                        sc.icon = val
                    elif kl == "shortcutpath":
                        sc.shortcut_path = val
                    elif kl == "launchoptions":
                        sc.launch_options = val
                    elif kl == "devkitgameid":
                        sc.devkit_game_id = val
                    elif kl == "flatpakappid":
                        sc.flatpak_app_id = val
                elif vtype == 0x02:
                    key, pos = read_cstring(pos)
                    val, pos = read_int(pos)
                    kl = key.lower()
                    if kl == "appid":
                        sc.appid = val
                    elif kl == "ishidden":
                        sc.is_hidden = bool(val)
                    elif kl == "allowdesktopconfig":
                        sc.allow_desktop_config = bool(val)
                    elif kl == "allowoverlay":
                        sc.allow_overlay = bool(val)
                    elif kl == "openvr":
                        sc.openvr = bool(val)
                    elif kl == "devkit":
                        sc.devkit = bool(val)
                    elif kl == "devkitoverrideappid":
                        sc.devkit_override_app_id = val
                    elif kl == "lastplaytime":
                        sc.last_play_time = val
                else:
                    break
            if pos < n and data[pos] == 0x08:
                pos += 1
            if sc.app_name or sc.exe:
                if not sc.appid:
                    sc.appid = steam_shortcut_app_id(sc.exe or "", sc.app_name or "")
                shortcuts.append(sc)
        return cls(shortcuts)

    def write(self, path: Path) -> None:
        out = bytearray()
        out.append(0x00)
        out.extend(b"shortcuts\x00")

        def w_str(key: str, value: str) -> None:
            out.append(0x01)
            out.extend(key.encode("utf-8") + b"\x00")
            out.extend((value or "").encode("utf-8") + b"\x00")

        def w_int(key: str, value: int) -> None:
            out.append(0x02)
            out.extend(key.encode("utf-8") + b"\x00")
            out.extend(struct.pack("<I", int(value) & 0xFFFFFFFF))

        for idx, sc in enumerate(self.shortcuts):
            out.append(0x00)
            out.extend(str(idx).encode("utf-8") + b"\x00")
            w_int("appid", sc.appid)
            w_str("AppName", sc.app_name)
            w_str("Exe", sc.exe)
            w_str("StartDir", sc.start_dir)
            w_str("icon", sc.icon)
            w_str("ShortcutPath", sc.shortcut_path)
            w_str("LaunchOptions", sc.launch_options)
            w_int("IsHidden", int(sc.is_hidden))
            w_int("AllowDesktopConfig", int(sc.allow_desktop_config))
            w_int("AllowOverlay", int(sc.allow_overlay))
            w_int("OpenVR", int(sc.openvr))
            w_int("Devkit", int(sc.devkit))
            w_str("DevkitGameID", sc.devkit_game_id)
            w_int("DevkitOverrideAppID", sc.devkit_override_app_id)
            w_int("LastPlayTime", sc.last_play_time)
            w_str("FlatpakAppID", sc.flatpak_app_id)
            out.append(0x00)
            out.extend(b"tags\x00")
            for ti, tag in enumerate(sc.tags):
                w_str(str(ti), tag)
            out.append(0x08)
            out.append(0x08)
        out.append(0x08)
        out.append(0x08)

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".griffin.tmp.{os.getpid()}")
        tmp.write_bytes(bytes(out))
        if path.exists():
            try:
                orig = path.with_suffix(".vdf.griffin-orig")
                if not orig.exists():
                    shutil.copy2(path, orig)  # pristine copy from before Griffin touched it
                shutil.copy2(path, path.with_suffix(".vdf.griffin.bak"))
            except OSError:
                pass
        os.replace(tmp, path)
        try:
            os.utime(path, None)
        except OSError:
            pass

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or self.path()
        if not path:
            raise RuntimeError(
                "Could not find Steam userdata path. "
                "Open Steam once, log in, then set the account in Settings if needed."
            )
        self.write(path)
        verify = ShortcutsVdf.load(path)
        names = {s.app_name for s in verify.shortcuts}
        ours = {s.app_name for s in self.shortcuts}
        missing = ours - names
        if missing:
            raise RuntimeError(
                f"Wrote shortcuts.vdf but verification failed for: {', '.join(missing)} ({path})"
            )
        log_install(f"Saved {len(self.shortcuts)} shortcut(s) → {path}")
        return path

    def remove_shortcut_by_app_id(self, app_id: int) -> bool:
        before = len(self.shortcuts)
        self.shortcuts = [s for s in self.shortcuts if s.appid != app_id]
        return len(self.shortcuts) < before

    def add_shortcut(self, shortcut: Shortcut) -> int:
        self.shortcuts = [
            s for s in self.shortcuts
            if s.app_name != shortcut.app_name and s.appid != shortcut.appid
        ]
        existing = {s.appid for s in self.shortcuts}
        while shortcut.appid in existing:
            shortcut.appid = generate_random_app_id()
        self.shortcuts.append(shortcut)
        return shortcut.appid


@dataclass
class SteamShortcutResult:
    app_id: int
    prefix_path: Path
    shortcuts_vdf: str = ""
    userdata: str = ""


def remove_steam_shortcut(app_id: int) -> None:
    vdf = ShortcutsVdf.load(strict=True)
    if vdf.remove_shortcut_by_app_id(app_id):
        vdf.save()
        log_install(f"Removed Steam shortcut for AppID {app_id}")
    try:
        if remove_compat_tool(app_id):
            log_install(f"Removed compat tool mapping for AppID {app_id}")
    except Exception as e:  # best effort
        log_warning(f"Could not remove compat tool mapping: {e}")


def _all_userdata_dirs() -> list[Path]:
    steam = find_steam_path()
    if not steam:
        return []
    ud_root = steam / "userdata"
    if not ud_root.is_dir():
        return []
    dirs = []
    for entry in ud_root.iterdir():
        if entry.is_dir() and entry.name.isdigit() and entry.name != "0":
            dirs.append(entry)
    return dirs


def add_game_shortcut(
    name: str,
    exe_path: str,
    start_dir: str,
    proton_name: str,
    is_electron: bool = False,
    icon_path: Optional[str] = None,
    extra_launch_options: str = "",
    proton_use_wined3d: bool = False,
    proton_no_esync: bool = False,
    proton_no_fsync: bool = False,
) -> SteamShortcutResult:
    """Create/update a non-Steam shortcut for a Windows game launcher and set Proton."""
    if is_steam_running():
        log_warning(
            "Steam appears to be running. Steam keeps shortcuts.vdf in memory and will "
            "overwrite disk changes on exit. Fully exit Steam (Steam → Exit) before installing, "
            "or restart Steam twice after install if the shortcut is missing."
        )

    exe_path = normalize_path_for_steam(exe_path)
    start_dir = normalize_path_for_steam(start_dir)

    primary_ud = find_userdata_path()
    if not primary_ud:
        candidates = _all_userdata_dirs()
        if candidates:
            primary_ud = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
            log_warning(f"No preferred account; using userdata {primary_ud}")
        else:
            raise RuntimeError(
                "Could not find Steam userdata. Launch Steam, log in once, then retry."
            )

    path = ShortcutsVdf.path_for_userdata(primary_ud)
    log_info(f"Using Steam userdata: {primary_ud}")
    log_info(f"shortcuts.vdf → {path}")

    vdf = ShortcutsVdf.load(path, strict=True)
    launch = generate_launch_options(
        is_electron,
        extra=extra_launch_options or "",
        proton_use_wined3d=proton_use_wined3d,
        proton_no_esync=proton_no_esync,
        proton_no_fsync=proton_no_fsync,
    )
    if launch and launch != "%command%":
        log_install(f"Launch options: {launch}")

    sc = Shortcut.new(name, exe_path, start_dir).with_tag("Griffin").with_launch_options(launch)
    if icon_path:
        sc.with_icon(icon_path)

    app_id = vdf.add_shortcut(sc)
    saved = vdf.save(path)
    log_install(f"Non-Steam shortcut '{name}' AppID={app_id}")

    for acc in get_steam_accounts():
        other = Path(find_steam_path() or "") / "userdata" / acc.account_id  # type: ignore
        if not other.is_dir() or other == primary_ud:
            continue
        if not acc.most_recent:
            continue
        try:
            other_path = ShortcutsVdf.path_for_userdata(other)
            other_vdf = ShortcutsVdf.load(other_path, strict=True)
            other_vdf.add_shortcut(sc)
            other_vdf.save(other_path)
            log_info(f"Also wrote shortcut for most-recent account {acc.account_id}")
        except Exception as e:
            log_warning(f"Could not write shortcut for account {acc.account_id}: {e}")

    try:
        set_compat_tool(app_id, proton_name)
        log_install(f"Compat tool for {app_id} → {proton_name}")
    except Exception as e:
        log_error(f"Failed to set Proton in config.vdf: {e}")
        raise RuntimeError(
            f"Shortcut was written to {saved}, but setting Proton failed: {e}"
        ) from e

    primary = find_steam_path()
    if not primary:
        raise RuntimeError("Could not find Steam installation")
    compat = primary / "steamapps" / "compatdata" / str(app_id)
    prefix = compat / "pfx"
    compat.mkdir(parents=True, exist_ok=True)
    (compat / "griffin_shortcut_meta.txt").write_text(
        f"name={name}\nexe={exe_path}\nappid={app_id}\nshortcuts={saved}\n",
        encoding="utf-8",
    )

    return SteamShortcutResult(
        app_id=app_id,
        prefix_path=prefix,
        shortcuts_vdf=str(saved),
        userdata=str(primary_ud),
    )
