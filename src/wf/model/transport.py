"""This module is `wf.adapters.model.transport` under its old name."""

import sys

from wf.adapters.model import transport as _impl

sys.modules[__name__] = _impl
