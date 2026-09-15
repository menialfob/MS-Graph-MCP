"""Parser tests, including regressions for every bug the spike uncovered."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pipeline.join import alias_keys, join_key, normalize_path
from pipeline.parse_docs import parse_page
from pipeline.parse_openapi import parse, unquote
from pipeline.parse_permissions import parse_file

SPEC = textwrap.dedent("""\
    openapi: 3.0.1
    info:
      title: Microsoft Graph
    paths:
      /me:
        description: Provides operations to manage the user singleton.
        get:
          tags:
            - users.user
          summary: Get user
          operationId: me.GetUser
          responses:
            2XX:
              $ref: '#/components/responses/microsoft.graph.user'
      '/users/{user-id}/messages':
        get:
          tags:
            - users.message
          summary: Get messages from users
          description: 'The messages in a mailbox or folder. It''s read-only.'
          operationId: users.ListMessages
          parameters:
            - $ref: '#/components/parameters/top'
            - name: $select
              in: query
          responses:
            2XX:
              $ref: '#/components/responses/microsoft.graph.messageCollectionResponse'
          x-ms-pageable:
            nextLinkName: '@odata.nextLink'
        post:
          summary: Create new navigation property
          operationId: users.CreateMessages
          requestBody:
            content:
              application/json:
                schema:
                  $ref: '#/components/schemas/microsoft.graph.message'
          responses:
            2XX:
              $ref: '#/components/responses/error'
    """)


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    path = tmp_path / "openapi.yaml"
    path.write_text(SPEC, encoding="utf-8")
    return path


def test_parses_every_operation(spec):
    ops = list(parse(spec))
    assert [o.key for o in ops] == [
        "GET /me",
        "GET /users/{user-id}/messages",
        "POST /users/{user-id}/messages",
    ]


def test_extracts_operation_fields(spec):
    op = next(o for o in parse(spec) if o.key == "GET /users/{user-id}/messages")
    assert op.operation_id == "users.ListMessages"
    assert op.summary == "Get messages from users"
    assert op.description.startswith("The messages in a mailbox")
    assert op.tags == ["users.message"]
    assert op.query_params == ["top", "$select"]
    assert op.path_params == ["user-id"]
    assert op.response_type == "microsoft.graph.message"
    assert op.returns_collection


def test_request_body_type_and_error_responses_ignored(spec):
    op = next(o for o in parse(spec) if o.method == "post")
    assert op.request_type == "microsoft.graph.message"
    assert op.response_type == ""  # the only response was the shared error ref


def test_unquote_handles_escaped_single_quotes():
    assert unquote("'It''s read-only.'") == "It's read-only."
    assert unquote("plain value") == "plain value"


class TestNormalizePath:
    def test_parameter_names_are_discarded(self):
        # The docs and the spec name the same parameter differently; only the
        # position is structurally meaningful.
        assert normalize_path("/users/{id | userPrincipalName}/messages") == (
            normalize_path("/users/{user-id}/messages")
        )

    def test_strips_version_prefix_and_query_string(self):
        assert normalize_path("/v1.0/users/?$filter=userType eq 'guest'") == "/users"

    def test_strips_odata_function_call_syntax(self):
        # Regression: the spec writes delta() and the docs write delta, which
        # silently broke the join for 599 v1.0 paths.
        assert normalize_path("/me/messages/delta()") == "/me/messages/delta"
        assert normalize_path(
            "/applications/{application-id}/federatedIdentityCredentials(name='{n}')"
        ) == "/applications/{}/federatedidentitycredentials"

    def test_strips_odata_type_cast_segments(self):
        assert normalize_path("/me/microsoft.graph.sendMail") == "/me/sendmail"


class TestAliasKeys:
    def test_me_maps_to_users_template(self):
        # Regression: the docs list /users/{id}/calendarView but not the bare
        # /me/calendarView, which is the most natural way to ask for it.
        assert alias_keys("GET", "/me/calendarView") == ["get /users/{}/calendarview"]

    def test_users_template_maps_back_to_me(self):
        assert alias_keys("GET", "/users/{user-id}/messages") == ["get /me/messages"]

    def test_unrelated_paths_have_no_alias(self):
        assert alias_keys("GET", "/groups/{group-id}/members") == []

    def test_does_not_alias_unrelated_prefix(self):
        assert alias_keys("GET", "/mentions") == []


DOC_PAGE = textwrap.dedent("""\
    ---
    title: "List messages"
    description: "Get the messages in the signed-in user's mailbox."
    ---

    # List messages

    Namespace: microsoft.graph

    Get the messages in the signed-in user's mailbox, including Deleted Items.

    ## Permissions

    [!INCLUDE [permissions-table](../includes/permissions/user-list-messages-permissions.md)]

    ## HTTP request

    ```http
    GET /me/messages
    GET /users/{id | userPrincipalName}/messages
    ```

    ## Examples

    ```http
    GET https://graph.microsoft.com/v1.0/me/messages?$select=sender
    ```
    """)


class TestDocParsing:
    @pytest.fixture
    def page(self, tmp_path: Path):
        path = tmp_path / "user-list-messages.md"
        path.write_text(DOC_PAGE, encoding="utf-8")
        return parse_page(path)

    def test_reads_frontmatter(self, page):
        assert page.title == "List messages"
        assert page.description.startswith("Get the messages")

    def test_captures_templates_with_spaces_inside_braces(self, page):
        # Regression: a \S+ path pattern dropped every /users/{id | upn} form,
        # which is most of the non-/me surface.
        assert page.http_templates == [
            ("GET", "/me/messages"),
            ("GET", "/users/{id | userPrincipalName}/messages"),
        ]

    def test_ignores_request_urls_outside_the_http_request_section(self, page):
        # The Examples section holds full URLs that are not canonical templates.
        assert all("graph.microsoft.com" not in p for _, p in page.http_templates)

    def test_finds_permissions_include(self, page):
        assert page.permissions_include == "user-list-messages-permissions"

    def test_intro_skips_the_namespace_line(self, page):
        assert page.intro.startswith("Get the messages")


def test_permission_table_parsing(tmp_path: Path):
    path = tmp_path / "perms.md"
    path.write_text(textwrap.dedent("""\
        |Permission type|Least privileged|Higher privileged|
        |:---|:---|:---|
        |Delegated (work or school account)|Mail.ReadBasic|Mail.ReadWrite, Mail.Read|
        |Delegated (personal Microsoft account)|Not supported.|Not supported.|
        |Application|Mail.ReadBasic.All|Mail.Read|
        """), encoding="utf-8")
    perms = parse_file(path)
    assert perms.least["delegated_work"] == ["Mail.ReadBasic"]
    assert perms.higher["delegated_work"] == ["Mail.ReadWrite", "Mail.Read"]
    assert "delegated_personal" not in perms.least
    assert perms.supports_delegated


def test_join_key_is_method_plus_normalized_path():
    assert join_key("GET", "/v1.0/Me/Messages") == "get /me/messages"
