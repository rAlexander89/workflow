"""This module is `wf.adapters.env.local_app` under its old name; the code moved with the architecture."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("wf.adapters.env.local_app")
