"""
Loads the distro modules for package-mode callers.

Mirrors the selection rules of the app's GtDistroManager (best os-release match,
honouring the Plugins-window on/off switches and the manual override stored in
~/.local/share/griffinhub/settings.json).  If the app already loaded the modules
in this process they are reused, never loaded twice.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_STATE: dict = {"loaded": False, "api": None, "classes": {}, "active": None, "computed": False}


def _module_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "distro_modules"


def _load_file(name: str, path: Path):
    if name in sys.modules:                      # already loaded by the app
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _settings() -> dict:
    try:
        return json.loads((Path.home() / ".local/share/griffinhub/settings.json").read_text())
    except Exception:
        return {}


def _load_all() -> None:
    if _STATE["loaded"]:
        return
    _STATE["loaded"] = True
    d = _module_dir()
    api_path = d / "_api.py"
    if not api_path.is_file():
        return
    api = _load_file("gh_distro_api", api_path)
    _STATE["api"] = api
    for p in sorted(d.glob("*.py")):
        if p.name.startswith("_"):
            continue
        try:
            mod = _load_file("gh_distro_" + p.stem, p)
            cls = getattr(mod, "Module")
            if issubclass(cls, api.DistroModule):
                _STATE["classes"][cls.id] = cls
        except Exception:
            continue


def active_module():
    """The distro module instance for this system, or None."""
    _load_all()
    if _STATE["computed"]:
        return _STATE["active"]
    api, classes = _STATE["api"], _STATE["classes"]
    _STATE["computed"] = True
    _STATE["active"] = None
    if not api or not classes:
        return None
    st = _settings()
    disabled = set(st.get("distro_modules_disabled", []))
    override = st.get("distro_module_override", "") or ""
    osr = api.parse_os_release()
    pick = None
    if override in classes and override not in disabled:
        pick = override
    else:
        best = 0
        for mid, cls in classes.items():
            if mid in disabled:
                continue
            sc = cls.match_score(osr)
            if sc > best:
                best, pick = sc, mid
    if pick:
        try:
            _STATE["active"] = classes[pick](osr)
        except Exception:
            _STATE["active"] = None
    return _STATE["active"]


def feature(key: str):
    """Resolved feature from the active module, or None when there is no module."""
    m = active_module()
    if m is None:
        return None
    try:
        return m.resolve(key)
    except Exception:
        return None


def feature_script(key: str) -> str:
    """Install script for a feature, or a shell snippet that explains why it is unavailable."""
    r = feature(key)
    if r is None:
        return "echo 'Griffin Hub distro modules are not available on this system.'; exit 1"
    if r.ok:
        return r.install
    import shlex
    return "echo " + shlex.quote("ERROR: " + r.reason) + "; exit 1"


def invalidate() -> None:
    _STATE["computed"] = False
    _STATE["active"] = None
