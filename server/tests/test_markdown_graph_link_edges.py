'Markdown destinations with titles and non-file schemes stay accurate.'

from agent_context.refs import _scan_markdown_doc_links


def test_local_doc_link_with_title_and_anchor_is_recognized():
    assert _scan_markdown_doc_links('[Route](route.md#Safety "Docs")', "features/README.md") == [
        ("features/route.md", "Safety")]


def test_uri_schemes_never_become_local_doc_paths():
    body = "[code](javascript:route.md) [ftp](ftp:route.md) [custom](app:route.md)"
    assert _scan_markdown_doc_links(body, "features/README.md") == []
