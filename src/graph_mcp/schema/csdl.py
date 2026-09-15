"""Entity schema from the CSDL metadata document, for graph_describe_type.

Before a model can write a correct ``$select`` or ``$filter`` it has to know
what properties exist and what they are called. Guessing produces a 400 that
does not name the offending property, so this is the tool that prevents a whole
class of retry loops.

The CSDL (``https://graph.microsoft.com/v1.0/$metadata``, 1.8 MB, no auth
required) is the authoritative source: 1,223 entity types, 1,810 complex types,
876 enums. Types inherit -- ``user`` derives from ``directoryObject`` -- so
properties are resolved up the base-type chain, otherwise ``id`` appears to be
missing from every directory type.
"""

from __future__ import annotations

import difflib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

EDM_NS = {"edm": "http://docs.oasis-open.org/odata/ns/edm"}
_COLLECTION_PREFIX = "Collection("


@dataclass
class Property:
    name: str
    type: str
    nullable: bool = True
    is_collection: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "is_collection": self.is_collection,
        }


@dataclass
class TypeInfo:
    name: str
    kind: str  # entity | complex | enum
    base_type: str = ""
    properties: list[Property] = field(default_factory=list)
    navigation_properties: list[Property] = field(default_factory=list)
    members: list[str] = field(default_factory=list)  # enums only

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "kind": self.kind,
            "base_type": self.base_type,
            "properties": [p.to_dict() for p in self.properties],
            "navigation_properties": [p.to_dict() for p in self.navigation_properties],
        }
        if self.members:
            out["members"] = self.members
        return out


def _split_type(raw: str) -> tuple[str, bool]:
    if raw.startswith(_COLLECTION_PREFIX) and raw.endswith(")"):
        return raw[len(_COLLECTION_PREFIX):-1], True
    return raw, False


def _canonical(raw: str) -> str:
    """'graph.user' and 'microsoft.graph.user' are the same type."""
    return raw.replace("graph.", "microsoft.graph.", 1) if raw.startswith("graph.") else raw


class TypeIndex:
    """Lazily parsed view over the CSDL document."""

    def __init__(self, path: Path):
        self.path = path

    @cached_property
    def types(self) -> dict[str, TypeInfo]:
        root = ET.parse(self.path).getroot()
        found: dict[str, TypeInfo] = {}
        for schema in root.iter(f"{{{EDM_NS['edm']}}}Schema"):
            namespace = schema.get("Namespace", "")
            for kind, tag in (("entity", "EntityType"), ("complex", "ComplexType")):
                for element in schema.findall(f"edm:{tag}", EDM_NS):
                    info = self._read_structured(element, namespace, kind)
                    found[info.name.lower()] = info
            for element in schema.findall("edm:EnumType", EDM_NS):
                name = f"{namespace}.{element.get('Name')}"
                found[name.lower()] = TypeInfo(
                    name=name, kind="enum",
                    members=[m.get("Name", "") for m in element.findall("edm:Member", EDM_NS)],
                )
        return found

    def _read_structured(self, element, namespace: str, kind: str) -> TypeInfo:
        info = TypeInfo(
            name=f"{namespace}.{element.get('Name')}",
            kind=kind,
            base_type=_canonical(element.get("BaseType", "")),
        )
        for prop in element.findall("edm:Property", EDM_NS):
            type_name, is_collection = _split_type(prop.get("Type", ""))
            info.properties.append(Property(
                name=prop.get("Name", ""), type=_canonical(type_name),
                nullable=prop.get("Nullable", "true") != "false",
                is_collection=is_collection,
            ))
        for nav in element.findall("edm:NavigationProperty", EDM_NS):
            type_name, is_collection = _split_type(nav.get("Type", ""))
            info.navigation_properties.append(Property(
                name=nav.get("Name", ""), type=_canonical(type_name),
                nullable=nav.get("Nullable", "true") != "false",
                is_collection=is_collection,
            ))
        return info

    def get(self, name: str) -> TypeInfo | None:
        key = _canonical(name).lower()
        if (found := self.types.get(key)) is not None:
            return found
        # Accept a bare leaf name ("user" for "microsoft.graph.user").
        if "." not in name:
            return self.types.get(f"microsoft.graph.{name}".lower())
        return None

    def resolve(self, name: str, *, max_depth: int = 10) -> TypeInfo | None:
        """Type with inherited properties folded in, base-first.

        Without this, 'user' appears to have no 'id' -- it is declared on
        directoryObject.
        """
        info = self.get(name)
        if info is None or info.kind == "enum":
            return info

        chain: list[TypeInfo] = []
        current: TypeInfo | None = info
        seen: set[str] = set()
        while current is not None and len(chain) < max_depth:
            if current.name.lower() in seen:
                break
            seen.add(current.name.lower())
            chain.append(current)
            current = self.get(current.base_type) if current.base_type else None

        merged = TypeInfo(name=info.name, kind=info.kind, base_type=info.base_type)
        by_name: dict[str, Property] = {}
        nav_by_name: dict[str, Property] = {}
        for entry in reversed(chain):  # base first so subtypes override
            for prop in entry.properties:
                by_name[prop.name] = prop
            for nav in entry.navigation_properties:
                nav_by_name[nav.name] = nav
        merged.properties = sorted(by_name.values(), key=lambda p: p.name)
        merged.navigation_properties = sorted(nav_by_name.values(), key=lambda p: p.name)
        return merged

    def search(self, term: str, limit: int = 10) -> list[str]:
        """Matching type names: exact leaf, then substring, then near spellings.

        The fuzzy pass matters because this feeds the "did you mean" on an
        unknown type, and a typo ("mesage") is a substring of nothing.
        """
        term = term.lower()
        exact = [t.name for k, t in self.types.items() if k.rsplit(".", 1)[-1] == term]
        partial = sorted(
            (t.name for k, t in self.types.items() if term in k and t.name not in exact),
            key=len,
        )
        results = exact + partial
        if len(results) < limit:
            leaves = {k.rsplit(".", 1)[-1]: t.name for k, t in self.types.items()}
            close = difflib.get_close_matches(term, leaves, n=limit, cutoff=0.7)
            results += [leaves[c] for c in close if leaves[c] not in results]
        return results[:limit]
