"""链接规范化与正文中的安全可点击范围。"""

import pytest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" HTTPS://Example.COM:443 ", "https://example.com/"),
        ("http://EXAMPLE.com:80/a?x=1#first", "http://example.com/a?x=1#first"),
        ("https://例子.测试/路径?x=1", "https://xn--fsqu00a.xn--0zwm56d/路径?x=1"),
    ],
)
def test_normalize_url_accepts_explicit_web_urls(raw, expected):
    from pc.services.links import normalize_url

    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["javascript:alert(1)", "example.com", "https://u:p@example.com", "https://example.com:99999", "https://example.com/a b", "https://", "https://example.com/%zz", "https://[:::]/", "https://[::1]:/", "https://example.com/\u202eevil", "http://example.com:８０", "http://[::1]:٨٠"])
def test_normalize_url_rejects_unsafe_or_ambiguous_urls(raw):
    from pc.services.links import normalize_url

    with pytest.raises(ValueError):
        normalize_url(raw)
