# WinBridge

> **NOTE:** This is a preview of a planned release. It describes what WinBridge is intended to do and how it is designed to work. The project is currently in active development, and not all described functionality may be fully implemented yet.

**A Linux desktop app that runs your Windows software without making you think about Wine.**

WinBridge is for people switching from Windows to Linux who don't want to become a Wine expert just to keep using the apps and games they already have. You point it at an `.exe`, tell it roughly what kind of program it is, and it handles the rest: creating an isolated Wine environment, installing the right runtime libraries, picking a compatible Wine/Proton build, and giving you a normal-looking "app" you can launch, update, and remove like anything else.

---

## Why this exists

If you've moved from Windows to Linux, or you're thinking about it, there's a specific kind of friction that has nothing to do with Linux being "hard." It's that the software you already own and already know how to use doesn't run natively. Wine and Proton solve the *technical* problem (translating Windows API calls so the program runs at all), but they don't solve the *human* problem: knowing which Wine build to use, which runtime DLLs a given program needs, how to keep one game's weird dependency soup from breaking a totally unrelated app, or what to do when a launcher silently fails because WebView2 isn't installed.

That gap is normally filled by experience — the kind you build up over years of fighting with `winecfg` and reading forum threads from 2014. WinBridge's whole reason for existing is to package that experience into software, so a first-week Linux user and a ten-year Wine veteran get roughly the same result.

Concretely, WinBridge was built around a few convictions:

- **Isolation should be the default, not a power-user feature.** Every app gets its own Wine prefix. A broken game shouldn't be able to take your office suite down with it.
- **You shouldn't have to know what DXVK or VKD3D are.** You should be able to pick "Gaming" and get the modules that make Windows games run well on Vulkan, without first learning what Vulkan translation even means.
- **The thing that breaks on a Windows switcher's first day, drivers, controllers, Wi-Fi chips with notoriously bad Linux support should have a dedicated, guided fix, not a Stack Overflow scavenger hunt.** That's what the Hardware Hub is for.
- **Power users shouldn't be boxed in.** Everything the GUI does, the CLI can do too, and everything the built-in profile system does, a plugin can extend.

WinBridge doesn't try to replace Lutris, Bottles, or PlayOnLinux; it overlaps with all of them in places. Its specific angle is being opinionated about *defaults* for someone who just switched platforms, while staying scriptable and extensible for everyone else.

---

## What WinBridge actually does

In one sentence: **WinBridge turns "install this Windows program" into a guided, profile-driven workflow on top of Wine and Proton**, instead of a sequence of manual `winecfg`/`winetricks` steps you have to already know.

When you install an app through WinBridge, here's what happens under the hood:

1. **A profile is chosen** (by you, or WinBridge's best guess), e.g., *Gaming*, *Adobe/Creative*,*Office/Productivity*. Each profile bundles a sensible Wine/Proton build and a starting set of compatibility modules.
2. **A fresh, isolated Wine prefix is created** under `~/.local/share/winbridge/prefixes/<app-id>/`. Nothing is shared with other apps by default.
3. **Modules are installed via winetricks** — DXVK, VKD3D, .NET, VC++ runtimes, fonts, WebView2, NTLM auth, whatever the profile calls for, with conflict resolution (e.g., WineD3D and DXVK can't both be active; WinBridge resolves that automatically rather than silently breaking).
4. **The installer runs inside that prefix**, same as it would on real Windows.
5. **Profile-specific post-install steps run**, where applicable — registering an NXM URL handler for Vortex, verifying WebView2 actually took for a launcher, and smoke-testing that the binary launches at all.
6. **The app shows up in My Apps**, launchable with one click, with its own environment variables, optional launch wrapper (`gamemoderun`, `gamescope`, etc.), and snapshot history.

Everything past step 1 can also be done from the terminal.

---

## Quick start

**GUI:**
1. Open WinBridge → **Install App**.
2. Point it at your `.exe`/`.msi`.
3. Pick a profile (or let WinBridge suggest one).
4. Wait for the prefix to set up and the installer to run.
5. Find it under **My Apps** from then on.

**CLI:**
```
winbridge install ~/Downloads/setup.exe --profile gaming --name "My Game"
winbridge list
winbridge launch "My Game"
```

---

## Installing

On first GUI launch, WinBridge will ask for admin authentication once to install a small system helper; this is what lets later features, such as CPU governor changes and driver installs, ask for permission cleanly instead of every single click prompting you separately. You can decline; everything still works, it just falls back to per-action prompts.

**Requirements on the host system**, regardless of install method:
- A 64-bit Linux distribution with glibc (the AppImage bundles its own Python + PyQt6, but still relies on the host's graphics stack, Wine packages, and system libraries it shells out to).
- [`wine`](https://www.winehq.org/) and [`winetricks`](https://github.com/Winetricks/winetricks) installed, or the willingness to let WinBridge's System Check page tell you what's missing.
- A Vulkan driver if you want DXVK/VKD3D-based profiles (Gaming, Steam Deck Gaming) to actually perform well; WinBridge will tell you if it can't find one.
- `pkexec`/PolicyKit for anything that needs elevated privileges (driver installs, CPU governor changes, system tuning). Present by default on virtually every desktop Linux distro.

---

## The app, tab by tab

WinBridge's sidebar has nine sections:

| Page | What it's for |
|---|---|
| **Dashboard** | At-a-glance stats, installed apps, runners, quick links to the other pages. |
| **Profiles** | Browse every available profile (built-in + plugin-added), see what runner and modules each one uses. |
| **Install App** | The main install wizard: pick an `.exe`, pick a profile, go. |
| **My Apps** | Everything you've installed: launch, edit environment variables/launch wrapper, manage snapshots, uninstall. |
| **Runners** | Install/remove Wine and Proton GE builds. WinBridge can also detect runners you already installed via Steam or ProtonUp-Qt without re-downloading them. |
| **System Check** | Verifies Wine, Winetricks, Vulkan, GameMode, MangoHud, Mesa, PipeWire, and 32-bit support are present, with fix hints for anything missing. |
| **System Tune** | Used to be GameTune Hub. Steam launch wrapper management, ProcessSentry, kernel autotune, live tuning, setup. See its own section below. |
| **Hardware Hub** | Used to be Gruffun Hub. Guided fixes for CPU governor, GPU drivers, controller drivers (wireless, SIM wheels, and many more), and notoriously bad-on-Linux Wi-Fi chipsets (Realtek and Broadcom). See its own section below. |
| **Settings** | General preferences, custom data paths, plugin management, security (sandbox toggle), and About. |

---

## Profiles, modules, and runners

These three concepts are the backbone of how WinBridge decides what to install and how to run it.

### Profiles

A **profile** is a named bundle of "what kind of software is this, and what does it need." WinBridge ships ten:

> **NOTE:** SteamDeck gaming will only work if you have your device ready to use AppImages with read/write capabilities.

| Profile | Runner | What it's for |
|---|---|---|
| **Gaming** | Proton GE | Windows games. DXVK, VKD3D, Esync/Fsync, Media Foundation, Core Fonts, VC++ 2022. |
| **Launcher** | Wine GE | Battle.net, Epic Games, Riot Client, and similar; heavy on .NET, WebView2, and auth components. |
| **Office/Productivity** | Wine Stable | Business software, accounting tools, corporate apps. |
| **Adobe/Creative** | Wine Staging | Photoshop, Illustrator, Premiere color profile fixes, old-API compatibility. |
| **Streaming/Content** | Wine Staging | Streamlabs, chatbots, overlay tools, audio routing. |
| **Legacy Software** | Wine Stable | XP/7-era software, old games, 32-bit-only tools. |
| **Dev Tools** | Wine Staging | Visual Studio, Windows SDKs, enterprise dev tooling. |
| **Modding Tools** | Wine GE | General-purpose mod managers, map editors, save editors. |
| **Vortex** | Wine GE | Nexus Mods Vortex specifically — NXM link handling, collections, mod deployment. |
| **Mod Organizer 2** | Wine GE | MO2 specifically — downloads and runs the official Linux MO2 installer for you. |
| **Steam Deck Gaming** | Proton GE | GOG Galaxy, EA App, Ubisoft Connect, Xbox PC App, and other launchers Steam doesn't manage. *Adaptive* — probes your GPU vendor, Mesa version, and whether Gamescope is active, then only applies the environment variables that are safe for your actual hardware. |

Every profile that needs more than "install some modules and run the .exe" (currently Vortex, MO2, and Steam Deck Gaming) declares structured **lifecycle hooks** — `setup_steps`, `post_install_steps`, and `post_install_checks` — so the install worker knows exactly what extra work to do and what to verify afterward. Plugins can add entirely new profiles with the same structure.

### Modules

A **module** is one compatibility component, a Winetricks verb, more or less. They're grouped by category:

- **Graphics:** DXVK, VKD3D, WineD3D (conflicts with DXVK), Legacy DirectX
- **Performance:** Esync, Fsync, GameMode, MangoHud
- **Runtime:** VC Runtime 2022, VC Runtime Full, .NET 4.8, .NET Full Stack
- **Fonts:** Core Fonts, Windows Fonts
- **Media:** Media Foundation, Quartz/DirectShow
- **Compatibility:** NTLM Auth, WebView2, DPI Scaling, 32-bit Libraries

WinBridge auto-resolves conflicts and missing dependencies when you change a module list, e.g., picking DXVK alongside WineD3D will warn you and drop WineD3D, since the two can't coexist. You can check this yourself without installing anything:

```
winbridge check-modules DXVK WineD3D
```

### Runners

A **runner** is the actual Wine or Proton build used to execute everything. WinBridge ships definitions for:

- **Proton GE** (`GloriousEggroll/proton-ge-custom`) — gaming-focused, downloaded on demand
- **Wine GE** (`GloriousEggroll/wine-ge-custom`) — for launchers and modding tools, downloaded on demand
- **Wine Staging** — uses your distro's system package if present
- **Wine Stable** — uses your distro's system `wine` package
- **Wine Development** — bleeding-edge, uses your distro's `wine-devel`/`wine-development` package

Downloadable runners (Proton GE, Wine GE) are fetched straight from their GitHub releases; system runners just shell out to whatever your distro already has installed. WinBridge will also find Proton/Wine GE builds you already downloaded through Steam or ProtonUp-Qt instead of duplicating them.

---

## Modding tools (Vortex/Mod Organizer 2/NXM links)

> **NOTE:** Modding support is in BETA. While the tool is in testing, I will be making sure this works as best as possible. Too long has Linux been ignored.

WinBridge has first-class support for the two big Nexus Mods managers, because "I just want to click Download on Nexus and have it land in my mod manager" is a real, common, and previously painful Linux workflow.

- **Vortex** and **Mod Organizer 2** are both selectable profiles with dedicated setup and post-install steps (WebView2 verification, filesystem symlink-support checks, NXM handler registration, launch smoke tests).
- Clicking **"Download with Mod Manager"** on Nexus Mods fires an `nxm://` URL at your browser. WinBridge registers itself as the system handler for that URL scheme via a small shell script + `.desktop` file (`xdg-mime`), so the link gets routed to `winbridge handle-nxm <url>`, which looks up which installed app declared `nxm_handler: true`, then launches Vortex or MO2 with that URL as an argument, exactly like it would on Windows.
- This handler script is written in a way that's deliberately AppImage-aware: it points at the *AppImage file itself* (via `$APPIMAGE`), not at a temporary internal mount path, so it keeps working after you close and reopen the app, and across reboots.

---

## The Hardware Hub

> **NOTE:** The UI looks different because this section controls the system; unlike the prior sections, this actually helps make your hardware work, so there are red buttons, colored cards, and so on to grab your attention to read what exactly this is changing and why it is changing. It is so you understand that changing anything here is important to know.

This exists because the very first thing a lot of Windows switchers hit isn't a software compatibility problem; it's "my Wi-Fi card barely works," or "I have no idea what CPU governor I should be using," or "why won't my controller pair." The Hardware Hub has four tabs aimed squarely at that:

- **CPU** — shows your current CPU governor and lets you switch between Power Save, Conservative, Balanced, Balanced (Legacy), Manual, and Performance, each with a plain-language explanation of the tradeoff.
- **GPU** — detects your GPU and walks you through the right driver path for it, including a "Hardware Swap" safety net if you're switching GPU vendors (e.g., NVIDIA → AMD) and need to cleanly back out of vendor-specific drivers first.
- **Controllers** — detects connected controllers and recommends the right driver (`xone` vs `xpad-noone` vs community forks), explaining what each one actually does instead of just listing package names.
- **WiFi** — Realtek and Broadcom Wi-Fi/Bluetooth chips have a long, well-known history of being badly supported by in-kernel Linux drivers. This tab detects your chipset and offers DKMS-based fixes specific to known problem chips (RTL8821CE, RTL8822BE, RTL8852AE, RTL8723DE, and others).

Every privileged action here (writing to `scaling_governor`, installing driver packages, reloading udev rules) goes through `pkexec`, and once you've installed the system helper on first launch, through the scoped `org.winbridge.helper` PolicyKit action rather than a blanket root prompt. See the next section.

---

## System Tune (GameTune Hub)

A separate, broader system-tuning surface from the Hardware Hub, organized into six tabs:

- **Overview** — current tuning state at a glance.
- **Steam Wrappers** — manages a combined launch wrapper script for Steam games (gaming-specific environment setup applied automatically at launch).
- **ProcessSentry** — a watchdog service/config for process-level monitoring.
- **Kernel Autotune** — a systemd-managed service + config for kernel-level performance tuning, with a persistent on/off "kill switch" (`/etc/no-auto-throttle`) you can flip without uninstalling anything.
- **Live Tuning** — apply tuning changes to the current session immediately.
- **Setup** — installs the udev rules, PolicyKit rule, and systemd units this whole subsystem depends on.

---

## Privileged operations & the PolicyKit helper

Several things in WinBridge legitimately need root: writing CPU governor files under `/sys`, installing driver packages, reloading udev rules, and managing the systemd services behind Kernel Autotune. Rather than asking you to type your password into a generic terminal prompt for every single one of these, WinBridge installs a small, deliberately narrow **PolicyKit helper**.

**What "scoped" means here, concretely:** the helper does *not* accept "run this arbitrary command as root." It exposes a fixed menu of named operations — `set-cpu-governor`, `write-file` (to one of a handful of known config paths, never an arbitrary path), `install-packages`/`remove-packages` (package names validated against a strict pattern before anything runs), `systemctl` (restricted to a fixed list of units and actions), `usermod-add-group` (restricted to a fixed list of groups), and a few other narrowly-defined operations. Anything outside that menu, or any argument that doesn't match what's expected, is rejected *before* it ever reaches a subprocess call — there is no shell string interpolation anywhere in the helper.

This is installed automatically and silently on your first **GUI** launch (CLI commands like `winbridge plugin list` never trigger it, since there's no reason a read-only command should ask for your password). It's a one-time, idempotent step — WinBridge hashes the bundled helper against what's already on your system and only re-prompts if something actually changed (e.g. after an update). Declining the prompt doesn't break anything; the app falls back to the same individual `pkexec` prompts it would have used otherwise.

If you want to inspect exactly what the helper can and can't do, it's a plain, readable Python script — see `packaging/winbridge-helper` in this repo.

---

## Plugins, in short

WinBridge's profile/module/runner system is fully extensible without touching the app's source. Plugins can add new profiles, new modules, new runners, new system checks, override distro-specific package manager commands, and even *extend* existing built-in profiles (e.g., adding a post-install step to the Gaming profile) through a deterministic, conflict-aware merge system.

This README only covers the surface; the full plugin authoring guide — manifest format, the trust/sandbox model, the `PLUGIN_EXTENDS` merge rules, how this all interacts with the AppImage packaging, and a complete worked example — lives in **[PLUGINS.md](PLUGINS.md)**.

Quick taste of the CLI surface:

```
winbridge store search vortex          # search the community plugin store
winbridge plugin install my-plugin     # install one
winbridge plugin list                  # see what's installed
winbridge plugin resolve steam_deck    # debug what plugins changed about a profile
```

---

## The command line

Everything below works identically whether you're running the AppImage (`./WinBridge.AppImage <command>`) or a source checkout (`python3 winbridge.py <command>`).

```
winbridge install <exe> [--profile ID] [--name NAME] [--runner ID] [--modules ...] [--dry-run]
winbridge list
winbridge launch <app> [--env KEY=VALUE ...] [--wrapper CMD] [--dry-run]
winbridge remove <app> [--keep-prefix]
winbridge profiles [--validate]
winbridge runners [install|remove RUNNER_ID]
winbridge syscheck
winbridge check-modules MODULE [MODULE ...]
winbridge handle-nxm <nxm-url>          # normally invoked automatically, not by hand
winbridge plugin list|install|update|remove|info|resolve [ID] [--all] [--json] [--force-refresh]
winbridge store search|refresh [QUERY] [--capability CAP] [--verified]
winbridge settings list|get|set [KEY] [VALUE]
winbridge --version
```

A few worth calling out:

- `winbridge install setup.exe --profile gaming --dry-run` — shows exactly what would happen (prefix path, modules, runner) without touching anything.
- `winbridge launch "My Game" --env SDL_VIDEODRIVER=x11 --wrapper gamemoderun --dry-run` — prints the final launch command and environment diff without launching, useful for debugging a launch-time problem.
- `winbridge plugin resolve steam_deck --json` — dumps the fully plugin-merged state of a profile as JSON, including an audit log of which plugin touched what. The single best tool for debugging "why does my profile look different from what I expect."

---

## Snapshots & backups

Every installed app can have versioned tarball snapshots of its Wine prefix taken from **My Apps**, stored under `~/.local/share/winbridge/snapshots/<app-id>/`. Up to 5 are kept per app; older ones are pruned automatically. This is what backs the "switch profile" workflow — if changing an app's profile/modules goes wrong, you can roll back to the last snapshot instead of reinstalling from scratch.

---

## Where WinBridge keeps its files

Everything lives under `~/.local/share/winbridge/`:

```text
~/.local/share/winbridge/
├── prefixes/        # one Wine prefix per installed app
├── runners/         # downloaded Proton GE / Wine GE builds
├── cache/           # store index cache, download cache
├── plugins/         # installed plugins (see PLUGINS.md)
├── snapshots/        # versioned prefix backups
├── logs/            # winbridge.log (rotating)
├── apps.json        # installed app records
├── runners.json     # runner registry
└── settings.json    # your settings
```

Nothing here lives inside the AppImage mount; the AppImage is read-only application code; all of your actual data is plain files in your home directory, so updating or reinstalling WinBridge never touches any of it.

---

## Logs & troubleshooting

- Log file: `~/.local/share/winbridge/logs/winbridge.log` (rotating, so it won't grow unbounded). You can jump straight to it from **Settings → General**.
- **System Check** page tells you exactly which dependency is missing and how to install it for your distro.
- If a privileged action silently fails, check whether the PolicyKit helper installed correctly (`ls -l /usr/lib/winbridge/winbridge-helper` should show an executable owned by root) — if it's missing, WinBridge fell back to per-action `pkexec` prompts, which is fine but worth knowing.
- `winbridge plugin resolve <profile_id> --json` is the fastest way to debug "this profile is behaving differently than I expect" when you have plugins installed.

---

## FAQ

**Is this a Wine replacement?**
No. WinBridge sits on top of Wine/Proton and orchestrates them; it never reimplements Windows API translation itself.

**Do I need Windows at all?**
No. You need the Windows installer file for the software you want to run; WinBridge and Wine handle the rest.

**Will every Windows program work?**
No tool can promise that. Wine/Proton compatibility varies by program, just as it would using Wine directly. WinBridge's job is to remove the *setup* friction, not to guarantee every program runs.

**Does this replace Lutris/Bottles/Heroic?**
Not exactly, there's real overlap, especially with Bottles. WinBridge's specific focus is the Windows-switcher onboarding experience (profiles tuned for non-gaming software too, the Hardware Hub, a heavier plugin/extension system) rather than being a general Wine prefix manager first.

**Is my data safe if I uninstall WinBridge?**
Yes, your prefixes, snapshots, and settings are plain files under `~/.local/share/winbridge/`, untouched by removing the AppImage itself. Removing *that* directory is what actually deletes your data; the two are intentionally separate steps.

---

## License

GPLv3. See `LICENSE`.

## Related Projects

| Project | Description | Link |
|---|---|---|
| Process Sentry | Process monitoring and management for Linux; controllable via WinBridge settings | [GitHub](https://github.com/bobbycomet/Process-Sentry) |
| Kernel Autotune V2 | Automated Linux kernel parameter tuning; controllable via WinBridge settings | [GitHub](https://github.com/bobbycomet/kernel-autotune-V2) |
| WinBridge Plugins | Community plugin store for WinBridge | [GitHub](https://github.com/bobbycomet/WinBridge-Plugins) |

---

*WinBridge v1.6.0 - Preview Release - Griffin Linux Project*
