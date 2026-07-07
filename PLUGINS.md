# Writing Griffin Hub Plugins

Griffin Hub plugins extend exactly two things: **System Tune** and the **Hardware Hub**. A plugin
is a single Python file (or a small folder, see Multi-file plugins that
declares diagnostic checks, one-click actions, and for trusted plugins only, small, well-defined
extensions to a couple of built-in configuration points.
---

## Quick start

1. Create a Python file anywhere, e.g. `my_plugin.py`.
2. Define `PLUGIN_METADATA` and at least one of `PLUGIN_CHECKS` / `PLUGIN_ACTIONS`.
3. Drop it into `~/.local/share/griffinhub/plugins/`.
4. Open Griffin Hub → **Plugins** tab → **Reload Plugins** (or restart the app).

That's it. Your check/action now shows up on the Plugins page, grouped under "System Tune
Extensions" or "Hardware Hub Extensions" depending on the `category` you gave it.

A minimal working plugin:

```python
# my_plugin.py
PLUGIN_METADATA = {
    "version": "1.0",
    "api_version": 1,
    "author": "Your Name",
    "description": "Checks that zram-tools is installed.",
}

PLUGIN_CHECKS = [
    {
        "id": "zram_tools_installed",
        "name": "zram-tools installed",
        "cmd": ["dpkg", "-s", "zram-tools"],
        "category": "tune",
        "fix_hint": "Install it with: sudo apt install zram-tools",
    }
]
```

Save it, drop it in the plugins folder, hit Reload, you'll see "zram-tools installed" under
"System Tune Extensions" on the Plugins page, with a **Run Check** button.

---

## The four things a plugin can define

| Name | Type | Purpose |
|---|---|---|
| `PLUGIN_METADATA` | `dict` | Version, author, description, trust level, API version, load priority |
| `PLUGIN_CHECKS` | `list[dict]` | Diagnostic checks shown with a "Run Check" button |
| `PLUGIN_ACTIONS` | `list[dict]` | One-click actions shown with a "Run Action" button |
| `PLUGIN_EXTENDS` | `dict` | Patches to a named extension point (**trusted plugins only** for dict-valued fields — see below) |

You can define any subset of these. A plugin with only `PLUGIN_CHECKS` is completely valid.

There's also an optional imperative hook:

| Name | Type | Purpose |
|---|---|---|
| `register(registry)` | `function` | Called at startup for **trusted, in-process plugins only** |

---

## PLUGIN_METADATA

```python
PLUGIN_METADATA = {
    "version": "1.2.0",           # your plugin's own version string
    "api_version": 1,             # Griffin Hub plugin API version this plugin targets
    "author": "Your Name",
    "description": "One sentence describing what this plugin does.",
    "trust_level": "safe",        # "safe" (default) or "trusted", see Trust levels below
    "priority": 100,              # optional, default 100, lower loads first (see Load order)
}
```

- `api_version` lets Griffin Hub warn (not necessarily block) if your plugin targets a newer API
  than the running build supports. Current API version is **1**.
- `trust_level` is what the plugin *asks for*. Whether it's actually granted is a local, manual
  decision — see Trust levels
- `priority` controls load order when multiple plugins extend the same thing. Plugins are sorted by
  `(priority ascending, filename ascending)`. This matters for `PLUGIN_EXTENDS` — see below.

---

## PLUGIN_CHECKS

A check is a read-only diagnostic: run a command, see if it succeeds. Checks show up on the Plugins
page with a **Run Check** button and, on failure, a hint if you provided one.

```python
PLUGIN_CHECKS = [
    {
        "id": "unique_id",             # required, must be unique across all plugins
        "name": "Human-readable name", # required, shown in the UI
        "cmd": ["which", "mangohud"],  # required, list[str] — never a shell string
        "category": "tune",            # required: "tune" or "hardware"
        "fix_hint": "Optional text shown if the check fails.",  # optional
    },
]
```

Rules:
- `cmd` must be a `list[str]`, never a shell string, this is enforced see Safety
- `category` must be exactly `"tune"` or `"hardware"`, this is what determines whether it shows up
  under System Tune Extensions or Hardware Hub Extensions on the Plugins page.
- The command is executed as plain subprocess and checked against an allowlist of executables see
  Reference at the bottom, you can't just run
  arbitrary binaries, only ones useful for diagnostics.
- `id` collisions are silently ignored (first registration wins) — pick something namespaced to your
  plugin, e.g. `"myplugin_zram_check"`, to avoid clashing with someone else's plugin.

## PLUGIN_ACTIONS

An action is a one-click operation, install a package, enable a service, apply a fix. Actions show
up with a **Run Action** button.

```python
PLUGIN_ACTIONS = [
    {
        "id": "unique_id",                     # required, must be unique
        "name": "Enable Foo Service",          # required
        "cmd": ["systemctl", "enable", "foo"], # required, list[str]
        "category": "hardware",                # required: "tune" or "hardware"
        "use_pkexec": True,                    # optional, default False
        "description": "What this does, shown as a tooltip.",  # optional
    },
]
```

- Set `use_pkexec: True` for anything that needs root (installing packages, writing to `/etc`,
  controlling services). Griffin Hub routes it through `pkexec` so the app itself never runs as
  root — the user gets a normal authentication prompt.
- Without `use_pkexec`, the command runs as a plain subprocess under the same allowlist as checks.
- Actions with `use_pkexec: True` are **not** restricted to the check/action allowlist, since
  `pkexec` already gates them behind authentication, but that also means you should be conservative
  about what you put here. Don't make an action do something a user wouldn't expect from its name.

---

## PLUGIN_EXTENDS and extension targets

Most plugins won't need this — `PLUGIN_CHECKS` and `PLUGIN_ACTIONS` cover the vast majority of useful
plugins. `PLUGIN_EXTENDS` exists for the rarer case where you want to add to something Griffin Hub
already manages, rather than bolt on a separate check/action.

There are currently two extension targets:

```python
EXTENSION_TARGETS = {
    # Extra packages offered on the System Tune → Setup tab's install list.
    "tune_setup_packages": {"packages": []},

    # Extra chips shown on the Hardware Hub overview (informational only).
    "hardware_notes": {"notes": []},
}
```

To extend one, declare a patch:

```python
PLUGIN_EXTENDS = {
    "tune_setup_packages": {
        "packages": ["zram-tools", "earlyoom"],
    },
}
```

**Merge rules** (deterministic, no plugin can silently overwrite another plugin's contribution):

- **List fields** (like `packages` above): your entries are appended to whatever's already there,
  de-duplicated. This works for *any* plugin, safe or trusted.
- **Dict-valued fields**: merged with your plugin's keys winning on collision — but this is
  **restricted to trusted plugins**. There are no dict-valued fields in the built-in
  `EXTENSION_TARGETS` today, but the mechanism exists for future extension points.
- **Unknown target name** or **unknown field within a target**: logged as a warning and ignored;
  this is a forward-compatibility guard, not a silent failure, so check the Plugins page for load
  warnings if something you extended doesn't show up.

**Load order**: all plugins' `PLUGIN_CHECKS`/`PLUGIN_ACTIONS` register first (in priority order),
*then* all `PLUGIN_EXTENDS` patches apply (also in priority order) — so a plugin extending a target
always sees contributions from every other loaded plugin, regardless of which one happened to load
first on disk.

---

## Trust levels: safe vs. trusted

Every plugin runs at one of two trust levels:

| | `safe` (default) | `trusted` |
|---|---|---|
| `PLUGIN_CHECKS` / `PLUGIN_ACTIONS` | ✅ | ✅ |
| List-valued `PLUGIN_EXTENDS` fields | ✅ | ✅ |
| Dict-valued `PLUGIN_EXTENDS` fields | ❌ | ✅ |
| `register(registry)` hook called | ❌ | ✅ |
| Runs sandboxed (community/store plugins) | Always | N/A, trusted implies in-process |

**A plugin cannot grant itself `trusted` status.** Setting `trust_level: "trusted"` in your own
`PLUGIN_METADATA` does nothing by itself. Trust is a **local, manual, per-user decision**:

1. The plugin file must be loaded **in-process** (not sandboxed) — which by default only applies to
   plugins the user placed directly in the plugins folder themselves, not ones fetched through the
   community store unless the user has disabled sandboxing for verified plugins in Settings.
2. The user must manually create a **`.trusted` sidecar file** next to the plugin for
   `my_plugin.py`, that's a file literally named `my_plugin.trusted` (contents don't matter; its
   existence is the signal) in the same folder.
3. *Only then* does the plugin's own `trust_level: "trusted"` declaration take effect.

This exists so that a plugin can't just claim elevated trust in its own metadata and get it,
elevating trust always requires a manual, deliberate action by the person running Griffin Hub,
separate from anything the plugin's code says about itself.

If you're publishing a plugin for others to install from the community store, assume it will run
`safe` and sandboxed unless the user has specifically decided otherwise. Design your plugin to be
useful at the `safe` tier checks and actions cover the overwhelming majority of legitimate plugin
ideas.

---

## The register() hook

For **trusted, in-process** plugins only, you can define an imperative `register()` function instead
of (or alongside) the declarative lists:

```python
def register(registry):
    registry.add_check({
        "id": "myplugin_check",
        "name": "My Check",
        "cmd": ["which", "foo"],
        "category": "tune",
    })
    registry.add_action({
        "id": "myplugin_action",
        "name": "Fix Foo",
        "cmd": ["systemctl", "restart", "foo"],
        "category": "tune",
        "use_pkexec": True,
    })
    registry.extend("tune_setup_packages", {"packages": ["foo-tools"]})
```

This is equivalent to declaring `PLUGIN_CHECKS`/`PLUGIN_ACTIONS`/`PLUGIN_EXTENDS` directly, but lets
you compute the contents dynamically (e.g. only register a check if some condition on the local
system is true). If your plugin isn't trusted, `register()` is simply never called — declarative
content (if you also included any) still loads normally, so it's safe to include both.

---

## Safety: the sandbox and the AST scanner

Two independent layers of protection apply to every plugin before it runs:

### 1. The sandbox

By default (`Settings → Sandbox community plugins`, on by default), plugins that aren't marked
`.trusted` are evaluated in an isolated environment rather than imported directly into the running
app:

- If `bubblewrap` (`bwrap`) is installed, the plugin runs inside a proper namespace sandbox: no home
  directory access, no network, minimal environment.
- If `bwrap` isn't available, Griffin Hub falls back to a stripped Python subprocess (`-S`, no
  site-packages, minimal env) — less isolation, but still separate from the main process.
- Either way, a sandboxed plugin can only communicate back to Griffin Hub through its declarative
  exports (`PLUGIN_CHECKS`, `PLUGIN_ACTIONS`, `PLUGIN_EXTENDS`, `PLUGIN_METADATA`, serialized as
  JSON) — arbitrary code the plugin runs at import time doesn't get to touch the running app, and
  `register()` is never called for a sandboxed plugin.

### 2. The AST safety scanner

Before a plugin is executed *at all* (sandboxed or not), Griffin Hub parses its source and rejects it
outright if it contains:

- `os.system()`, `os.popen()`, `subprocess.run/call/Popen/check_call/check_output/...`
- `eval()`, `exec()`, `compile()`, `__import__()`, `breakpoint()`
- `shutil.rmtree()`
- Dynamic import bypasses (`importlib.import_module`, `.reload()`, obfuscated `getattr()` calls
  targeting any of the above)
- Dangerous shell-command literals appearing anywhere in the source (`rm -rf`, `sudo `, `dd if=`,
  `mkfs`, `> /dev/`, pipe-to-shell patterns, `chmod +x`, etc.)
- Assigning a blocked callable to another name and calling *that* instead (a common obfuscation trick)

This is defense-in-depth, not a substitute for reading a plugin before installing it — but it does
mean a plugin that runs a subprocess directly (instead of declaring a `PLUGIN_CHECKS`/`PLUGIN_ACTIONS`
entry) will simply fail to load, with the specific violation logged. **This is intentional.** If your
plugin needs to run a command, that's exactly what checks and actions are for — the scanner is telling
you you're doing it the wrong way, not that you've hit a bug.

> **Gotcha:** the literal-scanning rule above looks at *every string constant in the file*,
> including strings sitting inside your `PLUGIN_CHECKS`/`PLUGIN_ACTIONS` declarations, not just
> code that's actually executed. A perfectly legitimate action like clearing a cache directory with
> `rm -rf some/path` will be rejected outright, because the substring `"rm -rf"` appears in your
> source regardless of the fact that it's just sitting in a `cmd` list. If you hit this, rephrase the
> command to avoid the flagged substring (e.g. `find <dir> -mindepth 1 -delete` instead of
> `rm -rf <dir>/*`) rather than trying to obfuscate it — obfuscation is exactly what the scanner is
> also watching for.

---

## Full example: a WiFi diagnostic plugin

A complete, realistic `safe` plugin — checks for a known-problematic chip and offers a fix action:

```python
# rtl8821au_fix.py
PLUGIN_METADATA = {
    "version": "1.0",
    "api_version": 1,
    "author": "Community",
    "description": "Detects RTL8821AU USB WiFi adapters and offers the DKMS driver fix.",
}

PLUGIN_CHECKS = [
    {
        "id": "rtl8821au_driver_installed",
        "name": "RTL8821AU DKMS driver installed",
        "cmd": ["dpkg", "-s", "rtl8821au-dkms"],
        "category": "hardware",
        "fix_hint": "Run the 'Install RTL8821AU DKMS Driver' action below if this adapter needs it.",
    },
]

PLUGIN_ACTIONS = [
    {
        "id": "rtl8821au_dkms_install",
        "name": "Install RTL8821AU DKMS Driver",
        "cmd": [
            "bash", "-c",
            "apt-get update -qq && apt-get install -y git dkms linux-headers-generic && "
            "git clone https://github.com/morrownr/8821au-20210708 /tmp/rtl8821au && "
            "cd /tmp/rtl8821au && ./install-driver.sh NoPrompt"
        ],
        "category": "hardware",
        "use_pkexec": True,
        "description": "Builds and installs the out-of-tree driver via DKMS so it survives kernel updates.",
    },
]
```

> Note: the check above uses `dpkg -s`, which is on the executable allowlist, so it runs as a plain
> subprocess with no special privileges. The action's `bash -c "..."` is only allowed because it's
> declared with `use_pkexec: True` — actions routed through `pkexec` aren't restricted to the
> allowlist, since `pkexec`'s own authentication prompt is the gate. Either way, the safety scanner
> only cares about what your *plugin's Python code* does, not what commands your declared `cmd` lists
> ask Griffin Hub to run on your behalf — there's no code execution inside the plugin file itself
> here at all, which is why this passes the scanner cleanly.

---

## Full example: a trusted plugin using register() and PLUGIN_EXTENDS

This one needs a `.trusted` sidecar to actually get its extras — without one, it still loads fine
and its checks still work, just as a `safe` plugin (the `register()` hook and the dict-valued
extend, if there were one, simply wouldn't run).

```python
# gaming_extras.py
import shutil

PLUGIN_METADATA = {
    "version": "1.0",
    "api_version": 1,
    "author": "Your Name",
    "description": "Adds gaming-oriented packages to Setup and a habit-cleanup action.",
    "trust_level": "trusted",   # only takes effect with a gaming_extras.trusted sidecar file
    "priority": 50,             # loads before default-priority (100) plugins
}

def register(registry):
    # Only offer this check if the tool it's checking for could plausibly exist.
    if shutil.which("dpkg"):
        registry.add_check({
            "id": "gaming_extras_gamemode_check",
            "name": "GameMode installed",
            "cmd": ["dpkg", "-s", "gamemode"],
            "category": "tune",
            "fix_hint": "Included automatically once you add gamemode to Setup's package list.",
        })

    registry.add_action({
        "id": "gaming_extras_trim_ssd",
        "name": "Trim SSD Now",
        "cmd": ["fstrim", "-av"],
        "category": "tune",
        "use_pkexec": True,
        "description": "Runs fstrim across all mounted, trim-capable filesystems immediately.",
    })

    # List-valued extend also works fine without register() — shown here for completeness.
    registry.extend("tune_setup_packages", {"packages": ["earlyoom", "zram-tools"]})
```

To actually enable the trusted behavior for this plugin:

```bash
touch ~/.local/share/griffinhub/plugins/gaming_extras.trusted
```

Then reload plugins from the Plugins page.

---

## Cloning driver sources with git (e.g. controller drivers)

Some hardware — an uncommon gamepad, a niche WiFi chip, only has a working Linux driver as source
you build yourself, not a distro package. `git` is on the checks/actions allowlist specifically for
this: a plugin can clone a driver's repo and build/install it, all through the normal
`PLUGIN_CHECKS`/`PLUGIN_ACTIONS` mechanism, no special plugin API beyond what you've already seen.

The recommended pattern is **two separate actions**:

1. A **clone action**, with `use_pkexec: False`, that clones into
   `~/.local/share/griffinhub/drivers/<name>` (a directory Griffin Hub creates and owns for exactly
   this purpose — never clone into a system path directly). This step doesn't need root at all;
   it's just a network fetch into a directory you already own.
2. A **build/install action**, with `use_pkexec: True`, that builds and installs from that already-cloned
   directory (DKMS, `make install`, etc.) — this step needs root, since installing a kernel module
   does.

Splitting it this way means a failed or partial clone never touches anything privileged, and a user
re-running the install action after a driver update doesn't need to re-authenticate just to re-fetch
source they already have.

```python
# xbox_elite2_driver.py
from pathlib import Path

PLUGIN_METADATA = {
    "version": "1.0",
    "api_version": 1,
    "author": "Your Name",
    "description": "Clones and installs an out-of-tree Xbox Elite Series 2 controller driver.",
}

DRIVER_DIR = Path.home() / ".local/share/griffinhub/drivers/xbox-elite2-driver"

PLUGIN_CHECKS = [
    {
        "id": "xbox_elite2_source_cloned",
        "name": "Xbox Elite 2 driver source present",
        "cmd": ["git", "-C", str(DRIVER_DIR), "rev-parse", "--is-inside-work-tree"],
        "category": "hardware",
        "fix_hint": "Run 'Clone Xbox Elite 2 Driver Source' below first.",
    },
]

PLUGIN_ACTIONS = [
    {
        "id": "xbox_elite2_clone",
        "name": "Clone Xbox Elite 2 Driver Source",
        "cmd": ["git", "clone", "--depth", "1",
                "https://github.com/atar-axis/xpadneo", str(DRIVER_DIR)],
        "category": "hardware",
        "use_pkexec": False,
        "description": "Fetches the driver source. No root needed for this step.",
    },
    {
        "id": "xbox_elite2_install",
        "name": "Build & Install Xbox Elite 2 Driver",
        "cmd": ["bash", "-c", f"cd {DRIVER_DIR} && make dkms-install"],
        "category": "hardware",
        "use_pkexec": True,
        "description": "Builds the kernel module via DKMS and installs it. Requires the source to be cloned first.",
    },
]
```

Note the top-level `from pathlib import Path`, completely ordinary and not flagged by the safety
scanner (only the specific blocked call *patterns* listed under
Safety are, like `__import__()` used as a call, a normal
`import` statement is untouched).

If a user re-runs the clone action against a directory that already exists, plain `git clone` will
fail (it refuses to clone into a non-empty directory), decide whether that's the behavior you want,
or use `git -C <dir> pull` as a second check/action for "update existing source" rather than
re-cloning.

---

## Testing your plugin locally

1. Drop your `.py` file into `~/.local/share/griffinhub/plugins/`.
2. Open Griffin Hub → **Plugins** → **Reload Plugins**.
3. Check the "Loaded Plugins" list at the top of the page, your plugin should appear with its
   version, trust level, and author.
4. If it doesn't appear, check the warnings box on the same page, load errors (AST scanner
   violations, missing required keys, bad category values, sandbox failures) are surfaced there with
   enough detail to fix them.
5. Use the **Run Check**/**Run Action** buttons directly on the page to verify behavior before
   sharing the plugin with anyone else.

You can also load a single plugin file ad hoc without restarting the whole app by using the Plugins
page's Reload button after editing, Griffin Hub re-scans the whole plugins folder each time.

---

## Publishing to the community store

The community store is a GitHub-backed index of installable plugins. To publish:

1. Host your plugin file(s) somewhere the store can serve them from (a GitHub repo, referenced via
   jsDelivr CDN, is the standard approach).
2. Add an entry to the store's `index.json`:

```json
{
  "schema_version": 1,
  "updated": "2026-07-04T00:00:00Z",
  "plugins": [
    {
      "id": "rtl8821au-fix",
      "name": "RTL8821AU WiFi Fix",
      "version": "1.0.0",
      "api_version": 1,
      "author": "Your Name",
      "description": "Detects RTL8821AU adapters and installs the DKMS driver.",
      "capabilities": ["wifi_fix"],
      "verified": false,
      "url": "https://cdn.jsdelivr.net/gh/you/your-repo@main/plugins/rtl8821au-fix/",
      "files": ["plugin.py"],
      "checksum": "sha256:<hex of plugin.py>"
    }
  ]
}
```

3. Users can then find it via `griffinhub store search` or the Plugins page, and install it with
   `griffinhub plugin install rtl8821au-fix`.

The `verified` flag is set by whoever maintains the index (not something you can self-declare) and
controls whether the plugin respects the separate "sandbox verified plugins" setting — verified
plugins are *not* sandboxed by default, on the theory that a maintained index has already reviewed
them. Setting `verified: true` on your own submission without going through review is a trust
violation, not just a technicality — don't do it.

Store installs always go through the same AST safety scanner and checksum verification as any other
plugin, verified or not.

---

## API versioning

The current Griffin Hub plugin API is **version 1**. Declare it explicitly:

```python
PLUGIN_METADATA = {"api_version": 1, ...}
```

If Griffin Hub's `PLUGIN_API_VERSION` is ever bumped for a breaking change, plugins declaring a newer
`api_version` than the running build supports will load with a warning rather than being silently
broken — check the Plugins page's warnings box if something seems off after a Griffin Hub update.

---

## Reference: allowed executables for checks/actions

Checks and actions **without** `use_pkexec` are restricted to this allowlist (actions with
`use_pkexec: True` are gated by `pkexec`'s own authentication instead, and aren't limited to this
list, but see the note in the WiFi example above about keeping root actions scoped and honest):

```
python3, python, which, ldconfig, dpkg, rpm, pacman, vulkaninfo, glxinfo, strings,
gamemoded, pipewire, flatpak, systemctl, sysctl, cpupower, lscpu, lspci, lsusb,
nvidia-smi, radeontop, upower, xrandr, udevadm, git
```

Note there's no shell (`sh`/`bash`) or `grep` on this list, which means a plain check can't do
"run this command and grep the output", it can only test a command's exit code directly. If you
need pattern-matching over hardware output, prefer testing for something with a clean exit-code
contract instead (`dpkg -s <package>` to check whether a fix is already installed, `which <tool>`
to check whether a tool is present), the way the examples above do. Genuine shell one-liners belong
in an **action** with `use_pkexec: True`, which isn't limited to this list, see the DKMS install
action in the WiFi example above.
