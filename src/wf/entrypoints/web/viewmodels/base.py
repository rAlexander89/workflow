"""What every view model provides."""

from __future__ import annotations

import dataclasses
from typing import Any

from wf.bootstrap import Container


class ViewModel:
    """A screen's state. Subclasses set `template` and, for a page, `active` (the navigation tab).

    A template is bound to its view model: every public field becomes a name in the template, and `vm` is the
    view model itself, for properties and methods.
    """

    template: str = ""
    active: str = ""

    def bindings(self) -> dict[str, Any]:
        fields = ({f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
                  if dataclasses.is_dataclass(self) else {k: v for k, v in vars(self).items() if not k.startswith("_")})
        return {**fields, "vm": self, "active": self.active}


class Screen(ViewModel):
    """A view model that loads its state from a container's use cases."""

    def __init__(self, container: Container):
        self.container = container
