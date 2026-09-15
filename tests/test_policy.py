"""Scope policy, curation and catalog tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from graph_mcp.policy.globs import match, match_any
from pipeline.catalog import build as build_catalog
from pipeline.curate import Profile, curate
from pipeline.parse_openapi import Operation


class TestGlobs:
    def test_double_star_matches_zero_or_more_segments(self):
        assert match("/sites/**", "/sites")
        assert match("/sites/**", "/sites/{}/lists")

    def test_single_star_matches_exactly_one_segment(self):
        assert match("/users/{}/*", "/users/{}/manager")
        assert not match("/users/{}/*", "/users/{}/a/b")

    def test_matching_is_case_insensitive(self):
        # Regression, and the nastiest bug in the spike: normalised paths are
        # lowercased while profiles are written in the camelCase spelling from
        # the docs. A case-sensitive matcher made every camelCase exclude a
        # silent no-op -- a scope rule that quietly does nothing.
        assert match("/deviceManagement/**", "/devicemanagement/manageddevices")
        assert match("**/managedDevices/**", "/me/manageddevices/{}")

    def test_parameter_placeholder_is_literal(self):
        assert match("/users/{}", "/users/{}")
        assert not match("/users/{}", "/users/alice")

    def test_match_any(self):
        assert match_any(["/groups/**", "/me/**"], "/me/messages")
        assert not match_any(["/groups/**"], "/me/messages")


PROFILE = textwrap.dedent("""\
    name: test
    description: fixture
    prune:
      max_path_params: 2
      drop_suffixes: ["/$count", "/$ref", "/$value"]
      drop_segments_containing: ["microsoft.graph."]
    include:
      - /me/**
      - /users/**
    exclude:
      - "**/managedDevices/**"
    read_only:
      - /users/**
    """)


@pytest.fixture
def profile(tmp_path: Path) -> Profile:
    path = tmp_path / "p.yaml"
    path.write_text(PROFILE, encoding="utf-8")
    return Profile.load(path)


def op(path: str, method: str = "get", **kw) -> Operation:
    return Operation(path=path, method=method, **kw)


class TestCuration:
    def test_keeps_in_scope_operations(self, profile):
        kept, _ = curate([op("/me/messages")], profile)
        assert [o.key for o in kept] == ["GET /me/messages"]

    def test_drops_count_ref_and_value_siblings(self, profile):
        ops = [op("/me/messages/$count"), op("/me/drive/$ref"), op("/me/photo/$value")]
        kept, stats = curate(ops, profile)
        assert kept == []
        assert stats.dropped_structural == 3

    def test_drops_deep_navigation_expansions(self, profile):
        deep = op("/me/a/{x-id}/b/{y-id}/c/{z-id}/d")
        kept, stats = curate([deep], profile)
        assert kept == []
        assert stats.dropped_structural == 1

    def test_drops_odata_cast_variants(self, profile):
        kept, _ = curate([op("/me/messages/microsoft.graph.eventMessage")], profile)
        assert kept == []

    def test_exclude_beats_include(self, profile):
        # /me/** includes it; **/managedDevices/** must still win.
        kept, stats = curate([op("/me/managedDevices")], profile)
        assert kept == []
        assert stats.dropped_excluded == 1

    def test_read_only_paths_reject_writes_but_keep_reads(self, profile):
        ops = [op("/users/{user-id}", "get"), op("/users/{user-id}", "patch")]
        kept, stats = curate(ops, profile)
        assert [o.method for o in kept] == ["get"]
        assert stats.dropped_read_only == 1

    def test_out_of_scope_paths_are_dropped(self, profile):
        # Neither included nor explicitly excluded: falls through the include
        # list. (A path matching an exclude rule is counted as excluded --
        # exclude is evaluated first.)
        kept, stats = curate([op("/groups/{group-id}")], profile)
        assert kept == []
        assert stats.dropped_not_included == 1


class TestCatalog:
    def test_collapses_odata_function_overloads(self):
        # Regression: GET /sites/{site-id}/getActivitiesByInterval() appears 24
        # times in v1.0, differing only by parameter signature. Left alone that
        # is 24 identical search results for one action.
        ops = [
            op("/sites/{site-id}/getActivitiesByInterval()",
               operation_id="sites.getActivitiesByInterval-4c35"),
            op("/sites/{site-id}/getActivitiesByInterval()",
               operation_id="sites.getActivitiesByInterval-ad27"),
        ]
        entries = build_catalog(ops, {}, {})
        assert len(entries) == 1
        assert entries[0].overloads == 2

    def test_entries_without_docs_are_not_indexed(self):
        entries = build_catalog([op("/me/messages")], {}, {})
        assert entries[0].indexed is False
