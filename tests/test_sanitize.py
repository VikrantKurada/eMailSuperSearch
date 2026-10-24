"""The message viewer's HTML sanitiser: an allowlist rebuild, so only known tags and safe links come out."""

import pytest

from emailsupersearch.host.sanitize import sanitize_html

_LINK = 'rel="noopener noreferrer" target="_blank"'


def test_sanitize_drops_script_and_cid_images_keeps_links():
    out = sanitize_html(
        '<p onclick="x()">Hi <img src="cid:logo"><script>evil()</script>'
        '<a href="https://a.b" style="c">l</a><a href="javascript:x">j</a></p>'
    )
    assert "script" not in out and "cid:" not in out and 'href="https://a.b"' in out
    assert "onclick" not in out and "javascript" not in out
    assert out == f'<p>Hi <a href="https://a.b" {_LINK}>l</a><a>j</a></p>'


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param("<font><center><x-y>deep <u>text</u></x-y></center></font>", "deep <u>text</u>", id="nested-unknown"),
        pytest.param('<a href="  JaVaScRiPt:alert(1)">x</a>', "<a>x</a>", id="javascript-space-case"),
        pytest.param('<a href="java&#9;script:alert(1)">x</a>', "<a>x</a>", id="javascript-tab"),
        pytest.param('<a href="&#1;javascript:alert(1)">x</a>', "<a>x</a>", id="javascript-control"),
        pytest.param('<a href="data:text/html,&lt;b&gt;x">d</a>', "<a>d</a>", id="data-href"),
        pytest.param('<a href="VBScript:msgbox(1)">v</a>', "<a>v</a>", id="vbscript-href"),
        pytest.param('<a name="top" class="c">t</a>', "<a>t</a>", id="a-without-href"),
        pytest.param("<a href>t</a>", "<a>t</a>", id="a-empty-href"),
        pytest.param('<a href=" HTTPS://Example.com/x y ">e</a>', f'<a href="HTTPS://Example.com/x y" {_LINK}>e</a>',
                     id="https-trimmed"),
        pytest.param('<a href="mailto:a@b.c">m</a>', f'<a href="mailto:a@b.c" {_LINK}>m</a>', id="mailto"),
        pytest.param("<a href='https://a.b/?q=\"x\"&amp;y=1'>q</a>",
                     f'<a href="https://a.b/?q=&quot;x&quot;&amp;y=1" {_LINK}>q</a>', id="href-escaped"),
        pytest.param("<div><p><b>bold", "<div><p><b>bold</b></p></div>", id="unclosed"),
        pytest.param("<b><i>x</b>y</i></td>", "<b><i>x</i></b>y", id="misnested-and-stray"),
        pytest.param("a<!-- secret -->b<!--[if mso]><p>x</p><![endif]-->", "ab", id="comments"),
        pytest.param('<!DOCTYPE html><?xml version="1.0"?><html><body><P>x</P></body></html>', "<p>x</p>",
                     id="doctype-pi-wrapper"),
        pytest.param(
            "<svg><text>hid</text></svg><iframe src=x>in</iframe><object>ob</object><embed src=e>"
            "<noscript>ns</noscript><template>tp</template><style>p{}</style><script>s()</script>ok",
            "ok",
            id="dropped-with-content",
        ),
        pytest.param("<svg><svg></svg>still hidden</svg>shown", "shown", id="nested-dropped"),
        pytest.param('<td colspan="2" class="c" id="i" style="x" onmouseover="y()">c</td>', "<td>c</td>",
                     id="attributes-stripped"),
        pytest.param("a<br>b<br/>c</br><hr><img src=x><input value=v><form>f</form>", "a<br>b<br>cf",
                     id="void-and-dropped-tags"),
        pytest.param("&lt;script&gt; a &amp; b > c", "&lt;script&gt; a &amp; b &gt; c", id="text-escaped"),
    ],
)
def test_sanitize_edge_cases(html, expected):
    assert sanitize_html(html) == expected
