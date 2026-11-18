"""Every ticket has one branch, cut from the internal main. All roles work on it."""


def branch_for(key: str) -> str:
    return f"agent/{key}"
