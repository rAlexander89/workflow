"""This module is `wf.adapters.forge.forge` under its old name; the code moved with the architecture."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("wf.adapters.forge.forge")
