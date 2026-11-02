import asyncio

import pytest

from app.config import load_settings
from app.llm import safe_json_parse
from app.pipeline import finalize_report, normalize_citations
from app.tools import WebTools, check_url_safety


# ---------------------------------------------------------------- safe_json_parse

def test_safe_json_parse_plain_and_fenced():
    assert safe_json_parse('{"a": 1}') == {"a": 1}
    assert safe_json_parse('```json\n{"a": 2}\n```') == {"a": 2}
    assert safe_json_parse('متن اضافه {"a": 3} پایان') == {"a": 3}


def test_safe_json_parse_strips_think_block():
    text = '<think>باید فکر کنم {"x": 0}</think>\n{"supported": true, "confidence": 0.9}'
    assert safe_json_parse(text) == {"supported": True, "confidence": 0.9}


def test_safe_json_parse_failure_returns_none():
    assert safe_json_parse("این اصلاً JSON نیست") is None
    assert safe_json_parse("") is None
    assert safe_json_parse('{"a": ') is None


def test_safe_json_parse_prefers_object_over_stray_brackets():
    assert safe_json_parse('نتیجه [1] این است: {"ok": true}') == {"ok": True}


# ---------------------------------------------------------------- SSRF

@pytest.mark.parametrize("url", [
    "http://localhost/admin",
    "http://127.0.0.1:8000/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]/",
    "http://2130706433/",          # decimal form of 127.0.0.1
    "http://0x7f.0.0.1/",
    "http://user@127.0.0.1/",
    "file:///etc/passwd",
    "ftp://example.com/file",
    "http://printer.local/",
])
def test_ssrf_blocked(url):
    safe, _reason = check_url_safety(url, resolve=False)
    assert safe is False


def test_ssrf_allows_public_ip_literal():
    assert check_url_safety("https://8.8.8.8/", resolve=False)[0] is True


def test_read_webpage_blocks_private_address_without_raising():
    tools = WebTools(load_settings())
    result = asyncio.run(tools.read_webpage("http://127.0.0.1/"))
    assert result["ok"] is False and result["code"] == "blocked"


# ---------------------------------------------------------------- report helpers

def test_normalize_citations_drops_unknown_numbers():
    assert normalize_citations("الف [1] ب [3] ج [1, 2]", 2) == "الف [۱] ب  ج [۱][۲]"


def test_finalize_report_requires_citation_and_appends_sources():
    sources = [{"n": 1, "url": "https://example.com/a", "title": "نمونه"}]
    body = "# عنوان\n## خلاصه\n" + "این یک جمله‌ی نمونه با ارجاع است [1]. " * 4
    report = finalize_report(body, "موضوع", sources, [])
    assert report is not None and "## منابع" in report and "[۱]" in report
    assert finalize_report("# عنوان\n" + "متن بدون ارجاع. " * 10, "موضوع", sources, []) is None
