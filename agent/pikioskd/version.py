"""Single source of the agent's version.

Read by the HTTP surface (``deviceInfo.agentVersion``), by ``--version`` and by
the integration's update entity. It lives in its own module so importing it
costs nothing — ``setup.py``-free packaging means the installer reads it with a
grep and the tests read it with an import, and a version in two places is a
version that disagrees with itself on exactly the release nobody checks.
"""

from __future__ import annotations

__version__ = "1.1.0"
