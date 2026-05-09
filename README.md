# WinBridge

> **NOTE:** This is a preview of a planned release. It describes what WinBridge is intended to do and how it is designed to work. The project is currently in active development and not all described functionality may be fully implemented yet.

WinBridge is a modern, profile-based Wine orchestration platform built with PyQt6. It provides a polished graphical interface and a full-featured command-line interface for installing, configuring, and launching Windows applications on Linux, with first-class support for gaming, modding tools, productivity software, creative suites, and legacy applications.

WinBridge is part of the **Griffin Linux Project**, a collection of tools designed to improve the Linux desktop experience for gamers and power users.

---

## Features

- **Profile-based installation:** Choose from built-in profiles for gaming, modding, launchers, office, creative, streaming, legacy, and development software. Each profile pre-selects the correct Wine runner, winetricks modules, environment variables, and post-install checks for its use case.
- **Graphical user interface:** A dark-themed PyQt6 UI with animated transitions, a card-based profile browser, tabbed settings, an integrated log viewer, and real-time install progress.
- **Full CLI:** Every GUI action is also available from the command line, making WinBridge scriptable and usable on headless or display-less systems.
- **Multiple Wine runners:** Supports Proton GE, Wine GE, Wine Staging, Wine Stable, and Wine Development. Runners are detected from the system or can be managed through ProtonUp-Qt.
- **Automatic module resolution:** Selects winetricks verbs, environment variable injections, and prefix architecture settings based on chosen modules. Detects and resolves conflicts (e.g., DXVK vs. WineD3D) and pulls in dependencies (e.g., DXVK requiring VC Runtime 2022) automatically.
- **Wine prefix snapshots:** Create, restore, and manage named snapshots of any prefix, allowing safe experimentation without losing a working configuration.
- **Mod manager support:** Dedicated profiles for Vortex and Mod Organizer 2, including automated download and setup of the MO2 Linux installer, NXM link registration, symlink/hardlink verification, and WebView2 validation.
- **Plugin system:** Extend WinBridge with community or custom plugins. Plugins can add profiles, modules, runners, system checks, and distro-specific overrides. A two-tier trust model separates safe declarative plugins from fully trusted in-process plugins.
- **Community plugin store:** Browse, install, update, and remove plugins from the community store directly from the GUI or CLI.
- **System diagnostics:** Built-in system check panel verifies Wine, Vulkan, 32-bit support, GameMode, MangoHud, Mesa, PipeWire, Flatpak, and more.
- **Subprocess safety:** All worker processes are guarded by a dual-mechanism watchdog timeout to prevent silent hangs.
- **Tarfile safety:** Archive extraction uses `filter='data'` on Python 3.12 and path-traversal guards on older versions.
- **SteamOS / Steam Deck support:** Auto-downloads winetricks on demand when running on an immutable root filesystem.

---

## Part of the Griffin Linux Project

WinBridge is developed as part of a broader suite of Linux tools. It includes settings to control Process Sentry and Kernel Autotune V2 directly from the WinBridge interface, making it a central configuration point for the Griffin Linux toolchain.

- **Process Sentry** - [https://github.com/bobbycomet/Process-Sentry](https://github.com/bobbycomet/Process-Sentry)
- **Kernel Autotune V2** - [https://github.com/bobbycomet/kernel-autotune-V2](https://github.com/bobbycomet/kernel-autotune-V2)
- **WinBridge Plugins (Community Store)** - [https://github.com/bobbycomet/WinBridge-Plugins](https://github.com/bobbycomet/WinBridge-Plugins)

---

## Requirements

**Runtime:**
- Python 3.10 or newer (Python 3.12 recommended for full tarfile safety support)
- PyQt6
- Wine (any variant: stable, staging, development, GE, or Proton GE)
- winetricks (auto-downloaded to `~/.local/share/winbridge/bin/winetricks` if not found; required for module installation)

**Optional but recommended:**
- `bubblewrap` (`bwrap`): enables full sandbox isolation for community plugins. Falls back to a minimal subprocess environment if not present.
- `vulkaninfo`: required for the Vulkan system check.
- `gamemoded`: required for GameMode support.
- MangoHud: required for the MangoHud overlay module.
- `glxinfo` (from `mesa-utils`): required for the Mesa driver check.
- PipeWire: required for the audio check.
- Flatpak and ProtonUp-Qt: optional, used for runner management suggestions.

---

## Launching WinBridge

**Graphical interface:**

```bash
python3 winbridge.py
```

**Command-line (no display required):**

```bash
python3 winbridge.py --help
```

---

## Profiles

Profiles are the central concept in WinBridge. Each profile bundles a recommended Wine runner, a set of modules, environment variable overrides, lifecycle hooks, and post-install diagnostic checks tailored for a category of Windows software.

### Built-in Profiles

| Profile | Runner | Use Case |
|---|---|---|
| Gaming | Proton GE | Windows games with DXVK, VKD3D, Esync, Fsync, GameMode |
| Launcher | Wine GE | Game launchers: Battle.net, Epic Games, Riot Client |
| Office / Productivity | Wine Stable | Business software, accounting tools, corporate apps |
| Adobe / Creative | Wine Staging | Photoshop, Illustrator, Premiere, and similar tools |
| Streaming / Content | Wine Staging | Streamlabs, chat bots, overlay software |
| Legacy Software | Wine Stable | Windows XP/7 era software, 32-bit apps, old games |
| Dev Tools | Wine Staging | Visual Studio, Windows SDKs, .NET toolchains |
| Modding Tools | Wine GE | Mod managers, map editors, save editors |
| Vortex | Wine GE | Nexus Mods Vortex with NXM links and collections |
| Mod Organizer 2 | Wine GE | MO2 with NXM links, virtual filesystem, game detection |

### Profile Metadata Fields (v1.1 and later)

Each profile supports the following optional fields beyond the core `id`, `name`, `runner`, and `modules`:

- `version`: semver string for the profile definition itself.
- `profile_type`: one of `app`, `mod_manager`, `launcher`, `tool`, or `legacy`. Used for UI grouping and install logic.
- `capabilities`: list of feature strings such as `nxm`, `mod_deploy`, `virtual_fs`, `game_detection`, `collections`, `overlay`, and `modding_tool`.
- `setup_steps`: ordered list of install phase names executed by the install worker.
- `post_install_steps`: phases run after the main installer completes.
- `post_install_checks`: profile-specific diagnostic checks with fix hints, run after install.
- `external_resources`: declarative list of files the installer must fetch, replacing hardcoded URLs in worker code.

### Profile Dependency and Conflict Fields (v1.6 and later)

- `depends_on`: list of profile IDs that must be installed before this profile.
- `conflicts_with`: list of profile IDs that cannot coexist in the same prefix.
- `permission_hints`: human-readable list of access requirements shown to the user before install, such as `"Downloads folder"`, `"network access"`, or `"NXM URL scheme"`.

---

## Modules

Modules represent optional Wine components, runtimes, overlays, and environment tweaks. When you install a profile, WinBridge resolves the selected modules into winetricks verbs and environment variable injections, applies conflict rules, and pulls in declared dependencies.

### Module Categories

**Graphics:** DXVK, VKD3D, WineD3D, Legacy DirectX

**Performance:** Esync, Fsync, GameMode, MangoHud

**Runtime:** VC Runtime 2022, VC Runtime Full, .NET 4.8, .NET Stack, .NET Full Stack, .NET Heavy Stack, .NET + VC Runtime

**Fonts:** Core Fonts, Windows Fonts

**Media:** Media Foundation, Quartz/DirectShow

**Compatibility:** NTLM Auth, WebView2, DPI Scaling, 32-bit Libraries

**Creative:** Color Profile Fixes, GPU Acceleration, GDI Tweaks, Old API Compat

**Streaming:** Hardware Encoding, Audio Routing, Low-Latency Timers

**Legacy:** Old VC Runtime, Safe DLL Overrides

**Development:** PowerShell, Windows SDK, Debugging Support, Path Handling

**Modding:** Mixed DirectX, File Permission Fixes, Explorer Integration

**Launchers:** Edge/IE Components

**Modding Tools:** Vortex Runtimes

### Module Conflict and Dependency Rules

WinBridge enforces the following conflict rules at install time and as live warnings in the module selection UI:

- DXVK conflicts with WineD3D.
- WineD3D conflicts with DXVK and VKD3D.
- VKD3D conflicts with WineD3D.
- 32-bit Libraries conflicts with .NET Heavy Stack and .NET Full Stack.

The following dependency rules cause automatic module inclusion:

- DXVK requires VC Runtime 2022.
- VKD3D requires VC Runtime 2022.
- .NET Heavy Stack requires .NET Full Stack.
- .NET Full Stack requires .NET Stack.
- .NET Stack requires .NET 4.8.
- WebView2 requires VC Runtime 2022.

Conflict resolution is iterative and fully transitive: if module A conflicts with B and B conflicts with C, all three levels are resolved in a single pass. The first-listed module wins on conflict. User-requested modules are never displaced by their own auto-added dependencies.

---

## Runners

WinBridge supports the following Wine runners:

| Runner ID | Name | Type | Notes |
|---|---|---|---|
| `proton-ge` | Proton GE | Gaming/Recommended | Recommended for most games |
| `wine-ge` | Wine GE | General/Recommended | Recommended for launchers and modding |
| `wine-staging` | Wine Staging | General | Good for creative and streaming software |
| `wine-stable` | Wine Stable | Stable/Legacy | Best compatibility for legacy apps |
| `wine-devel` | Wine Development | Testing | Latest features, less stable |

System runners are detected from `PATH`. Custom or downloaded runners (such as those managed by ProtonUp-Qt) are supported as well. Runner IDs use lowercase-with-hyphens internally; display names are only used in the UI.

---

## Wine Prefixes and Snapshots

Each installed profile gets its own isolated Wine prefix under `~/.local/share/winbridge/prefixes/`. Prefixes are never shared between profiles by default.

WinBridge supports named snapshots of any prefix. You can:

- Create a snapshot before making risky changes.
- Restore a prefix to any previous snapshot.
- Delete snapshots you no longer need.
- List all snapshots for a prefix.

Snapshots are stored as compressed tarballs. Extraction uses `safe_tarextract()`, which applies `filter='data'` on Python 3.12 and path-traversal validation on older Pythons.

---

## NXM Link Handling

Profiles with the `nxm` capability (currently Vortex and Mod Organizer 2) can register an `nxm://` URI handler on your Linux desktop. This allows clicking mod download links on Nexus Mods in a browser and having them sent directly to the correct mod manager running under Wine.

Registration writes:
- A `.desktop` file to `~/.local/share/applications/winbridge-nxm.desktop`
- A handler shell script to `~/.local/share/winbridge/nxm-handler.sh`

The handler can be re-registered or removed from the profile's post-install options.

---

## Plugin System

WinBridge has a first-class plugin system. Plugins are single `.py` files placed in `~/.local/share/winbridge/plugins/`. They are loaded automatically at startup.

### What Plugins Can Do

A plugin may define any combination of the following module-level variables:

- `PLUGIN_PROFILES`: list of profile dicts following the same schema as built-in profiles. Appears as selectable profiles in the Install App screen.
- `PLUGIN_MODULES`: dict mapping category names to lists of module dicts.
- `PLUGIN_RUNNERS`: list of runner dicts.
- `PLUGIN_CHECKS`: list of system check dicts.
- `PLUGIN_EXTENDS`: dict mapping existing profile IDs to patch dicts. Extends a built-in or other plugin's profile rather than replacing it. See [Profile Extension API](#profile-extension-api).
- `PLUGIN_METADATA`: dict with `version`, `api_version`, `author`, `description`, and `trust_level`.
- `PLUGIN_KIND`: string indicating the plugin type: `"profile"`, `"app"`, `"distro"`, `"check"`, or `"runner"`.
- `PLUGIN_DISTRO_OVERRIDES`: used by distro plugins to override package manager commands, system check fix commands, and DE/compositor detection.
- `register(registry)`: called at startup for trusted plugins only. The registry exposes `.add_profile()`, `.extend_profile()`, `.add_module_category()`, `.add_runner()`, and `.add_check()`.

### AST Safety Scanner

Before any plugin is executed, WinBridge runs an AST-based safety scanner that blocks plugins containing:

- `os.system()` calls
- `eval()` or `exec()` calls
- `subprocess` calls with `shell=True`
- `shutil.rmtree()` calls
- Dangerous shell command literals such as `rm -rf`

This scanner runs regardless of trust level. It is a first line of defense, not a complete sandbox.

> **Warning:** Plugins execute arbitrary Python code. Only install plugins you trust. The AST scanner catches common dangerous patterns but cannot catch all possible harmful code.

---

## Plugin Trust Tiers

WinBridge uses a two-tier trust model for plugins, declared in `PLUGIN_METADATA["trust_level"]`.

### Safe (default)

- Declarative-only: `PLUGIN_PROFILES`, `PLUGIN_MODULES`, `PLUGIN_RUNNERS`, `PLUGIN_CHECKS`.
- No `register()` hook.
- No `env_overrides` in `PLUGIN_EXTENDS`.
- All community store plugins are forced to this level.
- Any plugin without a `.trusted` sidecar file is capped at this level.

### Trusted

- Full API access: `register()` hook, `env_overrides` injection, and full `PLUGIN_EXTENDS`.
- Requires a `.trusted` sidecar file placed next to the plugin `.py` file.
- The `.trusted` file **must be created manually by the user.** It cannot be self-granted by the plugin.
- `trust_level = "trusted"` in `PLUGIN_METADATA` is only respected when the `.trusted` sidecar is present.

### Mapping to Execution Contexts

| Context | Trust Level |
|---|---|
| Community store / sandboxed | Forced to `safe` |
| In-process, no `.trusted` sidecar | Capped at `safe` |
| In-process with `.trusted` sidecar | `trusted` respected |

---

## Profile Extension API

Available in Plugin API v2 and later. Plugins can extend existing profiles without replacing them by declaring `PLUGIN_EXTENDS`.

```python
PLUGIN_EXTENDS = {
    "steam_deck": {
        # All fields are optional; omitted fields leave the profile unchanged.
        "modules":             ["ExtraModule"],    # dedupe-append
        "post_install_steps":  ["extra_step"],     # dedupe-append
        "post_install_checks": [                   # dedupe by check id
            {"id": "my_check", "name": "...", "cmd": [...], "fix_hint": "..."}
        ],
        "env_overrides": {                         # trusted plugins only
            "MY_VAR": "value"
        },
        "performance_mode": True,                  # OR'd with existing value
    }
}
```

### Merge Rules

These rules are deterministic and designed to avoid surprises:

- `modules`: deduplicated union; plugin entries are appended after the base profile's entries.
- `post_install_steps`: deduplicated union; plugin entries are appended after the base.
- `post_install_checks`: deduplicated by the `id` field; plugin entries are appended.
- `env_overrides`: dict merge; plugin keys win on collision. Restricted to trusted plugins.
- `performance_mode`: boolean OR; the result is `True` if either the base profile or the patch sets it.
- Any unrecognized key in the patch dict is warned about and skipped, ensuring forward compatibility.

---

## Sandbox System

WinBridge uses three distinct execution contexts for running code.

### SANDBOXED

Used for community plugins and store downloads. Plugins are evaluated via bubblewrap (`bwrap`) if installed, or via a minimal subprocess with `-S` (no site-packages) and a stripped environment if bubblewrap is not available. No home directory access. No network access. Output is declarative-only JSON. The `register()` hook is never called in this context. Controlled by the `plugin_sandbox` setting.

### CONTROLLED

Used for Wine/winetricks invocations, CLI system checks, and post-install checks. Runs as a plain subprocess restricted to an allowlist of permitted executables (`ALLOWED_CHECK_EXECUTABLES`).

### NATIVE

Used for prefix creation, mod symlinking, NXM handler registration, and snapshots. Has full user filesystem access, which is intentional Wine behavior. The `plugin_sandbox` setting has **no effect** on NATIVE-context paths.

### Notes

- If `plugin_sandbox` is enabled but `bwrap` is not installed, WinBridge will warn you and fall back to the minimal subprocess approach.
- The `.trusted` sidecar bypasses the sandbox for in-process plugin loading, granting full registry access.

---

## Community Plugin Store

WinBridge includes a built-in community plugin store backed by the [WinBridge-Plugins](https://github.com/bobbycomet/WinBridge-Plugins) repository, served via the jsDelivr CDN for fast, globally cached downloads.

The store index is cached locally for one hour (`STORE_CACHE_TTL = 3600` seconds). You can force a cache refresh from the GUI or CLI.

### Store CLI Commands

```bash
# Search the store
python3 winbridge.py store search
python3 winbridge.py store search --query modding
python3 winbridge.py store search --capability nxm
python3 winbridge.py store search --verified

# Refresh the store cache
python3 winbridge.py store refresh

# Install a plugin
python3 winbridge.py plugin install <plugin-id>

# Update a plugin
python3 winbridge.py plugin update <plugin-id>

# Remove a plugin
python3 winbridge.py plugin remove <plugin-id>

# Show plugin info (remote and local)
python3 winbridge.py plugin info <plugin-id>

# List installed plugins
python3 winbridge.py plugin list

# Verify installed plugins
python3 winbridge.py plugin verify

# Reload all plugins without restarting
python3 winbridge.py plugin reload
```

---

## System Integration

WinBridge provides a `SystemIntegration` helper for opening files and URLs that respects the user's preferred file manager and desktop environment. The resolution order is:

1. `settings["file_manager"]`: user override.
2. DE-specific fallbacks: Nautilus (GNOME), Dolphin (KDE), Thunar (XFCE), and others.
3. `xdg-open`: universal fallback.

Use the "Open Log Folder" button under Settings to open the log directory directly in your file manager.

---

## Structured Logging

All WinBridge events are logged to `~/.local/share/winbridge/logs/winbridge.log`.

Worker output lines are prefixed with `[STEP]`, `[OK]`, or `[FAIL]` so the in-app log panel and failure dialogs can parse and display them consistently. Fatal install failures emit a user-facing bullet-point summary via `finished(False, summary)`.

You can open the log folder directly from: **Settings** > **Paths** > **Open Log Folder**

---

## Command-Line Interface

WinBridge has a full CLI that does not require a display. All commands follow the pattern:

```
python3 winbridge.py <command> [options]
```

### Core Commands

```bash
# Show help
python3 winbridge.py --help

# Install a Windows application using a profile
python3 winbridge.py install setup.exe --profile gaming
python3 winbridge.py install setup.exe --profile mo2

# List all installed apps
python3 winbridge.py list

# Run the system compatibility check
python3 winbridge.py syscheck

# Check whether specific modules are available
python3 winbridge.py check-modules DXVK WineD3D

# Launch an installed app
python3 winbridge.py launch <app-id>

# Remove an installed app
python3 winbridge.py remove <app-id>
```

### Snapshot Commands

```bash
python3 winbridge.py snapshot create <prefix-id> <snapshot-name>
python3 winbridge.py snapshot restore <prefix-id> <snapshot-name>
python3 winbridge.py snapshot list <prefix-id>
python3 winbridge.py snapshot delete <prefix-id> <snapshot-name>
```

### Settings Commands

```bash
# List all settings
python3 winbridge.py settings list

# Get a setting value
python3 winbridge.py settings get plugin_sandbox

# Set a setting value
python3 winbridge.py settings set plugin_sandbox true
python3 winbridge.py settings set file_manager dolphin
```

### Plugin Commands

```bash
python3 winbridge.py plugin list
python3 winbridge.py plugin install <id>
python3 winbridge.py plugin update <id>
python3 winbridge.py plugin remove <id>
python3 winbridge.py plugin info <id>
python3 winbridge.py plugin verify
python3 winbridge.py plugin reload
```

### Store Commands

```bash
python3 winbridge.py store search
python3 winbridge.py store search --query <term>
python3 winbridge.py store search --capability <cap>
python3 winbridge.py store refresh
```

---

## Settings

WinBridge stores its settings in `~/.local/share/winbridge/settings.json`. Key settings include:

| Key | Type | Description |
|---|---|---|
| `plugin_sandbox` | bool | Enable bubblewrap/subprocess sandboxing for community plugins |
| `file_manager` | string | Override the default file manager used to open folders |
| `store_url` | string | Override the community store index URL |
| `wine_debug` | string | `WINEDEBUG` value passed to Wine processes |
| `enable_esync` | bool | Global Esync toggle (per-profile settings take precedence) |
| `enable_fsync` | bool | Global Fsync toggle |

You can view and modify all settings from the GUI (Settings tab) or via `winbridge settings list` and `winbridge settings set`.

---

## Data Locations

| Path | Contents |
|---|---|
| `~/.local/share/winbridge/` | Root data directory |
| `~/.local/share/winbridge/prefixes/` | Wine prefixes, one directory per installed app |
| `~/.local/share/winbridge/runners/` | Downloaded runners |
| `~/.local/share/winbridge/cache/` | Store index cache and other cached downloads |
| `~/.local/share/winbridge/plugins/` | Installed plugins (`.py` files and optional `.trusted` sidecars) |
| `~/.local/share/winbridge/logs/winbridge.log` | Application log |
| `~/.local/share/winbridge/apps.json` | Installed app records |
| `~/.local/share/winbridge/runners.json` | Runner state |
| `~/.local/share/winbridge/settings.json` | User settings |
| `~/.local/share/winbridge/bin/winetricks` | Auto-downloaded winetricks (used on SteamOS and other immutable systems) |
| `~/.local/share/applications/winbridge-nxm.desktop` | NXM link handler desktop entry |
| `~/.local/share/winbridge/nxm-handler.sh` | NXM handler script |

---

## System Checks

WinBridge includes a built-in system diagnostics panel. The following checks are performed:

| Check | Category | Required |
|---|---|---|
| Wine | Core | Yes |
| Winetricks | Core | Yes |
| 32-bit Support | Core | Yes |
| Vulkan Loader | Graphics | Yes (for DXVK/VKD3D) |
| Mesa Drivers | Graphics | Yes |
| GameMode | Performance | Optional |
| MangoHud | Performance | Optional |
| PipeWire Audio | Audio | Optional |
| ProtonUp-Qt | Runners | Optional |
| Flatpak | Core | Optional |

Profile-specific post-install checks (such as the Vortex WebView2 check and filesystem symlink test) are run automatically after install and are also available on demand from the diagnostics panel.

---

## Security

WinBridge takes several measures to reduce risk when running Windows software and third-party plugins:

- **AST scanner:** Blocks plugins containing dangerous Python patterns before execution.
- **Trust tier system:** Limits what community and untrusted plugins can do. `env_overrides` and the `register()` hook are restricted to manually trusted plugins.
- **Sandbox system:** Community plugins run in a bubblewrap container (if available) with no home directory or network access.
- **Subprocess allowlist:** CONTROLLED-context subprocesses are restricted to a whitelist of permitted executables.
- **Tarfile safety:** Archive extraction is protected against path traversal attacks.
- **Watchdog timeouts:** All worker subprocesses have a dual-mechanism timeout (watchdog thread and per-line deadline check) to prevent silent hangs.
- **No self-granting of trust:** The `.trusted` sidecar file must be created by the user manually. Plugins cannot grant themselves elevated trust.

Wine prefixes run with the current user's permissions. WinBridge never requires or requests elevated privileges. Windows software running under Wine has access to whatever your user account has access to; exercise the same caution you would with any application.

---

## Troubleshooting

**WinBridge fails to start with a PyQt6 import error:**
Install PyQt6 with `pip install PyQt6`. On some distributions, `python3-pyqt6` is available as a system package.

**Winetricks module installation fails:**
WinBridge will attempt to auto-download winetricks if it is not found in `PATH`. If the download fails, install winetricks manually from your package manager or from [https://github.com/Winetricks/winetricks](https://github.com/Winetricks/winetricks).

**`plugin_sandbox` enabled but bubblewrap not found:**
WinBridge will warn you and fall back to a minimal subprocess sandbox. Install bubblewrap for full isolation: `sudo apt install bubblewrap` (or your distribution's equivalent).

**NXM links not being captured by the browser:**
Re-run the NXM handler registration from the Vortex or MO2 profile's post-install actions. Make sure `~/.local/share/applications/winbridge-nxm.desktop` exists and that your browser is configured to handle `nxm://` links via the desktop file.

**Wine process hangs during install:**
The install worker has a watchdog timeout. If a process is silently stalled, it will be killed once the wall-clock deadline is reached. Check the log file at `~/.local/share/winbridge/logs/winbridge.log` for `[FAIL]` prefixed lines and the failure summary.

**A plugin is blocked by the AST scanner:**
The scanner detected a dangerous code pattern in the plugin. Review the plugin source before adding an exception. If you trust the plugin author and understand the flagged code, you can load the plugin as a trusted plugin by creating a `.trusted` sidecar file.

**Unknown setting key error in CLI:**
Run `python3 winbridge.py settings list` to see all valid keys and their current values.

---

## Contributing

WinBridge is in active development and contributions are welcome. When submitting plugins or profiles to the community store, please refer to the plugin schema documented in the [WinBridge Plugins](https://github.com/bobbycomet/WinBridge-Plugins) repository.

Plugin naming conventions:
- Module IDs: lowercase, e.g., `dxvk`, `wined3d`, `vcrun2022`
- Profile IDs: lowercase-with-hyphens, e.g., `wine-ge`, `mod-organizer2`
- Display names are only used in UI code; core logic always uses IDs

When writing plugins, declare `PLUGIN_METADATA` with `api_version`, `version`, `author`, `description`, and `trust_level`. This information is displayed in the store and helps users make informed decisions.

---

## Related Projects

| Project | Description | Link |
|---|---|---|
| Process Sentry | Process monitoring and management for Linux; controllable via WinBridge settings | [GitHub](https://github.com/bobbycomet/Process-Sentry) |
| Kernel Autotune V2 | Automated Linux kernel parameter tuning; controllable via WinBridge settings | [GitHub](https://github.com/bobbycomet/kernel-autotune-V2) |
| WinBridge Plugins | Community plugin store for WinBridge | [GitHub](https://github.com/bobbycomet/WinBridge-Plugins) |

---

*WinBridge v1.6.0 - Preview Release - Griffin Linux Project*
