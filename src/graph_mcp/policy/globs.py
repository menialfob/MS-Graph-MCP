"""Segment-wise glob matching for Graph path templates.

Normalised paths keep ``{}`` as a literal placeholder segment, so patterns read
naturally in config:

    /me/**                  /me and everything under it
    /users/{}/messages      exactly that template
    /users/{}/*             exactly one more segment, whatever it is

Matching is case-insensitive. Normalised Graph paths are lowercased, but
profiles are written in the camelCase spelling people actually read in the
docs (``/deviceManagement/**``). A case-sensitive matcher silently fails to
match every one of those -- and a scope rule that silently does nothing is
worse than one that errors, so both sides are folded here.

Convention: ``**`` matches ZERO or more segments, so ``/sites/**`` covers
``/sites`` itself as well as everything below it. This differs from git's
pathspec rule, and is chosen deliberately -- profiles would otherwise have to
list ``/sites`` and ``/sites/**`` separately for every resource, which is
tedious and easy to get wrong in a config file that decides what an assistant
can see.
"""

from __future__ import annotations

from fnmatch import fnmatchcase


def match(pattern: str, path: str) -> bool:
    pat = [s for s in pattern.strip("/").lower().split("/") if s != ""]
    seg = [s for s in path.strip("/").lower().split("/") if s != ""]
    return _match(pat, seg)


def _match(pat: list[str], seg: list[str]) -> bool:
    if not pat:
        return not seg
    if pat[0] == "**":
        if len(pat) == 1:
            return True
        # Try every split point for the remainder of the pattern.
        return any(_match(pat[1:], seg[i:]) for i in range(len(seg) + 1))
    if not seg:
        return False
    if pat[0] == "{}" and seg[0] == "{}":
        return _match(pat[1:], seg[1:])
    if not fnmatchcase(seg[0], pat[0]):
        return False
    return _match(pat[1:], seg[1:])


def match_any(patterns: list[str], path: str) -> bool:
    return any(match(p, path) for p in patterns)
