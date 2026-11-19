"""
Griffin Hub distro module: unknown / unsupported distribution (fallback)

Used when no other module matches (for example Void, Alpine, Gentoo, NixOS).
Nothing is installed automatically; every hub shows a clear explanation and
you can still use the Plugins window to add a module for your distribution.
"""

from gh_distro_api import DistroModule, Resolved


class Module(DistroModule):
    id = "generic"
    name = "Generic (no automatic installs)"
    version = "1.0.0"
    description = "Fallback when no distro module matches: detection only, no package management."
    pkg_manager = "unknown"
    family = "unknown"
    ids = ()

    @classmethod
    def match_score(cls, osr):
        return 1   # always matches, but loses against any real module

    def is_installed(self, pkg: str) -> bool:
        return False

    def pm_functions(self) -> str:
        return ""

    def build_deps_lines(self) -> str:
        return 'echo "No supported package manager was detected."; exit 1'

    def resolve(self, key: str) -> Resolved:
        return Resolved(ok=False, reason="Griffin Hub has no distro module for this system yet, so it cannot "
                                         "install this automatically. Install the packages with your distribution's "
                                         "tools, or add a distro module in the Plugins window.")

    def setup_install_argv(self, packages):
        return []

    def setup_packages(self):
        return []
