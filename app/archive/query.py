"""The search query language: parse what was typed, emit what FTS5 wants.

Two jobs. Words are widened to their whole Czech paradigm, so `hospoda` also
finds `hospody` and `hospodě` (see `czech.py`). And the query itself is a small
boolean language, so several words can be combined:

    hospoda pivo            both words, in any inflected form
    hospoda OR pivo         either
    (pivo OR víno) hospoda  grouping
    pivo -hospoda           exclude
    "hospody"               this exact form, no widening
    hospod*                 prefix, which already spans the paradigm

`OR`, `AND` and `NOT` are operators only in capitals, so ordinary lowercase
text is never mistaken for one; a literal capital `OR` can still be searched by
quoting it. Everything emitted is quoted, so nothing a user types can reach the
FTS parser as syntax.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .czech import Expansion, Lexicon

# A pathological query - a word folding onto several big verb paradigms - can
# reach a few hundred terms, which FTS5 still answers in milliseconds. This is
# only a stop against something absurd; words over the budget are searched
# literally rather than the search failing.
MAX_TERMS = 2000


class QueryError(ValueError):
    """The query cannot be expressed as a search. The message is user-facing."""


# --------------------------------------------------------------- spelling

_APOSTROPHES = "'’ʼ"
_STRIP_APOSTROPHE = str.maketrans("", "", _APOSTROPHES)
_SPLIT_APOSTROPHE = str.maketrans({c: " " for c in _APOSTROPHES})

# The tails an English or Czech apostrophe leaves behind: don't, she's, you're,
# I've, we'll, I'd, I'm, they'em. A leading head is short instead: o'clock,
# y'all, d'Artagnan.
_CONTRACTIONS = frozenset({"t", "s", "d", "m", "n", "re", "ve", "ll", "em"})


def _phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _variants(word: str) -> list[str]:
    """Every spelling of one word that an apostrophe could account for.

    The tokenizer treats an apostrophe as a separator, so "don't" is indexed as
    the two tokens don + t while "dont" is indexed as one, and neither spelling
    can find the other on its own. A word is therefore searched as both: itself
    without apostrophes, plus the phrases that put one back where a writer
    plausibly dropped it.
    """
    plain = word.translate(_STRIP_APOSTROPHE)
    if not plain:
        return []
    if plain != word:
        # They typed the apostrophe: the two spellings are exactly known.
        return [plain, word.translate(_SPLIT_APOSTROPHE).strip()]
    found = [plain]
    for cut in range(1, len(plain)):
        if cut == 1 or plain[cut:].lower() in _CONTRACTIONS:
            found.append(f"{plain[:cut]} {plain[cut:]}")
    return found


def _any_of(spellings: list[str]) -> str:
    return spellings[0] if len(spellings) == 1 else "(" + " OR ".join(spellings) + ")"


# ----------------------------------------------------------------- lexing

_OPERATORS = {"OR": "or", "|": "or", "AND": "and", "NOT": "not"}


@dataclass(frozen=True)
class _Token:
    kind: str  # word | phrase | ( | ) | or | and | not
    text: str = ""
    prefix: bool = False


def _lex(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, end = 0, len(text)
    while i < end:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch in "()":
            tokens.append(_Token(ch))
            i += 1
        elif ch == '"':
            close = text.find('"', i + 1)
            # An unclosed quote is a half-typed phrase, not an error: take the
            # rest of the line, which is what the user was in the middle of.
            close = end if close < 0 else close
            tokens.append(_Token("phrase", text[i + 1 : close]))
            i = close + 1
        else:
            start = i
            while i < end and not text[i].isspace() and text[i] not in '()"':
                i += 1
            tokens.append(_word_token(text[start:i]))
    return [t for t in tokens if t.kind != "word" or t.text]


def _word_token(raw: str) -> _Token:
    if raw in _OPERATORS:
        return _Token(_OPERATORS[raw])
    # A leading minus excludes; one inside a word (e-mail, česko-slovenský) is
    # part of it.
    if raw.startswith("-") and len(raw) > 1:
        return _Token("not-word", raw[1:].rstrip("*"), raw.endswith("*"))
    return _Token("word", raw.rstrip("*"), raw.endswith("*"))


# ------------------------------------------------------------------ nodes


@dataclass
class _And:
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)

    def sql(self) -> str:
        if not self.positives and not self.negatives:
            return ""
        if not self.positives:
            raise QueryError(
                "A search needs at least one word to look for, not only words to exclude."
            )
        kept = " AND ".join(self.positives)
        if not self.negatives:
            return kept
        # FTS5's NOT is a binary operator - "a AND NOT b" is a syntax error, so
        # everything excluded hangs off the right of one NOT.
        left = kept if len(self.positives) == 1 else f"({kept})"
        return f"{left} NOT {_any_of(self.negatives)}"


# ----------------------------------------------------------------- parsing


class _Parser:
    def __init__(self, tokens: list[_Token], lexicon: Lexicon | None):
        self.tokens = tokens
        self.at = 0
        self.lexicon = lexicon
        self.terms: list[Expansion] = []
        self.budget = MAX_TERMS

    # -- cursor
    def peek(self) -> _Token | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.tokens[self.at]
        self.at += 1
        return token

    # -- grammar
    def parse(self) -> str:
        sql = self.or_expr()
        if self.peek() is not None:
            raise QueryError("Unexpected ')' - check the brackets in your search.")
        return sql

    def or_expr(self) -> str:
        parts = [self.and_expr()]
        while (token := self.peek()) and token.kind == "or":
            self.take()
            parts.append(self.and_expr())
        parts = [p for p in parts if p]
        if not parts:
            return ""
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    def and_expr(self) -> str:
        group = _And()
        while (token := self.peek()) and token.kind not in ("or", ")"):
            if token.kind == "and":
                self.take()
                continue
            # Either spelling of exclusion: a NOT before the word, or a minus
            # stuck to the front of it, which lexes as one token.
            negate = token.kind in ("not", "not-word")
            if token.kind == "not":
                self.take()
                if (nxt := self.peek()) is None or nxt.kind in ("or", ")"):
                    raise QueryError("NOT needs a word after it.")
            clause = self.atom()
            if not clause:
                continue
            (group.negatives if negate else group.positives).append(clause)
        return group.sql()

    def atom(self) -> str:
        token = self.take()
        if token.kind == "(":
            inner = self.or_expr()
            if (closing := self.peek()) is None or closing.kind != ")":
                raise QueryError("A '(' in your search is never closed.")
            self.take()
            return inner
        if token.kind == ")":
            raise QueryError("Unexpected ')' - check the brackets in your search.")
        if token.kind == "phrase":
            return self.phrase(token.text)
        # A "-slovo" token carries its own negation, which and_expr() has
        # already noted; what is left here is just the word.
        return self.word(token)

    # -- leaves
    def phrase(self, inner: str) -> str:
        """A quoted phrase is taken literally - no widening, exactly as typed."""
        spellings = {
            _phrase(inner.translate(_STRIP_APOSTROPHE)),
            _phrase(inner.translate(_SPLIT_APOSTROPHE)),
        }
        return _any_of(sorted(spellings))

    def word(self, token: _Token) -> str:
        if token.prefix:
            # A prefix already spans the paradigm, and it cannot be a phrase,
            # so it only gets the apostrophe-free spelling.
            plain = token.text.translate(_STRIP_APOSTROPHE)
            return f"{_phrase(plain)}*" if plain else ""
        # Both spellings at once: the apostrophe variants a word always gets,
        # plus its Czech paradigm. A word can need both - "dont" is an English
        # contraction and, as it happens, a Czech noun.
        spellings = {_phrase(v) for v in _variants(token.text)}
        spellings.update(self.widen(token.text))
        return _any_of(sorted(spellings))

    def widen(self, word: str) -> list[str]:
        """The word's whole Czech paradigm, quoted; empty if it has none."""
        if self.lexicon is None:
            return []
        found = self.lexicon.expand(word)
        if found is None or len(found.forms) > self.budget:
            return []
        self.budget -= len(found.forms)
        self.terms.append(found)
        return [_phrase(form) for form in found.forms]


# ------------------------------------------------------------------ public


@dataclass(frozen=True)
class Query:
    expression: str
    terms: tuple[Expansion, ...]


def build_query(text: str, lexicon: Lexicon | None = None) -> Query:
    """Parse a typed search into an FTS5 expression. Raises QueryError."""
    parser = _Parser(_lex(text.strip()), lexicon)
    return Query(parser.parse(), tuple(parser.terms))


def to_fts_query(text: str, lexicon: Lexicon | None = None) -> str:
    return build_query(text, lexicon).expression
