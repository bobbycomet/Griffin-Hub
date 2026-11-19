"""Lightweight VDF (text) parser for appmanifest / libraryfolders."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def parse_vdf(content: str) -> dict[str, Any]:
    tokens: list[str] = []
    i = 0
    n = len(content)
    while i < n:
        c = content[i]
        if c.isspace():
            i += 1
            continue
        if c == "/" and i + 1 < n and content[i + 1] == "/":
            while i < n and content[i] != "\n":
                i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n:
                if content[i] == "\\":
                    i += 1
                    if i < n:
                        buf.append(content[i])
                        i += 1
                    continue
                if content[i] == '"':
                    i += 1
                    break
                buf.append(content[i])
                i += 1
            tokens.append("".join(buf))
            continue
        if c in "{}":
            tokens.append(c)
            i += 1
            continue
        i += 1

    def parse_object(idx: int) -> tuple[dict[str, Any], int]:
        obj: dict[str, Any] = {}
        while idx < len(tokens):
            t = tokens[idx]
            if t == "}":
                return obj, idx + 1
            if t == "{":
                idx += 1
                continue
            key = t
            idx += 1
            if idx >= len(tokens):
                break
            if tokens[idx] == "{":
                child, idx = parse_object(idx + 1)
                obj[key] = child
            else:
                obj[key] = tokens[idx]
                idx += 1
        return obj, idx

    root, _ = parse_object(0)
    # Often root is a single key like AppState / libraryfolders
    return root


def parse_library_folders(content: str) -> list[str]:
    """
    Extract library root paths from libraryfolders.vdf.

    Supports modern nested form::

        "1" { "path" "/mnt/sdb1/SteamLibrary" ... }

    and the older flat form::

        "1"  "/mnt/sdb1/SteamLibrary"
    """
    root = parse_vdf(content)
    folders = root.get("libraryfolders", root)
    paths: list[str] = []
    if not isinstance(folders, dict):
        return paths

    def _looks_like_path(s: str) -> bool:
        if not s or not isinstance(s, str):
            return False
        s = s.strip()
        # Absolute Unix path, or Windows-style (rare on Linux Steam)
        return s.startswith("/") or (len(s) >= 3 and s[1] == ":" and s[0].isalpha())

    for key, v in folders.items():
        if isinstance(v, dict):
            p = v.get("path") or v.get("Path") or v.get("PATH")
            if isinstance(p, str) and _looks_like_path(p):
                paths.append(p.strip())
                continue
            # Sometimes the only string values are nested paths
            for sv in v.values():
                if isinstance(sv, str) and _looks_like_path(sv):
                    paths.append(sv.strip())
                    break
        elif isinstance(v, str) and _looks_like_path(v):
            # Older flat libraryfolders.vdf: "1" "/mnt/..."
            paths.append(v.strip())
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out



def parse_library_folders_detailed(content: str) -> list[dict]:
    """
    Parse libraryfolders.vdf into structured entries.

    Each entry::

        {"path": "/mnt/sdb1/SteamLibrary", "apps": {"489830", "22380", ...}}

    Modern nested form includes an ``apps`` map (appid → size). Older flat
    form only has the path string — ``apps`` is then empty and callers should
    fall back to scanning steamapps/appmanifest_*.acf.
    """
    root = parse_vdf(content)
    folders = root.get("libraryfolders", root)
    out: list[dict] = []
    if not isinstance(folders, dict):
        return out

    def _looks_like_path(s: str) -> bool:
        if not s or not isinstance(s, str):
            return False
        s = s.strip()
        return s.startswith("/") or (len(s) >= 3 and s[1] == ":" and s[0].isalpha())

    for key, v in folders.items():
        # Skip non-folder metadata keys
        if key in ("contentstatsid", "TimeNextStatsReport"):
            continue
        path: str | None = None
        apps: set[str] = set()
        if isinstance(v, dict):
            p = v.get("path") or v.get("Path") or v.get("PATH")
            if isinstance(p, str) and _looks_like_path(p):
                path = p.strip()
            apps_node = v.get("apps") or v.get("Apps")
            if isinstance(apps_node, dict):
                for app_id in apps_node.keys():
                    if str(app_id).isdigit():
                        apps.add(str(app_id))
            if path is None:
                for sv in v.values():
                    if isinstance(sv, str) and _looks_like_path(sv):
                        path = sv.strip()
                        break
        elif isinstance(v, str) and _looks_like_path(v):
            path = v.strip()
        if path:
            out.append({"path": path, "apps": apps})

    # Deduplicate by resolved path string
    seen: set[str] = set()
    deduped: list[dict] = []
    for entry in out:
        p = entry["path"]
        if p in seen:
            continue
        seen.add(p)
        deduped.append(entry)
    return deduped


class AppManifest:
    def __init__(self, app_id: str, name: str, install_dir: str, state_flags: int):
        self.app_id = app_id
        self.name = name
        self.install_dir = install_dir
        self.state_flags = state_flags

    @classmethod
    def from_vdf(cls, content: str) -> Optional["AppManifest"]:
        root = parse_vdf(content)
        state = root.get("AppState", root)
        if not isinstance(state, dict):
            return None
        try:
            return cls(
                app_id=str(state.get("appid", "")),
                name=str(state.get("name", "")),
                install_dir=str(state.get("installdir", "")),
                state_flags=int(state.get("StateFlags", "0") or 0),
            )
        except Exception:
            return None

    def is_installed(self) -> bool:
        # StateFlags is a bitmask; bit 4 = fully installed (other bits may be set too,
        # e.g. 6 = installed + update required).
        return bool(self.state_flags & 4)



# ---------------------------------------------------------------------------
# Position-aware scanner — used to *edit* VDF text in place without rewriting
# (and possibly mangling) the rest of the file.
# ---------------------------------------------------------------------------
@dataclass
class Tok:
    kind: str  # "str" | "{" | "}"
    text: str
    start: int
    end: int


def tokenize(content: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(content)
    while i < n:
        c = content[i]
        if c.isspace():
            i += 1
        elif c == "/" and i + 1 < n and content[i + 1] == "/":
            while i < n and content[i] != "\n":
                i += 1
        elif c == '"':
            start = i
            i += 1
            buf: list[str] = []
            while i < n:
                ch = content[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(content[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            toks.append(Tok("str", "".join(buf), start, i))
        elif c in "{}":
            toks.append(Tok(c, c, i, i + 1))
            i += 1
        else:
            i += 1
    return toks


@dataclass
class Block:
    key: str
    key_tok: int
    open_tok: int
    close_tok: int = -1
    blocks: list["Block"] = field(default_factory=list)
    values: dict[str, int] = field(default_factory=dict)  # lower-case key -> value token index

    def child(self, key: str) -> Optional["Block"]:
        k = key.lower()
        for b in self.blocks:
            if b.key.lower() == k:
                return b
        return None


def parse_blocks(tokens: list[Tok]) -> Block:
    root = Block("", -1, -1)

    def parse(idx: int, blk: Block) -> int:
        while idx < len(tokens):
            t = tokens[idx]
            if t.kind == "}":
                blk.close_tok = idx
                return idx + 1
            if t.kind == "{":
                idx += 1
                continue
            nxt = tokens[idx + 1] if idx + 1 < len(tokens) else None
            if nxt is not None and nxt.kind == "{":
                child = Block(t.text, idx, idx + 1)
                blk.blocks.append(child)
                idx = parse(idx + 2, child)
            elif nxt is not None and nxt.kind == "str":
                blk.values[t.text.lower()] = idx + 1
                idx += 2
            else:
                idx += 1
        return idx

    parse(0, root)
    return root


def find_path(root: Block, path: list[str]) -> Optional[Block]:
    cur: Optional[Block] = root
    for key in path:
        cur = cur.child(key) if cur else None
        if cur is None:
            return None
    return cur
