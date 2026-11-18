"""One agent run through Claude Code: `claude -p` started in a ticket's worktree.

The implementation is the Claude Code adapter, `wf.adapters.runner.claude_code`. This module is the same
module under its old name, so existing imports and test patches keep working.
"""

import sys

from wf.adapters.runner import claude_code as _impl

sys.modules[__name__] = _impl
