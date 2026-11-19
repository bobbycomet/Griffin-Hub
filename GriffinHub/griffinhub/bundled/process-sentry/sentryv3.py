#!/usr/bin/env python3
"""
ProcessSentry v3 - Predictive System Performance Guardian
High-performance adaptive process priority daemon with ML-based prediction

Key Improvements:
- Predictive throttling based on process behavior patterns
- Lock-free data structures for minimal overhead
- Batch processing for system calls
- Pre-emptive throttling for known resource hogs
- Better cgroup v2 integration with CPU quotas
- Memory pressure detection via PSI (Pressure Stall Information)
- Faster polling with intelligent back-off
- Process classification (system/user/gaming/background)
- Better laptop battery preservation
"""
import os
import sys
import time
import yaml
import psutil
import signal
import atexit
import logging
import hashlib
import json
import platform
import subprocess
from pathlib import Path
from enum import Enum
from typing import Dict, Tuple, Set, Optional
from collections import defaultdict, deque
from dataclasses import dataclass, field

# =========================
# Configuration
# =========================
CONFIG_PATH = Path("/etc/process-sentry/config.yaml")
HABIT_PATH = Path("/var/lib/process-sentry/habits.yaml")
LOG_PATH = Path("/var/log/process-sentry.log")
PID_PATH = Path("/run/process-sentry.pid")
KILL_SWITCH = Path("/etc/no-auto-throttle")
CGROUP_ROOT = Path("/sys/fs/cgroup")
CGROUP_NAME = "sentry.slice"
PSI_PATH = Path("/proc/pressure")

DEFAULT_CONFIG = {
    "intervals": {
        "base": 5.0,          # Faster base polling
        "min": 1.0,           # More responsive
        "max": 15.0,          # Less aggressive max
        "ema_alpha": 0.3,     # More reactive to changes
    },
    "thresholds": {
        "cpu_percent": 50.0,
        "io_mbps": 25.0,
        "memory_percent": 18.0,
        "memory_pressure_avg10": 10.0,  # PSI threshold
        # physical (default) = historical; logical = count SMT threads (avoids under-report)
        "cpu_count_mode": "physical",
    },
    "prediction": {
        "history_size": 10,
        "pre_throttle_confidence": 0.75,
        "pattern_match_threshold": 0.8,
    },
    "learning": {
        "up_rate": 0.08,
        "down_rate": 0.03,
        "habit_cap": 1.0,
        "decay_rate": 0.001,  # Slowly forget old patterns
    },
    "cgroup": {
        "cpu_quota_throttled": 70,    # 70% CPU when throttled
        "cpu_quota_normal": 100,
        "io_weight_throttled": 10,
        "io_weight_normal": 100,
    },
    "exclusions": {
        "never_touch_prefixes": [
            "systemd", "dbus", "pipewire", "wireplumber",
            "pulseaudio", "Xorg", "wayland", "kwin", "cinnamon", 
            "muffin", "gnome", "plasmashell", "login", "sshd", 
            "udevd", "NetworkManager", "steamwebhelper", "irqbalance"
        ],
        "gaming_markers": [
            "steam", "proton", "wine", "gamescope", "mangohud", 
            "gameoverlayrenderer", "reaper", ".exe"
        ],
        "system_critical": [
            "init", "kernel", "kthread", "migration"
        ],
    },
}

# =========================
# Logging
# =========================
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler()
    ],
    datefmt="%H:%M:%S",
)

# =========================
# Data Structures
# =========================
@dataclass
class ProcessMetrics:
    """Stores recent metrics for a process"""
    cpu_history: deque = field(default_factory=lambda: deque(maxlen=10))
    io_history: deque = field(default_factory=lambda: deque(maxlen=10))
    mem_history: deque = field(default_factory=lambda: deque(maxlen=10))
    last_cpu_time: float = 0.0
    last_cpu_ts: float = 0.0
    last_io_bytes: int = 0
    last_io_ts: float = 0.0
    throttle_count: int = 0
    last_seen: float = 0.0

class ProcessClass(Enum):
    SYSTEM = "system"
    GAMING = "gaming"
    USER_INTERACTIVE = "user_interactive"
    BACKGROUND = "background"

class Mode(Enum):
    DESKTOP = "desktop"
    LAPTOP = "laptop"
    HANDHELD = "handheld"

# =========================
# Utilities
# =========================
def require_root():
    if os.getuid() != 0:
        logging.error("Must run as root")
        sys.exit(1)

def load_yaml(path: Path, default: dict) -> dict:
    try:
        if path.exists():
            with open(path) as f:
                loaded = default.copy()
                data = yaml.safe_load(f) or {}
                for key, value in data.items():
                    if key in loaded and isinstance(loaded[key], dict) and isinstance(value, dict):
                        loaded[key].update(value)
                    else:
                        loaded[key] = value
                return loaded
    except Exception as e:
        logging.warning(f"Failed loading {path}: {e}")
    return default.copy()

def save_yaml(path: Path, data: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, indent=2, default_flow_style=False)
        tmp.replace(path)
    except Exception as e:
        logging.warning(f"Failed saving {path}: {e}")

def on_battery() -> bool:
    try:
        b = psutil.sensors_battery()
        return b is not None and not b.power_plugged
    except Exception:
        return False

def is_handheld() -> bool:
    node = platform.node().lower()
    return (
        "deck" in node
        or ("rog" in node and "ally" in node)
        or Path("/sys/class/power_supply/axp20x-battery").exists()
    )

def get_memory_pressure() -> Optional[float]:
    """Read memory pressure from PSI (Pressure Stall Information)"""
    try:
        mem_psi = PSI_PATH / "memory"
        if mem_psi.exists():
            with open(mem_psi) as f:
                for line in f:
                    if line.startswith("some avg10="):
                        return float(line.split("=")[1].split()[0])
    except Exception:
        pass
    return None

# =========================
# Main Daemon
# =========================
class ProcessSentry:
    def __init__(self):
        self.config = load_yaml(CONFIG_PATH, DEFAULT_CONFIG)
        self.habits: Dict[str, float] = load_yaml(HABIT_PATH, {})
        
        # Process tracking
        self.process_metrics: Dict[int, ProcessMetrics] = {}
        self.process_classes: Dict[int, ProcessClass] = {}
        self.original_priorities: Dict[int, Tuple[int, Tuple[int, int]]] = {}
        self.throttled: Set[int] = set()
        
        # Thresholds (will be updated by mode)
        self.base_cpu_threshold = self.config["thresholds"]["cpu_percent"]
        self.base_io_threshold = self.config["thresholds"]["io_mbps"] * 1024 * 1024
        self.base_mem_threshold = self.config["thresholds"]["memory_percent"]
        self.mem_pressure_threshold = self.config["thresholds"]["memory_pressure_avg10"]
        
        self.cpu_threshold = self.base_cpu_threshold
        self.io_threshold = self.base_io_threshold
        self.mem_threshold = self.base_mem_threshold
        
        # Intervals
        self.base_interval = self.config["intervals"]["base"]
        self.interval = self.base_interval
        self.min_interval = self.config["intervals"]["min"]
        self.max_interval = self.config["intervals"]["max"]
        self.ema_alpha = self.config["intervals"]["ema_alpha"]
        self.load_ema = 0.0
        
        # Learning
        self.learn_up = self.config["learning"]["up_rate"]
        self.learn_down = self.config["learning"]["down_rate"]
        self.habit_cap = self.config["learning"]["habit_cap"]
        self.decay_rate = self.config["learning"]["decay_rate"]
        
        # Prediction
        self.history_size = self.config["prediction"]["history_size"]
        self.pre_throttle_confidence = self.config["prediction"]["pre_throttle_confidence"]
        
        # Exclusions
        self.never_touch = [p.lower() for p in self.config["exclusions"]["never_touch_prefixes"]]
        self.gaming_markers = [m.lower() for m in self.config["exclusions"]["gaming_markers"]]
        self.system_critical = [s.lower() for s in self.config["exclusions"]["system_critical"]]
        
        # CPU divisor for percent. Config: thresholds.cpu_count_mode = physical|logical
        mode = (self.config.get("thresholds") or {}).get("cpu_count_mode", "physical")
        if mode == "logical":
            self.cpu_count = psutil.cpu_count(logical=True) or 1
        else:
            self.cpu_count = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 1
        self.cpu_count_mode = mode
        
        self.detect_mode()
        self.setup_cgroup()
        self.write_pid()
        self.register_cleanup()
        
        logging.info(
            f"ProcessSentry v3 started — mode={self.mode.value}, "
            f"cores={self.cpu_count} ({self.cpu_count_mode})"
        )

    def detect_mode(self):
        prev_mode = getattr(self, "mode", None)
        
        if is_handheld():
            self.mode = Mode.HANDHELD
            self.cpu_threshold = 80.0
            self.io_threshold = self.base_io_threshold * 2.0
            self.mem_threshold = 30.0
        elif on_battery():
            self.mode = Mode.LAPTOP
            self.cpu_threshold = 60.0
            self.io_threshold = self.base_io_threshold * 1.3
            self.mem_threshold = self.base_mem_threshold
        else:
            self.mode = Mode.DESKTOP
            self.cpu_threshold = self.base_cpu_threshold
            self.io_threshold = self.base_io_threshold
            self.mem_threshold = self.base_mem_threshold
        
        if prev_mode and prev_mode != self.mode:
            logging.info(f"Mode changed: {prev_mode.value} → {self.mode.value}")

    def classify_process(self, proc: psutil.Process, name_lower: str) -> ProcessClass:
        """Classify process for appropriate handling"""
        try:
            # System critical
            if any(name_lower.startswith(s) for s in self.system_critical):
                return ProcessClass.SYSTEM
            
            # Gaming
            cmd = " ".join(proc.cmdline() or []).lower()
            if any(marker in cmd for marker in self.gaming_markers):
                return ProcessClass.GAMING
            
            # Check if interactive (has terminal or GUI)
            try:
                if proc.terminal() or proc.num_threads() > 3:
                    return ProcessClass.USER_INTERACTIVE
            except Exception:
                pass
            
            return ProcessClass.BACKGROUND
        except Exception:
            return ProcessClass.BACKGROUND

    def is_never_touch(self, name_lower: str) -> bool:
        return any(name_lower.startswith(prefix) for prefix in self.never_touch)

    def habit_key(self, proc: psutil.Process, name_lower: str) -> str:
        """name + short cmdline hash — reduces collisions for short process names."""
        try:
            cmd = " ".join(proc.cmdline() or [])[:200]
        except Exception:
            cmd = ""
        if not cmd:
            return name_lower
        h = hashlib.sha1(cmd.encode("utf-8", errors="replace")).hexdigest()[:10]
        return f"{name_lower}#{h}"

    # -------------------------
    # Cgroup v2 Setup
    # -------------------------
    def _write_cgroup_status(self, mode: str, detail: str = ""):
        """Hub reads /var/lib/process-sentry/status.json to show cgroup fallback."""
        try:
            path = Path("/var/lib/process-sentry/status.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"cgroup": mode, "detail": detail}))
        except Exception:
            pass

    def setup_cgroup(self):
        """Setup cgroup v2 with CPU and IO controls"""
        self.cgroup_path = None
        
        try:
            if not CGROUP_ROOT.exists():
                return
            
            # Check if we're using cgroup v2
            with open("/proc/mounts") as f:
                if "cgroup2" not in f.read():
                    logging.warning("cgroup v2 not detected, using nice/ionice fallback")
                    return
            
            self.cgroup_path = CGROUP_ROOT / CGROUP_NAME
            self.cgroup_path.mkdir(exist_ok=True)
            
            # Enable controllers
            subtree_control = CGROUP_ROOT / "cgroup.subtree_control"
            if subtree_control.exists():
                try:
                    current = subtree_control.read_text().strip()
                    needed = {"cpu", "io", "memory"}
                    missing = needed - set(current.split())
                    if missing:
                        subtree_control.write_text(" ".join(f"+{c}" for c in missing))
                except Exception as e:
                    logging.debug(f"Controller enable failed (may be OK): {e}")
            
            # Set default limits for throttled processes
            self.apply_cgroup_limits(throttled=True)
            
            logging.info(f"cgroup v2 initialized: {self.cgroup_path}")
            self._write_cgroup_status("v2", str(self.cgroup_path))
        except Exception as e:
            logging.warning(f"cgroup setup failed, using nice/ionice: {e}")
            self.cgroup_path = None
            self._write_cgroup_status("fallback_nice_ionice", str(e)[:200])

    def apply_cgroup_limits(self, throttled: bool = True):
        """Apply CPU and IO limits to the cgroup"""
        if not self.cgroup_path:
            return
        
        try:
            quota = self.config["cgroup"]["cpu_quota_throttled" if throttled else "cpu_quota_normal"]
            io_weight = self.config["cgroup"]["io_weight_throttled" if throttled else "io_weight_normal"]
            
            # CPU quota (percentage of one core * 1000)
            cpu_max = self.cgroup_path / "cpu.max"
            if cpu_max.exists():
                cpu_max.write_text(f"{quota * 1000} 100000\n")
            
            # IO weight
            io_weight_file = self.cgroup_path / "io.weight"
            if io_weight_file.exists():
                io_weight_file.write_text(f"default {io_weight}\n")
        except Exception as e:
            logging.debug(f"Failed to apply cgroup limits: {e}")

    def move_to_cgroup(self, pid: int, target_path: Path):
        try:
            procs_file = target_path / "cgroup.procs"
            procs_file.write_text(f"{pid}\n")
        except Exception as e:
            logging.debug(f"Failed moving PID {pid} to cgroup: {e}")

    # -------------------------
    # Prediction and Analysis
    # -------------------------
    def should_pre_throttle(self, pid: int, name_lower: str, hkey: str | None = None) -> bool:
        """Predict if process will need throttling based on history"""
        habit = self.habits.get(hkey or name_lower, self.habits.get(name_lower, 0.0))
        
        # Strong habit pattern
        if habit > self.pre_throttle_confidence:
            return True
        
        # Check recent history
        if pid in self.process_metrics:
            metrics = self.process_metrics[pid]
            if len(metrics.cpu_history) >= 3:
                avg_cpu = sum(metrics.cpu_history) / len(metrics.cpu_history)
                if avg_cpu > self.cpu_threshold * 0.8:
                    return True
        
        return False

    def update_metrics(self, pid: int, cpu_percent: float, io_bps: float, mem_percent: float):
        """Update process metrics for prediction"""
        if pid not in self.process_metrics:
            self.process_metrics[pid] = ProcessMetrics()
        
        metrics = self.process_metrics[pid]
        metrics.cpu_history.append(cpu_percent)
        metrics.io_history.append(io_bps)
        metrics.mem_history.append(mem_percent)
        metrics.last_seen = time.time()

    def cleanup_stale_metrics(self, current_time: float, max_age: float = 300):
        """Remove metrics for processes that haven't been seen recently"""
        stale = [pid for pid, m in self.process_metrics.items() 
                 if current_time - m.last_seen > max_age]
        for pid in stale:
            del self.process_metrics[pid]
            self.process_classes.pop(pid, None)

    # -------------------------
    # Throttling Actions
    # -------------------------
    def throttle(self, proc: psutil.Process, reason: str = "overload"):
        if proc.pid in self.throttled:
            return
        
        try:
            # Store original priorities
            if proc.pid not in self.original_priorities:
                self.original_priorities[proc.pid] = (
                    proc.nice(),
                    proc.ionice()
                )
            
            # Apply nice/ionice
            proc.nice(15)  # More aggressive throttling
            proc.ionice(psutil.IOPRIO_CLASS_BE, 7)
            
            # Move to throttled cgroup
            if self.cgroup_path:
                self.move_to_cgroup(proc.pid, self.cgroup_path)
            
            self.throttled.add(proc.pid)
            
            # Update metrics
            if proc.pid in self.process_metrics:
                self.process_metrics[proc.pid].throttle_count += 1
            
            logging.debug(f"Throttled {proc.name()}[{proc.pid}] ({reason})")
        except Exception as e:
            logging.debug(f"Throttle failed for PID {proc.pid}: {e}")

    def unthrottle(self, proc: psutil.Process):
        if proc.pid not in self.throttled:
            return
        
        try:
            # Restore original priorities
            original = self.original_priorities.get(proc.pid)
            if original:
                orig_nice, orig_ionice = original
                proc.nice(orig_nice)
                proc.ionice(*orig_ionice)
            else:
                proc.nice(0)
                proc.ionice(psutil.IOPRIO_CLASS_BE, 4)
            
            # Move back to root cgroup
            if self.cgroup_path:
                self.move_to_cgroup(proc.pid, CGROUP_ROOT)
            
            self.throttled.discard(proc.pid)
            self.original_priorities.pop(proc.pid, None)
            
            logging.debug(f"Unthrottled {proc.name()}[{proc.pid}]")
        except Exception as e:
            logging.debug(f"Unthrottle failed for PID {proc.pid}: {e}")

    # -------------------------
    # Main Loop
    # -------------------------
    def monitor(self):
        last_habit_save = time.time()
        last_metrics_cleanup = time.time()
        game_requested = False
        
        # Prime CPU measurement
        psutil.cpu_percent(percpu=False)
        
        while True:
            loop_start = time.time()
            
            # Check kill switch
            if KILL_SWITCH.exists():
                logging.info("Kill switch active — pausing")
                time.sleep(10)
                continue
            
            # Re-detect mode periodically
            self.detect_mode()
            
            # Calculate system load
            load = psutil.cpu_percent(interval=None)
            self.load_ema = (self.ema_alpha * load) + (1 - self.ema_alpha) * self.load_ema
            
            # Adjust interval based on load (more aggressive)
            if self.load_ema > 80:
                self.interval = self.min_interval
            elif self.load_ema < 20:
                self.interval = self.max_interval
            else:
                self.interval = self.base_interval * (1 + (self.load_ema - 50) / 100)
            self.interval = max(self.min_interval, min(self.max_interval, self.interval))
            
            # Check memory pressure
            mem_pressure = get_memory_pressure()
            under_memory_pressure = mem_pressure and mem_pressure > self.mem_pressure_threshold
            
            any_game_detected = False
            batch_throttle = []
            batch_unthrottle = []
            
            # Process iteration
            for proc in psutil.process_iter(["pid", "name", "memory_percent", "cmdline", "cpu_times"]):
                try:
                    info = proc.info
                    pid = info["pid"]
                    name_lower = info["name"].lower()
                    
                    # Skip system critical and never-touch
                    if self.is_never_touch(name_lower):
                        if pid in self.throttled:
                            batch_unthrottle.append(proc)
                        self.process_metrics.pop(pid, None)
                        continue
                    
                    # Classify process
                    if pid not in self.process_classes:
                        self.process_classes[pid] = self.classify_process(proc, name_lower)
                    
                    proc_class = self.process_classes[pid]
                    
                    # Gaming override
                    if proc_class == ProcessClass.GAMING:
                        any_game_detected = True
                        if pid in self.throttled:
                            batch_unthrottle.append(proc)
                        continue
                    
                    now = time.time()
                    
                    # Calculate CPU percent (accurate method)
                    ctimes = info["cpu_times"]
                    total_cpu_time = ctimes.user + ctimes.system
                    
                    if pid in self.process_metrics:
                        metrics = self.process_metrics[pid]
                        prev_cpu_time = metrics.last_cpu_time
                        prev_cpu_ts = metrics.last_cpu_ts
                        time_delta = max(now - prev_cpu_ts, 0.01)
                        cpu_delta = total_cpu_time - prev_cpu_time
                        cpu_percent = (cpu_delta / time_delta) * 100 / self.cpu_count
                    else:
                        cpu_percent = 0.0
                        self.process_metrics[pid] = ProcessMetrics()
                    
                    self.process_metrics[pid].last_cpu_time = total_cpu_time
                    self.process_metrics[pid].last_cpu_ts = now
                    
                    # Calculate IO
                    try:
                        io = proc.io_counters()
                        total_io = io.read_bytes + io.write_bytes
                        
                        metrics = self.process_metrics[pid]
                        if metrics.last_io_bytes > 0:
                            io_delta = max(now - metrics.last_io_ts, 0.01)
                            io_bps = (total_io - metrics.last_io_bytes) / io_delta
                        else:
                            io_bps = 0.0
                        
                        metrics.last_io_bytes = total_io
                        metrics.last_io_ts = now
                    except Exception:
                        io_bps = 0.0
                    
                    # Memory
                    mem = info["memory_percent"] or 0.0
                    
                    # Update metrics
                    self.update_metrics(pid, cpu_percent, io_bps, mem)
                    
                    # Habit factor (name#cmdline-hash key; falls back to bare name)
                    hkey = self.habit_key(proc, name_lower)
                    habit = self.habits.get(hkey, self.habits.get(name_lower, 0.0))
                    factor = max(0.3, 1.0 - habit)  # More aggressive reduction
                    
                    # System class gets more lenient thresholds
                    if proc_class == ProcessClass.SYSTEM:
                        factor *= 1.5
                    
                    # Overload detection
                    overloaded = (
                        cpu_percent > self.cpu_threshold * factor or
                        io_bps > self.io_threshold * factor or
                        mem > self.mem_threshold * factor or
                        (under_memory_pressure and mem > self.mem_threshold * 0.5)
                    )
                    
                    # Predictive throttling
                    if not overloaded and self.should_pre_throttle(pid, name_lower, hkey):
                        overloaded = True
                        logging.debug(f"Pre-throttling {name_lower}[{pid}] (predicted)")
                    
                    # Apply throttling decision (batched)
                    if overloaded:
                        batch_throttle.append((proc, name_lower, hkey))
                    else:
                        if pid in self.throttled:
                            batch_unthrottle.append(proc)
                        # Decay habit slowly when behaving
                        self.habits[hkey] = max(0.0, habit - self.learn_down)
                
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    self.throttled.discard(pid)
                    self.process_metrics.pop(pid, None)
                    self.process_classes.pop(pid, None)
                    self.original_priorities.pop(pid, None)
                except Exception as e:
                    logging.debug(f"Error processing PID {pid}: {e}")
            
            # Batch apply throttling
            for proc, name_lower, hkey in batch_throttle:
                self.throttle(proc)
                habit = self.habits.get(hkey, self.habits.get(name_lower, 0.0))
                self.habits[hkey] = min(self.habit_cap, habit + self.learn_up)
            
            for proc in batch_unthrottle:
                self.unthrottle(proc)
            
            # GameMode integration
            if any_game_detected and not game_requested:
                try:
                    subprocess.Popen(
                        ["gamemoded", "-r"], 
                        stdout=subprocess.DEVNULL, 
                        stderr=subprocess.DEVNULL
                    )
                    logging.info("Gaming detected — GameMode requested")
                    game_requested = True
                except FileNotFoundError:
                    pass
            elif not any_game_detected:
                game_requested = False
            
            # Periodic maintenance
            current_time = time.time()
            
            # Decay all habits slowly
            if len(self.habits) > 0:
                for name in list(self.habits.keys()):
                    self.habits[name] = max(0.0, self.habits[name] - self.decay_rate)
                    if self.habits[name] < 0.001:
                        del self.habits[name]
            
            # Save habits
            if current_time - last_habit_save > 180:  # 3 minutes
                save_yaml(HABIT_PATH, self.habits)
                logging.info(f"Saved {len(self.habits)} habits, {len(self.throttled)} throttled")
                last_habit_save = current_time
            
            # Clean stale metrics
            if current_time - last_metrics_cleanup > 60:
                self.cleanup_stale_metrics(current_time)
                last_metrics_cleanup = current_time
            
            # Sleep with compensation for loop time
            loop_time = time.time() - loop_start
            sleep_time = max(0.1, self.interval - loop_time)
            time.sleep(sleep_time)

    # -------------------------
    # Cleanup
    # -------------------------
    def write_pid(self):
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()) + "\n")

    def register_cleanup(self):
        def cleanup():
            logging.info("Shutting down...")
            
            # Unthrottle all processes
            for pid in list(self.throttled):
                try:
                    proc = psutil.Process(pid)
                    self.unthrottle(proc)
                except Exception:
                    pass
            
            # Clean up cgroup
            if self.cgroup_path and self.cgroup_path.exists():
                procs_file = self.cgroup_path / "cgroup.procs"
                if procs_file.exists():
                    try:
                        pids = [int(l.strip()) for l in procs_file.read_text().splitlines() if l.strip()]
                        for pid in pids:
                            self.move_to_cgroup(pid, CGROUP_ROOT)
                    except Exception as e:
                        logging.warning(f"Error clearing cgroup: {e}")
                
                try:
                    self.cgroup_path.rmdir()
                except Exception as e:
                    logging.warning(f"Could not remove cgroup: {e}")
            
            # Save final state
            save_yaml(HABIT_PATH, self.habits)
            PID_PATH.unlink(missing_ok=True)
            
            logging.info("Shutdown complete")
        
        atexit.register(cleanup)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda *args: sys.exit(0))

# =========================
# Entry Point
# =========================
if __name__ == "__main__":
    require_root()
    ProcessSentry().monitor()
