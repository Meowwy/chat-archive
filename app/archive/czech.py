"""Czech morphology: turn one word into every inflected form of its lemma.

Czech inflects heavily - a noun has about ten forms, a verb about fifty - so a
literal search finds only the spelling that was typed. Searching `hospoda` in
this archive finds 6 messages; its whole paradigm finds 48.

The dictionary is three parallel files in `data/czech/`, built by the sibling
`czech_word_game` project out of the hunspell cs_CZ word list filtered through
LINDAT MorphoDiTa (see data/czech/NOTICE.md for the licence, which is
non-commercial):

    words.txt        3,475,913 lowercase forms, one per line
    lemmas.txt         272,867 lemmas, one per line
    word-lemma.u24   one big-endian 24-bit index into lemmas.txt per word

`build()` folds those into a SQLite dictionary that answers both directions of
the question the search needs: which lemma does this form belong to, and what
are that lemma's other forms.

Everything here is stored and returned *diacritic-folded*, because the FTS
index is built with `unicode61 remove_diacritics 2` and so contains folded
tokens too. `fold()` is byte-identical to that tokenizer for every Czech
character - `smoke_test.py` asserts it, and expansion would silently miss if it
ever drifted.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import config, db

SOURCE_FILES = ("words.txt", "lemmas.txt", "word-lemma.u24")


def fold(text: str) -> str:
    """Lowercase and strip diacritics, exactly as the FTS tokenizer does."""
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(ch) != "Mn"
    )


def negated(form: str, lemma: str) -> bool:
    """Is `form` the negated half of `lemma`'s paradigm? Both folded.

    Czech negation is a prefix that MorfFlex folds into the lemma, so `necekal`
    and `cekal` share the lemma `cekat` and half of any paradigm is the ne-
    half. Searching for one should not return the other - they mean opposite
    things - so the two halves are kept apart.

    Two traps. A superlative `nej-` comes first and is not a negation, though
    `nejne-` is. And a lemma can simply start with ne- of its own accord
    (nevim, nevesta, nenavidet, nemoc), in which case nothing is negated and
    the paradigm stays whole.
    """
    if lemma.startswith("ne"):
        return False
    if form.startswith("nej"):
        form = form[3:]
    return form.startswith("ne")


@dataclass(frozen=True)
class Expansion:
    """What one query word widened out to."""

    word: str
    lemmas: tuple[str, ...]
    forms: tuple[str, ...]


# ------------------------------------------------------------------- build

_SCHEMA = """
CREATE TABLE lemmas (
    lemma_id INTEGER PRIMARY KEY,
    lemma    TEXT NOT NULL
);
CREATE TABLE forms (
    fold     TEXT NOT NULL,
    lemma_id INTEGER NOT NULL,
    neg      INTEGER NOT NULL,
    PRIMARY KEY (fold, lemma_id)
) WITHOUT ROWID;
"""


def build(src: Path, dest: Path, *, verbose: bool = True) -> Path:
    """Compile the three source files into the lookup dictionary."""
    missing = [name for name in SOURCE_FILES if not (src / name).is_file()]
    if missing:
        raise FileNotFoundError(f"{src} is missing {', '.join(missing)}")

    lemmas = (src / "lemmas.txt").read_text(encoding="utf-8").rstrip("\n").split("\n")
    folded_lemmas = [fold(lemma) for lemma in lemmas]
    ids = (src / "word-lemma.u24").read_bytes()
    if len(ids) != 3 * sum(1 for _ in (src / "words.txt").open("rb")):
        raise ValueError("word-lemma.u24 and words.txt disagree on how many words there are")

    def rows():
        with (src / "words.txt").open(encoding="utf-8") as handle:
            for n, line in enumerate(handle):
                at = 3 * n
                # Big-endian u24, as written by czech_word_game's 07-rarity.mjs.
                lemma_id = (ids[at] << 16) | (ids[at + 1] << 8) | ids[at + 2]
                word = fold(line.rstrip("\n"))
                yield word, lemma_id, int(negated(word, folded_lemmas[lemma_id]))

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".building")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(str(tmp))
    try:
        con.execute("PRAGMA journal_mode = OFF")
        con.execute("PRAGMA synchronous = OFF")
        con.executescript(_SCHEMA)
        con.executemany("INSERT INTO lemmas VALUES (?, ?)", enumerate(lemmas))
        # Folding collapses about 100k spellings onto ones already there
        # (byt/byt/byt all fold to "byt"); the first wins, they agree anyway.
        con.executemany("INSERT OR IGNORE INTO forms VALUES (?, ?, ?)", rows())
        con.execute("CREATE INDEX forms_lemma_ix ON forms(lemma_id, neg, fold)")
        con.commit()
        con.execute("VACUUM")
        con.execute("ANALYZE")
        con.commit()
        counts = con.execute("SELECT COUNT(*) FROM forms").fetchone()[0]
    finally:
        con.close()
    dest.unlink(missing_ok=True)
    tmp.replace(dest)
    if verbose:
        print(f"{dest}: {counts:,} forms, {len(lemmas):,} lemmas, {dest.stat().st_size / 1e6:.0f} MB")
    return dest


# ------------------------------------------------------------------ lookup


class Lexicon:
    """Read-only view of the built dictionary."""

    def __init__(self, path: Path):
        self.path = path
        self._con = sqlite3.connect(db.ro_uri(path), uri=True, check_same_thread=False)
        self._cache: dict[str, Expansion | None] = {}

    def expand(self, word: str) -> Expansion | None:
        """Every form sharing this word's lemma and polarity, or None if unknown.

        Unknown is the common case for names, slang, English and emoji, and the
        caller searches those literally instead.
        """
        key = fold(word)
        if key in self._cache:
            return self._cache[key]
        found = self._con.execute(
            "SELECT lemma_id, neg FROM forms WHERE fold = ?", (key,)
        ).fetchall()
        if not found:
            self._cache[key] = None
            return None
        # A folded spelling can belong to several lemmas - "byt" is byt, byt
        # and byt - and there is no context here to choose between them, so
        # every reading is searched.
        forms: set[str] = set()
        lemmas: set[str] = set()
        for lemma_id, neg in found:
            forms.update(
                row[0]
                for row in self._con.execute(
                    "SELECT fold FROM forms WHERE lemma_id = ? AND neg = ?", (lemma_id, neg)
                )
            )
            lemmas.add(
                self._con.execute(
                    "SELECT lemma FROM lemmas WHERE lemma_id = ?", (lemma_id,)
                ).fetchone()[0]
            )
        result = Expansion(word, tuple(sorted(lemmas)), tuple(sorted(forms)))
        self._cache[key] = result
        return result


_lexicon: Lexicon | None = None
_lexicon_path: Path | None = None


def lexicon() -> Lexicon | None:
    """The dictionary, or None when it has not been built.

    Search has to keep working without it - it simply stops widening and
    matches literal words, which is what it did before any of this existed.
    """
    global _lexicon, _lexicon_path
    path = config.LEXICON_PATH
    if _lexicon is not None and _lexicon_path == path:
        return _lexicon
    _lexicon, _lexicon_path = None, path
    if path is not None and path.is_file():
        try:
            _lexicon = Lexicon(path)
        except sqlite3.Error:
            _lexicon = None
    return _lexicon
