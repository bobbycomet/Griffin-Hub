#!/usr/bin/env python3
"""Write hicolor icons from griffinhub.png.   usage: make_icons.py <src.png> <outdir>  (needs PyQt6)"""
import os, sys
from PyQt6.QtGui import QImage
from PyQt6.QtCore import Qt
src, out = sys.argv[1], sys.argv[2]
img = QImage(src)
if img.isNull():
    sys.exit("cannot read " + src)
for size in (16, 24, 32, 48, 64, 128, 256, 512):
    d = os.path.join(out, f"{size}x{size}", "apps")
    os.makedirs(d, exist_ok=True)
    img.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(os.path.join(d, "griffinhub.png"))
print("icons written to", out)
