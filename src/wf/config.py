"""This module is `wf.adapters.config.workspace` under its old name; the code moved with the architecture."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("wf.adapters.config.workspace")
