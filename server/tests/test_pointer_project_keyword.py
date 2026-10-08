'A pointer call names its project positionally or as `project=`; both must scope it.'
from agent_context.refs import _scan_pointers


def test_positional_project_is_read():
    assert _scan_pointers('get_doc("pins.md", "example-api")') == [("doc", "pins.md", "example-api")]


def test_keyword_project_is_read():
    assert _scan_pointers('get_doc("pins.md", project="example-api")') == [
        ("doc", "pins.md", "example-api")]
    assert _scan_pointers("get_memory('slug', project = 'example-app')") == [
        ("memory", "slug", "example-app")]


def test_keyword_project_is_read_in_generic_form():
    assert _scan_pointers('get_entity("script", "name", project="example-api")') == [
        ("script", "name", "example-api")]


def test_no_project_stays_none():
    assert _scan_pointers('get_doc("pins.md")') == [("doc", "pins.md", None)]
    assert _scan_pointers('get_doc("pins.md", section="Rules")') == [("doc", "pins.md", None)]
