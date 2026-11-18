"""This module is `wf.adapters.model.claude_code` under its old name."""

import sys

from wf.adapters.model import claude_code as _impl

sys.modules[__name__] = _impl
