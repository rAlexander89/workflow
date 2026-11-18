"""This module is `wf.domain.delivery.github_webhook` under its old name; the code moved with the architecture."""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("wf.domain.delivery.github_webhook")
