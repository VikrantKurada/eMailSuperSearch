import re
from itertools import pairwise

import pytest
from helpers import LONG_TEXT_3000_WORDS, LONG_TEXT_30000_WORDS
from tokenizers import Tokenizer

from emailsupersearch.index.embedder import tokenizer_path
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


def test_token_count_is_that_of_the_full_chunk_text(chunker):
    import emailsupersearch_models_en as models

    tok = Tokenizer.from_file(str(tokenizer_path(models.embedding_dir())))
    tok.no_truncation()  # the shipped file truncates and pads to 128
    tok.no_padding()
    out = chunker.chunk(LONG_TEXT_3000_WORDS, "From a on 2026-03-01: s [body]")
    assert [c.idx for c in out] == list(range(len(out)))
    assert all(c.token_count == len(tok.encode(c.text, add_special_tokens=False).ids) for c in out)


def test_overlong_context_line_never_pushes_a_chunk_past_hard_max(chunker, chunker_ws):
    for c in (chunker, chunker_ws):
        out = c.chunk(LONG_TEXT_3000_WORDS, "RE: " + "budget " * 400)
        assert len(out) > 5 and all(x.token_count <= 256 for x in out)


@pytest.mark.parametrize("text", ["word " * 700, "=" * 3000, "Lead in. " + "9 " * 700 + "tail."])
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
