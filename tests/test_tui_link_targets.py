import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontends"))
from tuiapp_v2 import HardBreakMarkdown


def links(markup, root):
    parsed = HardBreakMarkdown(markup, link_root=root).parsed
    found = []
    pending = list(parsed)
    while pending:
        token = pending.pop(0)
        if token.type == "link_open":
            found.append(token.attrs.get("href"))
        pending[0:0] = token.children or []
    return found


def test_plain_web_url_is_clickable_and_keeps_balanced_parentheses(tmp_path):
    assert links("See https://example.org/a(b).", tmp_path) == [
        "https://example.org/a(b)"
    ]


def test_existing_relative_file_target_resolves_to_parent_directory(tmp_path):
    target = tmp_path / "real file.txt"
    target.write_text("ok", encoding="utf-8")
    assert links("[open](real%20file.txt)", tmp_path) == [tmp_path.as_uri()]


def test_existing_relative_directory_target_remains_navigable(tmp_path):
    target = tmp_path / "folder"
    target.mkdir()
    assert links("[open](folder)", tmp_path) == [target.as_uri()]


def test_missing_relative_path_has_no_ctrl_click_target(tmp_path):
    assert links("[fake](missing.txt)", tmp_path) == [""]


def test_unsafe_scheme_is_not_rendered_as_link(tmp_path):
    assert links("[bad](javascript:alert(1))", tmp_path) == []


def test_inline_code_url_is_not_autolinked(tmp_path):
    assert links("`https://example.org`", tmp_path) == []
