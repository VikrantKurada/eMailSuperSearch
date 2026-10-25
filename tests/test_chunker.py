import random
import re
import string
import time
from itertools import pairwise

import pytest
from helpers import LONG_TEXT_3000_WORDS, LONG_TEXT_30000_WORDS
from tokenizers import Tokenizer

from emailsupersearch.ingest.chunker import Chunker, ChunkText, context_line_for


def test_chunks_respect_token_bounds(chunker):
    out = chunker.chunk(LONG_TEXT_3000_WORDS, "From a on 2026-03-01: s [body]")
    assert len(out) > 5 and all(c.token_count <= 256 for c in out) and all(c.text.startswith("From a on") for c in out)
def test_chunks_overlap(chunker):
    out = chunker.chunk(LONG_TEXT_3000_WORDS, "ctx"); tail = " ".join(out[0].text.split()[-5:]); assert tail in out[1].text
def test_short_text_is_one_chunk(chunker): assert len(chunker.chunk("Short note.", "ctx")) == 1
def test_long_text_yields_many_chunks(chunker): assert len(chunker.chunk(LONG_TEXT_30000_WORDS, "ctx")) > 64
def test_empty_text_gives_no_chunks(chunker): assert chunker.chunk("   ", "ctx") == []
def test_whitespace_chunker_works_without_model(chunker_ws): assert len(chunker_ws.chunk(LONG_TEXT_3000_WORDS, "ctx")) > 5
def test_context_line_format(): assert context_line_for("Priya Nair", 1773705600, "RE: Acme", "body") == "From Priya Nair on 2026-03-17: RE: Acme [body]"


def test_context_line_without_a_date():
    assert context_line_for("Priya Nair", None, "RE: Acme", "body") == "From Priya Nair on unknown date: RE: Acme [body]"


def test_chunk_text_is_context_line_newline_piece(chunker):
    out = chunker.chunk("First point.\n\nSecond point.", "ctx")
    assert out == [ChunkText(0, "ctx\nFirst point.\n\nSecond point.", out[0].token_count)]


_MIXED_BLOB = "".join(random.Random(5).choices(string.ascii_letters + string.digits + "+/=-.", k=4000))
_TEXTS = {
    "prose": LONG_TEXT_3000_WORDS,
    "unpunctuated": " ".join(LONG_TEXT_3000_WORDS.replace(".", "").split()),
    "blob": "Intro sentence. " + _MIXED_BLOB + " Closing sentence.",
    "rule": "Intro sentence. " + "=" * 3000,
    "cjk": "漢字" * 800,
}


@pytest.mark.parametrize("name", list(_TEXTS))
@pytest.mark.parametrize("budget", [(220, 256), (12, 40)], ids=["default", "tiny"])
def test_token_count_is_that_of_the_full_chunk_text(tokenizer_file, name, budget):
    tok = Tokenizer.from_file(str(tokenizer_file))
    tok.no_truncation()  # the shipped file truncates and pads to 128
    tok.no_padding()
    chunker = Chunker(tokenizer_file, target_tokens=budget[0], hard_max_tokens=budget[1])
    out = chunker.chunk(_TEXTS[name], "From a on 2026-03-01: s [body]")
    assert [c.idx for c in out] == list(range(len(out)))
    assert all(c.token_count == len(tok.encode(c.text, add_special_tokens=False).ids) for c in out)
    assert all(c.token_count <= budget[1] for c in out)


def test_overlong_context_line_never_pushes_a_chunk_past_hard_max(chunker, chunker_ws):
    for c in (chunker, chunker_ws):
        out = c.chunk(LONG_TEXT_3000_WORDS, "RE: " + "budget " * 400)
        assert len(out) > 5 and all(x.token_count <= 256 for x in out)


@pytest.mark.parametrize(
    "text",
    ["word " * 700, "=" * 3000, "Lead in. " + "9 " * 700 + "tail."],
    ids=["words", "rule", "digits-between-sentences"],
)
def test_oversized_sentences_are_split_to_fit(chunker, text):
    out = chunker.chunk(text, "ctx")
    assert len(out) > 1 and all(c.token_count <= 256 for c in out)


def test_oversized_sentence_is_split_without_a_model(chunker_ws):
    out = chunker_ws.chunk("word " * 700, "ctx")
    assert len(out) > 1 and all(c.token_count <= 256 for c in out)


def test_large_units_do_not_repeat_chunks(chunker_ws):
    text = " ".join(" ".join(f"w{i}x{j}" for j in range(150)) + "." for i in range(6))
    out = chunker_ws.chunk(text, "ctx")
    assert len(out) == 6 and len({c.text for c in out}) == 6


def test_every_chunk_adds_text_beyond_the_previous_one(chunker):
    out = chunker.chunk(LONG_TEXT_3000_WORDS, "ctx")
    assert all(b.text.split("\n", 1)[1] not in a.text for a, b in pairwise(out))


def test_custom_budget_is_honoured(chunker_ws):
    small = Chunker(None, target_tokens=40, overlap_ratio=0.25, hard_max_tokens=64)
    out = small.chunk(LONG_TEXT_3000_WORDS, "ctx")
    assert len(out) > 40 and all(c.token_count <= 64 for c in out)
    assert len(out) > len(chunker_ws.chunk(LONG_TEXT_3000_WORDS, "ctx"))


def test_overlap_is_the_last_sentences_that_fit_under_the_ratio(chunker_ws):
    # 10-word sentences, so the default 15% of 220 tokens allows three of them (30 < 33, 40 is not).
    text = " ".join(f"S{i} " + "x " * 8 + "x." for i in range(100))
    first, second = (list(map(int, re.findall(r"S(\d+)", c.text))) for c in chunker_ws.chunk(text, "ctx")[:2])
    assert second[:3] == first[-3:] and second[3] == first[-1] + 1


@pytest.mark.parametrize(
    ("ts", "date"),
    [(0, "1970-01-01"), (-2208988800, "1900-01-01"), (253402297200, "9999-12-31"), (253402300800, "unknown date"), (10**18, "unknown date")],
)
def test_context_line_dates_beyond_the_platform_range(ts, date):
    assert context_line_for("a", ts, "s", "body") == f"From a on {date}: s [body]"


_UNPUNCTUATED = " ".join(LONG_TEXT_3000_WORDS.replace(".", "").split())


def _overlap_words(a: ChunkText, b: ChunkText) -> list[str]:
    """The longest run of words that ends one chunk's piece and starts the next one's."""
    wa, wb = a.text.split("\n", 1)[1].split(), b.text.split("\n", 1)[1].split()
    return next(wa[-k:] for k in range(min(len(wa), len(wb)), 0, -1) if wa[-k:] == wb[:k])


@pytest.mark.parametrize("n_words", [221, 2000, 3000])
@pytest.mark.parametrize("fixture", ["chunker", "chunker_ws"])
def test_text_without_sentence_breaks_overlaps_and_leaves_no_orphans(request, fixture, n_words):
    c = request.getfixturevalue(fixture)
    out = c.chunk(" ".join(_UNPUNCTUATED.split()[:n_words]), "ctx")
    assert len(out) > 1
    assert all(x.token_count - 1 >= 110 for x in out[:-1])  # the 1 is the "ctx" line; 110 is half of 220
    for a, b in pairwise(out):
        shared = c.chunk(" ".join(_overlap_words(a, b)), "")[0].token_count
        assert int(0.15 * 220) - 5 <= shared <= int(0.15 * 220)


@pytest.mark.parametrize("kind", ["base64", "rule"])
def test_huge_unbroken_token_is_split_in_near_linear_time(chunker, kind):
    blob = (
        "".join(random.Random(3).choices(string.ascii_letters + string.digits + "+/", k=1_000_000))
        if kind == "base64"
        else "=" * 200_000
    )
    start = time.perf_counter()
    out = chunker.chunk(blob, "ctx")
    elapsed = time.perf_counter() - start
    # The quadratic splitter this guards against took over 40 s on the 1 MB blob on a fast desktop; the linear
    # one takes about 1.3 s there and about 3 s on a shared CI runner, so 20 s separates them on any machine.
    assert elapsed < 20 and len(out) > 100 and all(c.token_count <= 256 for c in out)
