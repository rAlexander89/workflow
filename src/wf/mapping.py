"""Automatic mapping from internal stages to a project's Jira status names.

The rules live in `wf.domain.board.stages`; this module keeps the old import path.
Stage owners (which agent acts in a stage) live in `wf.domain.delivery.routing`.
"""

from wf.domain.board.stages import (  # noqa: F401
    STAGE_PATTERNS, STAGES, StageRow, map_project, normalize, stages_for,
)
