"""Keep sidecar tests off the developer's ~/.gradia memory bus."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="gradia-home-")
os.environ.setdefault("GRADIA_HOME", _HOME)
atexit.register(shutil.rmtree, _HOME, True)
