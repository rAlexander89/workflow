"""This module is `wf.adapters.workflow.langgraph` under its old name; the code moved with the architecture."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("wf.adapters.workflow.langgraph")
