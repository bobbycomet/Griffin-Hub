"""Create a minimal fake Steam install (with one Proton build) under a HOME directory for tests."""
import os
from pathlib import Path

CONFIG_VDF = '''"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"AutoUpdateWindowStart"\t\t"-1"
\t\t\t\t"CompatToolMapping"
\t\t\t\t{
\t\t\t\t\t"0"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"proton_experimental"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"75"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
'''

def make(home: str, proton: str = "GE-Proton10-2", account: str = "12345") -> Path:
    home = Path(home)
    steam = home / ".local/share/Steam"
    (steam / "steamapps/common").mkdir(parents=True, exist_ok=True)
    (steam / "config").mkdir(parents=True, exist_ok=True)
    (steam / "config/config.vdf").write_text(CONFIG_VDF)
    (steam / f"userdata/{account}/config").mkdir(parents=True, exist_ok=True)
    (steam / "config/loginusers.vdf").write_text(
        '"users"\n{\n\t"%d"\n\t{\n\t\t"AccountName"\t\t"tester"\n\t\t"PersonaName"\t\t"Tester"\n\t\t"MostRecent"\t\t"1"\n\t\t"Timestamp"\t\t"1700000000"\n\t}\n}\n'
        % (76561197960265728 + int(account)))
    p = steam / "compatibilitytools.d" / proton
    (p / "files/bin").mkdir(parents=True, exist_ok=True)
    (p / "proton").write_text(
        '#!/bin/sh\n# fake proton: "proton run wineboot -u" creates the prefix like the real one\n'
        'mkdir -p "$STEAM_COMPAT_DATA_PATH/pfx/drive_c/windows/system32" "$STEAM_COMPAT_DATA_PATH/pfx/drive_c/windows/syswow64"\n'
        'touch "$STEAM_COMPAT_DATA_PATH/pfx/system.reg"\nexit 0\n')
    for f in ("files/bin/wine", "files/bin/wineserver"):
        (p / f).write_text("#!/bin/sh\nexit 0\n")
    for f in ("proton", "files/bin/wine", "files/bin/wineserver"):
        os.chmod(p / f, 0o755)
    (steam / "steamapps/libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t\t"apps"\n\t\t{\n\t\t}\n\t}\n}\n' % steam)
    return steam
