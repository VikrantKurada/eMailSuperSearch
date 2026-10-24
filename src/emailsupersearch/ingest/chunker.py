"""Chunker: cut a body or an attachment's text into overlapping pieces that fit the embedding model.

The int8 MiniLM truncates at 256 word pieces, so every text is cut into chunks of about 220 tokens.
Each chunk is ``context_line + "\\n" + piece``: the one-line context header says whose email the
piece came from, so a chunk from page 12 of an attachment still carries its sender, date and subject.

Pieces are packed greedily from whole sentences (paragraphs are split on blank lines, sentences on
``.``, ``!`` and ``?``). The next chunk begins with the last sentence or two of the previous one (when
those and the next new sentence fit together), so a fact that straddles a cut is whole in at least one
chunk. A sentence that cannot fit is cut into word runs, and a single word that cannot fit is cut into
character runs.

A token is a word piece when a tokenizer file is given, or else a whitespace-separated word (the
keyword-only install, which has no model).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tokenizers import Tokenizer

CHUNKER_VERSION = 1
# The pipeline embeds only this many chunks of one attachment; the chunker itself returns them all.
MAX_EMBEDDED_CHUNKS_PER_ATTACHMENT = 64

_SPECIAL_TOKENS = 2  # [CLS] and [SEP], which the model adds around every text
_MIN_PIECE_TOKENS = 16  # an over-long context line is trimmed to leave at least this much for content
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class ChunkText:
    idx: int
    text: str
    token_count: int


@dataclass(frozen=True)
class _Unit:
    """A sentence (or the part of one that fits) and what separates it from the unit before it."""

    sep: str
    text: str
    tokens: int


def context_line_for(sender: str, sent_ts: int | None, subject: str, part_label: str) -> str:
    """The header line of every chunk, e.g. ``From Priya Nair on 2026-03-17: RE: Acme [body]``."""
    date = "unknown date" if sent_ts is None else datetime.fromtimestamp(sent_ts, UTC).strftime("%Y-%m-%d")
    return f"From {sender} on {date}: {subject} [{part_label}]"


def _join(units: list[_Unit]) -> str:
    return units[0].text + "".join(u.sep + u.text for u in units[1:])


class Chunker:
    def __init__(
        self,
        tokenizer_path: Path | None,
        target_tokens: int = 220,
        overlap_ratio: float = 0.15,
        hard_max_tokens: int = 256,
    ) -> None:
        self._tokenizer: Tokenizer | None = None
        if tokenizer_path is not None:
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            # The shipped tokenizer.json truncates and pads to 128, which would flatten every count.
            self._tokenizer.no_truncation()
            self._tokenizer.no_padding()
        self._target = target_tokens
        self._overlap_limit = int(overlap_ratio * target_tokens)
        self._hard_max = hard_max_tokens

    def chunk(self, text: str, context_line: str) -> list[ChunkText]:
        """Cut ``text`` into chunks of ``context_line + "\\n" + piece``; blank text gives no chunks."""
        text = text.strip()
        if not text:
            return []
        context_line = self._fit_context(context_line)
        budget = max(1, min(self._target, self._hard_max - self._count(context_line) - _SPECIAL_TOKENS))
        units = self._units(text, budget)
        texts = [context_line + "\n" + _join(units[a:b]) for a, b in self._spans(units, budget)]
        return [ChunkText(i, t, n) for i, (t, n) in enumerate(zip(texts, self._count_many(texts)))]

    # --- token counting -------------------------------------------------------------------------

    def _count(self, text: str) -> int:
        return self._count_many([text])[0]

    def _count_many(self, texts: list[str]) -> list[int]:
        if self._tokenizer is None:
            return [len(t.split()) for t in texts]
        return [len(e.ids) for e in self._tokenizer.encode_batch(texts, add_special_tokens=False)]

    # --- splitting ------------------------------------------------------------------------------

    def _fit_context(self, context_line: str) -> str:
        """Drop trailing words of a context line so long that no room is left for the content."""
        room = self._hard_max - _SPECIAL_TOKENS - _MIN_PIECE_TOKENS
        if self._count(context_line) <= room:
            return context_line
        words = context_line.split()
        lo, hi = 0, len(words)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._count(" ".join(words[:mid])) <= room:
                lo = mid
            else:
                hi = mid - 1
        return " ".join(words[:lo])

    def _units(self, text: str, budget: int) -> list[_Unit]:
        """Sentences in order, each at most ``budget`` tokens; longer ones are cut into word runs."""
        sentences: list[tuple[str, str]] = []
        for p, paragraph in enumerate(_PARAGRAPH_BREAK.split(text)):
            first = True
            for sentence in _SENTENCE_END.split(paragraph.strip()):
                if sentence:
                    sentences.append(("\n\n" if p and first else " ", sentence))
                    first = False
        units: list[_Unit] = []
        for (sep, sentence), tokens in zip(sentences, self._count_many([s for _, s in sentences])):
            if tokens <= budget:
                units.append(_Unit(sep, sentence, tokens))
                continue
            for k, (part, part_tokens) in enumerate(self._split_to_fit(sentence, budget)):
                units.append(_Unit(sep if k == 0 else " ", part, part_tokens))
        return units

    def _split_to_fit(self, sentence: str, budget: int) -> list[tuple[str, int]]:
        """Cut an over-long sentence into runs of whole words of at most ``budget`` tokens each."""
        parts: list[tuple[str, int]] = []
        run: list[str] = []
        run_tokens = 0
        for word, tokens in zip(words := sentence.split(), self._count_many(words)):
            if tokens > budget:
                if run:
                    parts.append((" ".join(run), run_tokens))
                    run, run_tokens = [], 0
                parts.extend(self._split_word(word, budget))
                continue
            if run_tokens + tokens > budget:
                parts.append((" ".join(run), run_tokens))
                run, run_tokens = [], 0
            run.append(word)
            run_tokens += tokens
        if run:
            parts.append((" ".join(run), run_tokens))
        return parts

    def _split_word(self, word: str, budget: int) -> list[tuple[str, int]]:
        """Cut one word that is over ``budget`` tokens (a rule of dashes, a CJK run) into character runs."""
        parts: list[tuple[str, int]] = []
        while word:
            lo, hi = 1, len(word)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self._count(word[:mid]) <= budget:
                    lo = mid
                else:
                    hi = mid - 1
            parts.append((word[:lo], self._count(word[:lo])))
            word = word[lo:]
        return parts

    # --- packing --------------------------------------------------------------------------------

    def _spans(self, units: list[_Unit], budget: int) -> list[tuple[int, int]]:
        """Greedy ``[start, end)`` unit ranges, each chunk starting inside the end of the one before."""
        spans: list[tuple[int, int]] = []
        start = 0
        while True:
            end = start + 1
            total = units[start].tokens
            while end < len(units) and total + units[end].tokens <= budget:
                total += units[end].tokens
                end += 1
            spans.append((start, end))
            if end == len(units):
                return spans
            # Repeat the last unit, and earlier ones while the repeat stays short, but only as many as
            # leave room for the next new unit. The greedy fill stopped because that unit did not fit
            # beside the whole chunk, so the repeat is never the whole chunk (a lone large unit is not
            # repeated at all) and two consecutive chunks are never identical.
            begin = end - 1
            overlap = units[begin].tokens
            while begin > start and overlap + units[begin - 1].tokens < self._overlap_limit:
                begin -= 1
                overlap += units[begin].tokens
            while begin < end and overlap + units[end].tokens > budget:
                overlap -= units[begin].tokens
                begin += 1
            start = begin
