"""This module is `wf.adapters.model.recording` under its old name."""

import sys

from wf.adapters.model import recording as _impl

sys.modules[__name__] = _impl
