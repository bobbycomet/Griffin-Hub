#!/bin/sh
# Launcher installed as /usr/bin/griffinhub by the .deb / .rpm / PKGBUILD packages.
exec python3 /usr/lib/griffinhub/griffinhub.py "$@"
