"""Steam config.vdf CompatToolMapping helpers.

Edits are done *in place* using a position-aware scanner, so the rest of
config.vdf is left byte-for-byte untouched. A backup is written first and
the new file is swapped in atomically.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from griffin.logging_utils import log_info, log_warning
from griffin.steam.paths import find_steam_path
from griffin.steam.vdf_parse import Block, find_path, parse_blocks, tokenize
from griffin.utils import atomic_write_text

_STEAM_PATH = ["InstallConfigStore", "Software", "Valve", "Steam"]


def _validate(name: str) -> None:
    if not name or '"' in name or "\n" in name or "\r" in name or "\\" in name:
        raise ValueError(f"Invalid Proton name: {name!r}")


def _line_indent(content: str, pos: int) -> tuple[int, str, bool]:
    """Return (line_start, indent, brace_alone) for the line containing ``pos``."""
    ls = content.rfind("\n", 0, pos) + 1
    prefix = content[ls:pos]
    alone = prefix.strip() == ""
    indent = prefix if alone else ""
    return ls, indent, alone


def _entry_text(app_id: str, name: str, ind: str) -> str:
    return (
        f'{ind}"{app_id}"\n'
        f"{ind}{{\n"
        f'{ind}\t"name"\t\t"{name}"\n'
        f'{ind}\t"config"\t\t""\n'
        f'{ind}\t"priority"\t\t"250"\n'
        f"{ind}}}\n"
    )


def _insert_before_close(content: str, toks, blk: Block, text_for_indent) -> str:
    close = toks[blk.close_tok]
    ls, ind, alone = _line_indent(content, close.start)
    child_ind = ind + "\t"
    text = text_for_indent(child_ind)
    if alone:
        return content[:ls] + text + content[ls:]
    return content[: close.start] + "\n" + text + content[close.start :]


def _backup(config_path: Path) -> None:
    orig = config_path.with_name(config_path.name + ".griffin-orig")
    bak = config_path.with_name(config_path.name + ".griffin.bak")
    try:
        if not orig.exists():
            shutil.copy2(config_path, orig)  # pristine copy from before we ever touched it
        shutil.copy2(config_path, bak)  # most recent previous state
    except OSError as e:
        raise RuntimeError(f"Could not back up config.vdf: {e}") from e


def _load(config_path: Path):
    content = config_path.read_text(encoding="utf-8", errors="replace")
    toks = tokenize(content)
    root = parse_blocks(toks)
    steam_blk = find_path(root, _STEAM_PATH)
    if steam_blk is None or steam_blk.close_tok < 0:
        raise RuntimeError("Could not find the Steam section in config.vdf")
    return content, toks, steam_blk


def set_compat_tool(app_id: int, proton_name: str) -> None:
    _validate(proton_name)
    steam = find_steam_path()
    if not steam:
        raise RuntimeError("Steam not found")
    config_path = steam / "config" / "config.vdf"
    if not config_path.exists():
        raise RuntimeError("Steam config.vdf not found")

    app = str(int(app_id))
    content, toks, steam_blk = _load(config_path)
    cm = steam_blk.child("CompatToolMapping")

    if cm is not None and cm.close_tok >= 0:
        entry = cm.child(app)
        if entry is not None and entry.close_tok >= 0:
            vi = entry.values.get("name")
            if vi is not None:
                t = toks[vi]
                new = content[: t.start] + f'"{proton_name}"' + content[t.end :]
            else:
                new = _insert_before_close(
                    content, toks, entry, lambda ind: f'{ind}"name"\t\t"{proton_name}"\n'
                )
        else:
            new = _insert_before_close(
                content, toks, cm, lambda ind: _entry_text(app, proton_name, ind)
            )
    else:
        def block(ind: str) -> str:
            return (
                f'{ind}"CompatToolMapping"\n{ind}{{\n'
                + _entry_text(app, proton_name, ind + "\t")
                + f"{ind}}}\n"
            )

        new = _insert_before_close(content, toks, steam_blk, block)

    if new == content:
        log_info(f"CompatToolMapping for {app} already set to {proton_name}")
        return

    # Re-parse the result to make sure we produced valid, balanced VDF.
    new_toks = tokenize(new)
    new_root = parse_blocks(new_toks)
    check = find_path(new_root, _STEAM_PATH + ["CompatToolMapping", app])
    if check is None or new_toks[check.values.get("name", 0)].text != proton_name:
        raise RuntimeError("Refusing to write config.vdf: edit did not verify")

    _backup(config_path)
    atomic_write_text(config_path, new)


def remove_compat_tool(app_id: int) -> bool:
    """Remove the CompatToolMapping entry for ``app_id`` (best effort). Returns True if removed."""
    steam = find_steam_path()
    if not steam:
        return False
    config_path = steam / "config" / "config.vdf"
    if not config_path.exists():
        return False
    try:
        content, toks, steam_blk = _load(config_path)
    except RuntimeError as e:
        log_warning(str(e))
        return False
    cm = steam_blk.child("CompatToolMapping")
    entry = cm.child(str(int(app_id))) if cm else None
    if not entry or entry.close_tok < 0:
        return False
    key_tok, close_tok = toks[entry.key_tok], toks[entry.close_tok]
    start = content.rfind("\n", 0, key_tok.start) + 1
    end = content.find("\n", close_tok.end)
    end = len(content) if end == -1 else end + 1
    _backup(config_path)
    atomic_write_text(config_path, content[:start] + content[end:])
    return True
