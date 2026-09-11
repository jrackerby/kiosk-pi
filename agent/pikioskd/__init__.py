"""pikioskd — a Fully-Kiosk-shaped kiosk agent for Raspberry Pi wall panels.

STANDARD LIBRARY ONLY, AND THAT IS A REQUIREMENT RATHER THAN A PREFERENCE. A
panel is headless with no input devices, so a failed dependency install during
an upgrade is a black screen recoverable only over SSH. Debian enforces PEP 668,
which makes every dependency a venv, a wheel build for ARM and a provisioning
step that can fail. Nothing here imports anything that is not in CPython's
standard library, and ``agent/tests/test_stdlib_only.py`` fails the build if
that ever stops being true.
"""

from __future__ import annotations

from .version import __version__

__all__ = ["__version__"]
