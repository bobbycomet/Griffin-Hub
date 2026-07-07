# Griffin Hub

**A PyQt6 desktop app for tuning your Linux system and managing your hardware — built to be extended.**

Version 1.0.0 · Linux (Ubuntu/Debian-focused, KDE Plasma friendly) · Python 3 + PyQt6

---

## What is Griffin Hub?

Griffin Hub is a single desktop application that puts the three things Linux users normally have to do
by hand, process/resource management, kernel tuning, and hardware troubleshooting, behind one
consistent, safe, GUI.

Concretely, it's the front end for two background daemons (**ProcessSentry** and **Kernel Autotune**)
plus a standalone **Hardware Hub** for day-to-day hardware headaches (bad WiFi chips, GPU driver
checks, controller setup), all wrapped in a plugin system so the community can extend any of it
without waiting on a new release.

Everything that needs root (writing to `/etc`, restarting services, installing packages) goes
through `pkexec`. Griffin Hub itself never runs as root — it asks the system to authenticate the
one privileged action it needs, once, and nothing more.

## Why it exists

Griffin Hub grew out of an earlier project (internally, "WinBridge") that tried to be a Wine/Proton
compatibility layer manager. That part of the project didn't pan out — Wine/Proton tooling already
has good dedicated solutions (Lutris, Bottles, ProtonUp-Qt), and trying to reinvent that wheel meant
neglecting the parts of the app that were actually solving a real, underserved problem: **Linux gives
you an enormous amount of low-level tuning power, and almost none of it is reachable without editing
config files by hand and knowing which `sysctl` key does what.**

So the project was stripped down to what was genuinely useful — process throttling, kernel tuning,
and hardware fixes — and rebuilt around a plugin system, so that the parts of Linux tuning too
niche or too hardware-specific to belong in core (an unusual WiFi chip, a distro-specific quirk, a
game-specific tweak) don't need to wait on the maintainer. They can just be a plugin.

## What it does

Griffin Hub is organized around two main areas, plus a Plugins page and Settings:

- **System Tune** — a home for two background daemons that watch and adjust your system live
  (ProcessSentry, Kernel Autotune), plus one-off tuning tools (Steam launch wrappers, live kernel
  tuning) and a Setup tab to install everything needed.
- **Hardware Hub** — direct, no-daemon-required tools for CPU governor control, GPU diagnostics,
  controller setup, and because it's a genuinely awful, common problem on Linux, a WiFi/Ethernet
  chip fix library for the Realtek and Broadcom chipsets that never quite work right out of the box.
- **Plugins** — install, inspect, and run checks/actions contributed by the community, without ever
  leaving the app.
- **Settings** — app-level preferences: notifications, the plugin sandbox, and data paths.

All of it is styled consistently: one card component, one badge component, one button style,
so nothing feels bolted on, regardless of which daemon or subsystem a given screen is talking to.

<img width="1920" height="1080" alt="Screenshot_20260705_101840" src="https://github.com/user-attachments/assets/025b8563-7b60-48be-9afc-0928038163a1" />
<img width="1920" height="1080" alt="Screenshot_20260705_120014" src="https://github.com/user-attachments/assets/bf053956-bec7-4b2c-a1eb-25786b8137e0" />
<img width="1920" height="1080" alt="Screenshot_20260705_120027" src="https://github.com/user-attachments/assets/b8f4553f-840d-4a97-b68c-36c8196730de" />
<img width="1920" height="1080" alt="Screenshot_20260705_120108" src="https://github.com/user-attachments/assets/2ba1f89d-9578-4d37-aa64-cb65e9463ffd" />
<img width="1920" height="1080" alt="Screenshot_20260705_120102" src="https://github.com/user-attachments/assets/d7fdb5e7-88e7-43f5-85e6-e85b21d4a3db" />


---

## The tabs, in detail

### Dashboard

The landing page. Five at-a-glance gauges (CPU, RAM, GPU, VRAM, temperature), live status from
ProcessSentry and Kernel Autotune, the two things running in the background that only Griffin Hub
actually knows about, and four simple 60-second history graphs (CPU/RAM/GPU/VRAM). It's deliberately
narrow in scope: no process tree, no network analyzer, no disk analyzer, nothing resembling htop
bundled into the app. It exists to answer one question — *did clicking that button actually do
something* — not to replace a system monitor. GPU/VRAM readings currently support NVIDIA (via
`nvidia-smi`) and AMD (via sysfs); Intel GPU utilization doesn't have a similarly simple one-shot
query, so those gauges show "not available" rather than a number that isn't real.

### System Tune

System Tune is itself a tabbed page. Each tab controls a different piece of the tuning stack:

#### Overview
A dashboard for the System Tune stack as a whole: which of the relevant binaries are actually
installed (`gamemoderun`, `gamescope`, `mangohud`, ProcessSentry, Kernel Autotune), the live status
of both background services, and whether the Steam launch wrapper is currently active. This is the
"is everything wired up correctly" tab. Start here if something else isn't working.

#### Steam Wrappers
Griffin Hub can wrap your Steam launch command with GameMode, gamescope, and MangoHud, combined
into a single generated wrapper script and desktop entry, so you don't have to hand-write launch
options for every game. This tab lets you:
- Toggle GameMode, gamescope, and MangoHud independently
- Set gamescope's internal and output resolution, refresh rate, and toggle FSR/VRR/HDR
- Back up and restore your original `steam.desktop` file before Griffin Hub touches it
- Regenerate the wrapper script at any time as you change settings

Nothing here requires a background daemon; it's a one-time (well, one-time-per-change) script
generation step, applied via `pkexec`.

#### ProcessSentry
ProcessSentry is a predictive, adaptive process-priority daemon. Instead of a static nice value, it:
- Polls the process table at an adaptive interval (faster under load, slower when idle)
- Throttles (via cgroup v2 + nice/ionice) any process that crosses a CPU/IO/memory threshold
- **Learns** — a process that gets throttled repeatedly accumulates a "habit score," and once that
  score crosses a confidence threshold, ProcessSentry pre-throttles it on the next cycle before it
  even misbehaves again
- Never touches display servers, compositors, audio daemons, or anything it recognizes as a game
  (Steam, Proton, Wine, Lutris, Heroic, Bottles, and their overlays are all explicitly excluded)

This tab is a full live editor for `/etc/process-sentry/config.yaml` — every section of the config
(polling intervals, throttle thresholds, predictive throttling, adaptive learning, cgroup quotas,
exclusion lists, logging, and advanced tuning) has a corresponding form field, so you don't need to
hand-edit YAML to tune it. A raw-YAML box is also available for anything not exposed as a field.
There's also an emergency kill switch that pauses all throttling without stopping the daemon.

#### Kernel Autotune *(or Live Tuning — see below)*
Kernel Autotune is a boot-time daemon that detects your hardware (RAM, cores, SSD/NUMA/thermal
presence, GPU) once and applies a matching set of kernel parameters: swappiness, dirty-page ratios,
CPU governor, transparent huge pages, ZRAM/ZSWAP, I/O scheduler (per SSD/HDD), NUMA balancing, and
network congestion control/queueing discipline.

This tab is a full live editor for `/etc/kernel-autotune/config.sh` — **this file is yours to edit**;
every setting it contains has a form field here. Alongside it, `/etc/kernel-autotune/state.json` is
shown as **read-only system info** (detected hardware, last-applied values, last apply status),
it's a snapshot of what the daemon detected and last did, not something you edit directly.

> **Griffin Hub shows either "Kernel Autotune" or "Live Tuning" — never both.**
> If Kernel Autotune is installed on your system, you get the full config.sh editor described above.
> If it isn't installed, the tab is replaced with **Live Tuning**: the same categories of settings
> (governor, swappiness, dirty ratios, THP, ZRAM, TCP congestion control, qdisc, I/O scheduler,
> network buffer sizes) applied immediately via `pkexec`, with no persistent config file and no
> daemon required. Either way, you can tune your kernel live from this tab, which underlying
> mechanism you get just depends on what's installed. Installing Kernel Autotune (from the Setup tab)
> swaps you over to the full editor without needing to restart the app.

#### Setup
Installs everything System Tune depends on: the required packages (`gamemode`, `gamescope`,
`mangohud`, `python3-psutil`, `python3-yaml`, plus anything a plugin has added to this list, see
`PLUGINS.md`), the ProcessSentry and Kernel Autotune systemd service files, and adds your user to
the `gamemode` group. This is the tab to run once, on a fresh install, before touching anything else.

### Hardware Hub

Hardware Hub doesn't depend on any background daemon — everything here is a direct, on-demand action.

- **CPU** — inspect topology and battery/chassis status, and set the CPU governor directly
  (`schedutil`, `performance`, `powersave`, etc.) via `pkexec`.
- **GPU** — vendor/driver detection (NVIDIA/AMD/Intel), Mesa version, RADV status, whether gamescope
  is active, and CUDA/Vulkan tooling install shortcuts.
- **Controllers** — detection and setup help for game controllers.
- **WiFi** — realistically, the single most annoying category of Linux hardware support. This tab
  detects your network chipset and, for the worst-offending Realtek and Broadcom chips, offers a
  one-click DKMS driver fix pulled from community-maintained repos, along with an explanation of
  *why* that chip is problematic and what the fix actually does.

### Plugins

Shows every plugin currently loaded, name, version, author, and trust level, along with any load
warnings, and lets you run the checks and one-click actions that plugins have registered for either
System Tune or Hardware Hub, right from this page. Plugins can `git clone` driver sources (into a
dedicated, user-owned directory — never a system path) as part of a check or action, which is how
community plugins add support for controllers or other hardware that only has an out-of-tree,
build-it-yourself Linux driver. See **PLUGINS.md** for how to write one.

### Settings

App-level preferences: desktop notifications, the plugin sandbox (on by default — see PLUGINS.md for
what this means), the plugin store URL (so you can point it at a private/mirrored index), the cache
directory, and the PolicyKit helper.

**PolicyKit helper.** Every privileged action in Griffin Hub runs through `pkexec`. That works out of
the box for a normal install, but it doesn't for the AppImage build specifically — `pkexec` refuses
to elevate a target that isn't a stable, root-verifiable file, and an AppImage is a user-owned file
that's FUSE-mounted in your session, which doesn't qualify. Settings → PolicyKit → **Install
PolicyKit Helper** installs a tiny helper script to `/usr/local/bin/griffinhub-pkexec-helper` plus a
PolicyKit `.policy` action file, giving `pkexec` a stable target regardless of how Griffin Hub is
packaged, and giving you a properly branded authentication prompt ("Griffin Hub needs to make a
system change") instead of a generic one. It's a one-time, one-click install (itself done via a
single plain `pkexec` call, since installing the helper doesn't have the chicken-and-egg problem the
helper solves for everything after it). Not installing it doesn't break anything — every pkexec call
in the app falls back to calling `pkexec` directly, exactly as it did before this existed.

---

## Command line

Everything except the actual GUI screens is also available headless:

```bash
griffinhub                          # launch the GUI
griffinhub --version

griffinhub plugin list              # installed plugins
griffinhub plugin install <id>      # install from the community store
griffinhub plugin update <id>       # update one plugin
griffinhub plugin update --all      # update everything
griffinhub plugin remove <id>
griffinhub plugin info <id>

griffinhub store search             # browse the community store
griffinhub store search wifi        # search
griffinhub store refresh            # bypass the cache and re-fetch the index

griffinhub settings list
griffinhub settings get plugin_sandbox
griffinhub settings set plugin_sandbox true
```

## Data & config locations

| What | Where |
|---|---|
| Griffin Hub's own data (settings, cache, plugins, logs) | `~/.local/share/griffinhub/` |
| Plugins | `~/.local/share/griffinhub/plugins/` |
| Plugin-cloned driver sources (git clone target) | `~/.local/share/griffinhub/drivers/` |
| Log file | `~/.local/share/griffinhub/logs/griffinhub.log` |
| ProcessSentry config | `/etc/process-sentry/config.yaml` |
| Kernel Autotune config (yours to edit) | `/etc/kernel-autotune/config.sh` |
| Kernel Autotune state (read-only, system info) | `/etc/kernel-autotune/state.json` |
| Kill switch (pauses ProcessSentry without stopping it) | `/etc/no-auto-throttle` |
| PolicyKit helper (optional, Settings → PolicyKit) | `/usr/local/bin/griffinhub-pkexec-helper` |
| PolicyKit action file (optional) | `/usr/share/polkit-1/actions/com.griffinhub.pkexec.policy` |

## Requirements

- Linux with `pkexec` (PolicyKit) available
- Python 3.10+
- `PyQt6` (`pip install PyQt6`)
- `PyYAML` (for the ProcessSentry editor — `pip install pyyaml`)
- Optional: `bubblewrap` (`bwrap`) for full plugin sandbox isolation — Griffin Hub falls back to a
  stripped-down subprocess sandbox if it isn't installed, but bubblewrap is recommended

```bash
python3 griffinhub.py
```

## Building the AppImage

`build-appimage.sh` builds `GriffinHub-1.0.0-x86_64.AppImage` via `appimagetool-x86_64.AppImage`.

```bash
chmod +x build-appimage.sh
./build-appimage.sh                 # bundled Python — self-contained, recommended
./build-appimage.sh --thin          # thin build — relies on the end user's system python3/PyQt6
./build-appimage.sh --clean         # remove build artifacts
```

Needs `griffinhub.py` and `griffinhub.png` next to the script, and either `appimagetool-x86_64.AppImage`
on your `PATH` or `APPIMAGETOOL=/path/to/it` set. The default (bundled) mode copies a full Python
stdlib + interpreter into the AppImage and pip-installs PyQt6/PyYAML into it, so the result runs on a
bare Linux install with no Python setup at all — this is what makes the PolicyKit helper (see Settings,
above) necessary in the first place, since `pkexec` can't elevate the AppImage file itself. `--thin`
skips all that and just calls the system's own `python3` at runtime, producing a much smaller AppImage,
at the cost of requiring `pip install PyQt6 pyyaml` on every machine it runs on.

## Safety notes

- Griffin Hub never runs as root. Every privileged action (writing a config file under `/etc`,
  restarting a service, installing a package) is a single, explicit `pkexec` call — you'll get the
  normal system authentication prompt for each one.
- Community plugins are sandboxed by default and pass through an AST safety scanner before they're
  ever executed — see **PLUGINS.md** for exactly what that means and what it blocks.
- Destructive actions (resetting Kernel Autotune, restoring your original `steam.desktop`) always
  show a confirmation dialog listing exactly what commands will run first.

## Extending Griffin Hub

If there's a hardware quirk or tuning tweak you want that isn't here, you probably don't need to
wait for a new release — you can write a plugin. See **[PLUGINS.md](./PLUGINS.md)** for the full
guide, including working examples.
