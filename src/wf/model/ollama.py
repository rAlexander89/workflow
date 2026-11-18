"""This module is `wf.adapters.model.ollama` under its old name."""

import sys

from wf.adapters.model import ollama as _impl

sys.modules[__name__] = _impl
