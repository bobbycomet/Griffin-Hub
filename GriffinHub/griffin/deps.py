"""Winetricks / cabextract dependency management for Griffin Game Tweaks."""

from __future__ import annotations

import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

from griffin.config import AppConfig
from griffin.logging_utils import log_debug, log_info, log_install, log_warning
from griffin.steam.proton import SteamProton
from griffin.utils import clean_env, download_file, run_streaming, sha256_file

# --------------------------------------------------------------------------- #
# Runtime sets ("profiles")
# --------------------------------------------------------------------------- #
# vcrun2022 covers the whole 2015–2022 VC++ family; the older vcrun verbs are
# redundant under Proton and mostly fail harmlessly, so they are *optional*.
# dotnetdesktop* already includes the base .NET runtime, so dotnet6/7/8 are not
# installed separately any more.

MINIMAL_VERBS = [
    "vcrun2022",
    "d3dcompiler_47",
    "corefonts",
]

STANDARD_VERBS = MINIMAL_VERBS + [
    "d3dcompiler_43",
    "d3dx11_43",
    "xact",
    "xact_x64",
    "msxml6",
    "dotnetdesktop6",
    "dotnetdesktop8",
]

EXTENDED_VERBS = STANDARD_VERBS + [
    "dotnet40",
    "arial",
    "tahoma",
    "msxml3",
]

PROFILES: dict[str, list[str]] = {
    "minimal": MINIMAL_VERBS,
    "standard": STANDARD_VERBS,
    "extended": EXTENDED_VERBS,
}

PROFILE_DESCRIPTIONS: dict[str, str] = {
    "minimal": "Fastest. VC++ 2015–2022, D3D compiler 47, core fonts. Good first try for Electron/CEF launchers.",
    "standard": "Recommended. Minimal + older D3D/XAudio pieces, msxml6, .NET Desktop 6 + 8.",
    "extended": "Slowest. Standard + .NET 4.0, extra fonts, msxml3. Use for old or picky launchers.",
}

# Optional — user enables under Advanced.
# winhttp / wininet install *native* Microsoft DLLs, which often breaks launchers
# under Wine; only enable them if a specific launcher asks for them.
OPTIONAL_VERBS = [
    "vcrun2019",
    "vcrun2017",
    "vcrun2015",
    "vcrun2013",
    "vcrun2012",
    "vcrun2010",
    "vcrun2008",
    "vcrun2005",
    "d3dx9",
    "dotnet472",
    "dotnet48",
    "dotnet35",
    "dotnet6",
    "dotnet8",
    "d3dcompiler_42",
    "dxvk",
    "vkd3d",
    "faudio",
    "physx",
    "gdiplus",
    "riched20",
    "quartz",
    "wmp11",
    "winhttp",
    "wininet",
]

# Older VC++/d3dx9 verbs that commonly fail harmlessly once vcrun2022 is present.
SOFT_FAIL_VERBS = {"vcrun2019", "vcrun2017", "vcrun2015", "vcrun2013", "d3dx9"}

# Key files used for post-install verification (relative to prefix)
VERIFY_PATHS = [
    "drive_c/windows/system32/msvcp140.dll",
    "drive_c/windows/system32/vcruntime140.dll",
    "drive_c/windows/system32/d3dcompiler_47.dll",
    "drive_c/windows/syswow64/msvcp140.dll",
]


def verbs_for_profile(profile: str, extra_verbs: Optional[list[str]] = None) -> list[str]:
    verbs = list(PROFILES.get((profile or "standard").lower(), STANDARD_VERBS))
    for v in extra_verbs or []:
        if v not in verbs:
            verbs.append(v)
    return verbs


# --------------------------------------------------------------------------- #
# Pinned, checksum-verified downloads
# --------------------------------------------------------------------------- #
WINETRICKS_VERSION = "20260125"
WINETRICKS_URL = (
    f"https://raw.githubusercontent.com/Winetricks/winetricks/{WINETRICKS_VERSION}/src/winetricks"
)
WINETRICKS_SHA256 = "431f82fc74000e6c864409f1d8fb495d696c03928808e3e8acffc45179312a7b"
WINETRICKS_LATEST_URL = "https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"

CABEXTRACT_URL = "https://github.com/SulfurNitride/NaK/releases/download/Cabextract/cabextract-linux-x86_64.zip"
CABEXTRACT_ZIP_SHA256 = "cd67080dcf5b3116d29a246a827925be8dc7b4db99772d430f5298c3223d7d5b"
MAX_DOWNLOAD = 10 * 1024 * 1024
LATEST_REFRESH_SECONDS = 24 * 3600


def get_bin_path() -> Path:
    return AppConfig.bin_path()


def check_command_available(cmd: str) -> bool:
    if shutil.which(cmd):
        return True
    return (get_bin_path() / cmd).exists()


def ensure_winetricks() -> Path:
    """Return a path to a usable winetricks.

    Default ("pinned" channel): a specific release, verified against a SHA-256 before it
    is ever executed. Offline after the first download.

    "latest" channel (Settings): the unverified master branch, refreshed at most daily.
    Falls back to whatever copy exists (local, then system) if the network is down.
    """
    cfg = AppConfig.load()
    bin_dir = get_bin_path()
    bin_dir.mkdir(parents=True, exist_ok=True)

    if (cfg.winetricks_channel or "pinned") == "latest":
        path = bin_dir / "winetricks-latest"
        fresh = path.exists() and (time.time() - path.stat().st_mtime) < LATEST_REFRESH_SECONDS
        if not fresh:
            try:
                download_file(WINETRICKS_LATEST_URL, path, timeout=30, max_bytes=MAX_DOWNLOAD)
                path.chmod(0o755)
                log_warning("Using UNVERIFIED winetricks from the master branch (channel: latest)")
            except Exception as e:
                if not path.exists():
                    system = shutil.which("winetricks")
                    if system:
                        log_warning(f"Could not download winetricks ({e}); using system copy {system}")
                        return Path(system)
                    raise RuntimeError(f"Failed to download winetricks and no local copy: {e}") from e
                log_warning(f"Could not refresh winetricks ({e}); using cached copy")
        return path

    path = bin_dir / f"winetricks-{WINETRICKS_VERSION}"
    if path.is_file() and sha256_file(path) == WINETRICKS_SHA256:
        return path
    log_info(f"Downloading winetricks {WINETRICKS_VERSION} (checksum-verified)…")
    try:
        download_file(
            WINETRICKS_URL, path, timeout=60,
            expected_sha256=WINETRICKS_SHA256, max_bytes=MAX_DOWNLOAD,
        )
        path.chmod(0o755)
        return path
    except Exception as e:
        system = shutil.which("winetricks")
        if system:
            log_warning(f"Could not fetch pinned winetricks ({e}); using system copy {system}")
            return Path(system)
        raise RuntimeError(
            f"Could not download winetricks and none is installed: {e}\n"
            "Install your distro's 'winetricks' package, or check your network."
        ) from e


def ensure_cabextract() -> Path:
    system = shutil.which("cabextract")
    if system:
        return Path(system)
    bin_dir = get_bin_path()
    dest = bin_dir / "cabextract"
    if dest.exists():
        return dest
    log_warning("System cabextract not found, downloading (checksum-verified)…")
    bin_dir.mkdir(parents=True, exist_ok=True)
    zip_path = bin_dir / "cabextract.zip"
    try:
        download_file(
            CABEXTRACT_URL, zip_path, timeout=120,
            expected_sha256=CABEXTRACT_ZIP_SHA256, max_bytes=MAX_DOWNLOAD,
        )
        with zipfile.ZipFile(zip_path) as zf:
            member = next(
                (m for m in zf.namelist() if Path(m).name == "cabextract" and not m.endswith("/")),
                None,
            )
            if member is None:
                raise RuntimeError("cabextract not found inside the downloaded archive")
            # Read that one member — never extractall() an archive we don't control.
            dest.write_bytes(zf.read(member))
        dest.chmod(0o755)
        log_info(f"cabextract installed to {dest}")
        return dest
    except Exception as e:
        log_warning(f"cabextract unavailable: {e}")
        try:
            dest.unlink()
        except OSError:
            pass
        return Path("cabextract")  # non-fatal; many verbs don't need it
    finally:
        zip_path.unlink(missing_ok=True)


def winetricks_env(prefix_path: Path, proton: SteamProton) -> dict:
    wine = proton.wine_binary()
    wineserver = proton.wineserver_binary()
    if not wine or not wineserver:
        raise RuntimeError("Wine/wineserver not found in Proton")
    cache = AppConfig.default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    env = clean_env()
    env.update(
        {
            "PATH": str(get_bin_path()) + os.pathsep + env.get("PATH", ""),
            "WINE": str(wine),
            "WINESERVER": str(wineserver),
            "WINEPREFIX": str(prefix_path),
            "WINETRICKS_CACHE": str(cache),
            "WINEDEBUG": "-all",
        }
    )
    return env


# kept for backwards compatibility with older imports
_winetricks_env = winetricks_env


def run_winetricks(
    prefix_path: Path,
    proton: SteamProton,
    verbs: list[str],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_flag: Optional[Callable[[], bool]] = None,
    *,
    per_verb: bool = True,
) -> list[str]:
    """
    Install verbs. Returns list of verbs that failed.

    When per_verb=True (default), each verb is run separately so one failure
    does not abort the rest. Winetricks output is streamed to ``log_callback``
    and written to the log file.
    """
    if not verbs:
        return []
    cb = log_callback or (lambda m: None)
    wt = ensure_winetricks()
    ensure_cabextract()
    env = winetricks_env(prefix_path, proton)
    failed: list[str] = []

    def _on_line(line: str) -> None:
        log_debug(f"[winetricks] {line}")
        cb(line)

    def _run_one(verb_list: list[str]) -> int:
        msg = f"winetricks -q {' '.join(verb_list)}"
        cb(msg)
        log_install(msg)
        return run_streaming([wt, "-q", *verb_list], env=env, on_line=_on_line, cancel=cancel_flag)

    if per_verb:
        total = len(verbs)
        for i, v in enumerate(verbs, 1):
            if cancel_flag and cancel_flag():
                raise RuntimeError("Cancelled")
            cb(f"[{i}/{total}] Installing {v}…")
            code = _run_one([v])
            if code != 0:
                log_warning(f"winetricks verb failed (exit {code}): {v}")
                cb(f"Warning: {v} failed (exit {code})")
                failed.append(v)
            else:
                log_install(f"OK: {v}")
    else:
        code = _run_one(verbs)
        if code != 0:
            raise RuntimeError(f"Winetricks failed with exit code {code}")

    if failed:
        log_warning(f"Failed verbs: {', '.join(failed)}")
    else:
        log_install("Winetricks completed successfully")
    return failed


def install_standard_deps(
    prefix_path: Path,
    proton: SteamProton,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_flag: Optional[Callable[[], bool]] = None,
    extra_verbs: Optional[list[str]] = None,
    profile: str = "standard",
) -> list[str]:
    """Install a runtime set (+ extra verbs). Returns list of failed verbs."""
    verbs = verbs_for_profile(profile, extra_verbs)
    return run_winetricks(prefix_path, proton, verbs, log_callback, cancel_flag, per_verb=True)


def verify_prefix_runtimes(prefix_path: Path) -> tuple[list[str], list[str]]:
    """
    Check that expected runtime DLLs exist under the prefix.
    Returns (found, missing).
    """
    root = Path(prefix_path)
    found: list[str] = []
    missing: list[str] = []
    for rel in VERIFY_PATHS:
        p = root / rel
        if p.is_file():
            found.append(rel)
        else:
            missing.append(rel)
    return found, missing
