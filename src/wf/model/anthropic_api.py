"""This module is `wf.adapters.model.anthropic_api` under its old name."""

import sys

from wf.adapters.model import anthropic_api as _impl

sys.modules[__name__] = _impl
