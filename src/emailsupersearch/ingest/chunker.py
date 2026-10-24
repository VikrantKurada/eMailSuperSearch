"""Chunker: cut a body or an attachment's text into overlapping pieces that fit the embedding model.

The int8 MiniLM truncates at 256 word pieces, so every text is cut into chunks of about 220 tokens.
Each chunk is ``context_line + "\\n" + piece``: the one-line context header says whose email the
piece came from, so a chunk from page 12 of an attachment still carries its sender, date and subject.

Pieces are packed greedily from whole sentences (paragraphs are split on blank lines, sentences on
``.``, ``!`` and ``?``). The next chunk begins with the last sentence or two of the previous one (when
those and the next new sentence fit together), so a fact that straddles a cut is whole in at least one
chunk. A sentence that cannot fit becomes one unit per word, so the same overlap applies to text
without sentence breaks, and a single word that cannot fit (a rule of dashes, a base64 blob) is cut
into character runs.

A token is a word piece when a tokenizer file is given, or else a whitespace-separated word (the
keyword-only install, which has no model).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokenizers import Encoding, Tokenizer

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


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _date_for(sent_ts: int | None) -> str:
    """The UTC date of a timestamp; ``fromtimestamp`` fails on Windows for dates before 1970."""
    if sent_ts is None:
        return "unknown date"
    try:
        return (_EPOCH + timedelta(seconds=sent_ts)).date().isoformat()
    except OverflowError:
        return "unknown date"


def context_line_for(sender: str, sent_ts: int | None, subject: str, part_label: str) -> str:
    """The header line of every chunk, e.g. ``From Priya Nair on 2026-03-17: RE: Acme [body]``."""
    return f"From {sender} on {_date_for(sent_ts)}: {subject} [{part_label}]"


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
        context_tokens = self._count(context_line)
        budget = max(1, min(self._target, self._hard_max - context_tokens - _SPECIAL_TOKENS))
        units = self._units(text, budget)
        # Units are separated by whitespace, so word pieces never straddle them and the counts add up.
        return [
            ChunkText(i, context_line + "\n" + _join(units[a:b]), context_tokens + sum(u.tokens for u in units[a:b]))
            for i, (a, b) in enumerate(self._spans(units, budget))
        ]

    # --- token counting -------------------------------------------------------------------------

    def _count(self, text: str) -> int:
        return self._measure([text])[0][0]

    def _measure(self, texts: list[str]) -> list[tuple[int, Encoding | None]]:
        """Each text's token count, with its encoding (None without a tokenizer) for cutting a huge word."""
        if self._tokenizer is None:
            return [(len(t.split()), None) for t in texts]
        return [(len(e.ids), e) for e in self._tokenizer.encode_batch(texts, add_special_tokens=False)]

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
        """Sentences in order; one that is over ``budget`` tokens becomes one unit per word."""
        sentences: list[tuple[str, str]] = []
        for p, paragraph in enumerate(_PARAGRAPH_BREAK.split(text)):
            first = True
            for sentence in _SENTENCE_END.split(paragraph.strip()):
                if sentence:
                    sentences.append(("\n\n" if p and first else " ", sentence))
                    first = False
        units: list[_Unit] = []
        measured = self._measure([s for _, s in sentences])
        for (sep, sentence), (tokens, encoding) in zip(sentences, measured):
            if tokens <= budget:
                units.append(_Unit(sep, sentence, tokens))
                continue
            # Words, not word runs: runs of about one budget each would leave nothing to overlap with.
            words = sentence.split()
            for k, (part, part_tokens) in enumerate(self._word_parts(words, tokens, encoding, budget)):
                units.append(_Unit(sep if k == 0 else " ", part, part_tokens))
        return units

    def _word_parts(
        self, words: list[str], tokens: int, encoding: Encoding | None, budget: int
    ) -> list[tuple[str, int]]:
        """The words of an over-long sentence; a word over ``budget`` tokens is cut into character runs.

        A sentence that is a single word has just been tokenized, so its encoding is reused; tokenizing
        a megabyte of base64 takes a second, and every extra pass over it counts.
        """
        measured = [(tokens, encoding)] if len(words) == 1 else self._measure(words)
        parts: list[tuple[str, int]] = []
        for word, (word_tokens, word_encoding) in zip(words, measured):
            if word_tokens <= budget or word_encoding is None:
                parts.append((word, word_tokens))
            else:
                parts.extend(self._cut_word(word, word_encoding, budget))
        return parts

    def _cut_word(self, word: str, encoding: Encoding, budget: int) -> list[tuple[str, int]]:
        """Cut one word that is over ``budget`` tokens (a rule of dashes, a CJK run) into character runs.

        The word is tokenized once and each run takes up to ``budget`` of those tokens, ending where a
        pre-token (a run of letters, or one punctuation mark) ends so that the run alone tokenizes to
        the same tokens. Searching for the cut by re-tokenizing candidate runs costs a pass over the
        text per probe, which is minutes for a megabyte of base64.
        """
        starts = [o[0] for o in encoding.offsets]
        owners = encoding.word_ids
        n = len(starts)
        parts: list[tuple[str, int]] = []
        first = 0  # the first token of the run
        begin = 0  # the character where the run starts
        while first < n:
            last = min(first + budget, n)  # one past the run's last token
            if last < n:
                while last > first and owners[last] == owners[last - 1]:
                    last -= 1
                if last == first:
                    last = first + budget  # one pre-token alone is over budget (tiny budgets only)
            whole_start = first == 0 or owners[first] != owners[first - 1]
            while True:
                end = starts[last] if last < n else len(word)
                run = word[begin:end]
                whole = whole_start and (last == n or owners[last] != owners[last - 1])
                # A run of whole pre-tokens has exactly the tokens it had in the word; one that starts or
                # ends inside a pre-token is tokenized afresh, and shortened until it fits.
                tokens = last - first if whole else self._count(run)
                if tokens <= budget or last == first + 1:
                    break
                last -= 1
            parts.append((run, tokens))
            first, begin = last, end
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
