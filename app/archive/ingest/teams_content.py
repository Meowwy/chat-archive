"""Decoding what a Teams message says, out of the markup it says it in.

Teams inherited Skype's message formats and kept every one of them, so a single
conversation mixes plain text, Skype's own tag soup and modern Teams HTML. What
the viewer needs is the sentence somebody typed - and none of the scaffolding
around it.

Four things are dug out here:

`text()`
    The message as prose. Emoticons come back as the emoji they stand for -
    `<ss type="laugh" alt="😆">(laugh)</ss>` is 😆, not "(laugh)" and not both -
    and everything that is not typed prose is dropped: the quoted copy of an
    earlier message, the `<context>` roster Teams staples onto @mentions, and
    the "Pokud chcete zobrazit tuto sdílenou fotku, přejděte na: …" boilerplate
    a media message carries in place of a caption. That boilerplate is the
    reason media messages are given no text at all rather than having it
    stripped: it is Skype talking, never the sender, and indexing it would put
    every photo in the archive into the results for "fotku".

`reply_to()`
    The id of the message being replied to. Teams says it two ways - modern
    `qtdMsgs`, Skype's `<quote messageid=…>` - and both are read, so a reply
    keeps pointing at its parent regardless of which client sent it.

`attachments()`
    What the message carried. `amsreferences` names the objects; the filename
    and size come from the `<URIObject>` or from `properties.files`, whichever
    the sending client wrote. Files kept on OneDrive rather than in the export
    are listed too, with no doc id - those bytes are simply not in the download.

`reactions()`
    Skype emoticon shortcodes (`cwl`, `2714_heavycheckmark`, `yes-tone1`) turned
    into emoji. Unknown ones keep their name rather than being dropped.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

# Message types whose whole content is an attachment wrapper. Their text is
# Skype's "go here to view this" fallback in the recipient's UI language, so it
# is never shown and never indexed.
MEDIA_TYPES = {
    "RichText/UriObject",
    "RichText/Media_Video",
    "RichText/Media_GenericFile",
    "RichText/Media_AudioMsg",
    "RichText/Media_FlikMsg",
    "RichText/Media_Album",
}

# Not conversation: calls, polls, joins, renames, and Skype's machine
# translation of a message that is already in the archive in the original.
SKIPPED_TYPES_PREFIXES = ("ThreadActivity/", "Event/")
SKIPPED_TYPES = {"Poll", "Translation"}

# Teams keeps its own call logs as ordinary text messages in a thread of their
# own. They are the same machine bookkeeping as an Event/Call, written a
# different way, so they are left out for the same reason.
CALL_LOG = re.compile(r"^Call Logs for Call [0-9a-fA-F-]{36}$")

# Tags whose entire subtree is scaffolding rather than what someone typed.
#   quote/legacyquote  the copied text of the message being replied to
#   context            the participant roster Teams attaches to @mentions
#   URIObject          the media fallback sentence, plus OriginalName/FileSize
_DROP = {"quote", "legacyquote", "context", "uriobject", "swift"}

# Tags that end a line.
_BREAK = {"p", "div", "br", "li", "ol", "ul", "h1", "h2", "h3", "h4", "h5", "h6",
          "blockquote", "pre", "tr"}

_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


def _is_emoji(value: str) -> bool:
    """True for an `alt` that is an emoji rather than a description.

    Teams labels a real picture `alt="shared image"` and an emoticon
    `alt="😉"`. Anything with a Latin letter in it is prose about the element,
    not the element itself.
    """
    return bool(value) and not re.search(r"[A-Za-z]", value)


class _TextParser(HTMLParser):
    """Skype/Teams markup -> the prose inside it.

    Discarded regions are tracked as a stack rather than a depth counter,
    because whether a tag opens one can depend on its attributes: a
    `<blockquote>` is a reply preview only when Teams says so. Pushing an entry
    for every occurrence - dropping or not - is what keeps a plain blockquote
    nested inside a reply from closing the wrong region.
    """

    # Tags that may or may not open a discarded region, decided per occurrence.
    _CONDITIONAL = {"blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._stack: list[bool] = []  # one entry per open tag that can drop

    # -- helpers
    @property
    def _dropping(self) -> bool:
        return any(self._stack)

    def _emit(self, value: str) -> None:
        if value and not self._dropping:
            self.parts.append(value)

    def _break(self) -> None:
        if not self._dropping:
            self.parts.append("\n")

    @staticmethod
    def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
        for key, value in attrs:
            if key == name:
                return value or ""
        return ""

    def _opens_drop(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag == "blockquote":
            return "Reply" in self._attr(attrs, "itemtype")
        return tag in _DROP or tag == "ss"

    # -- HTMLParser
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "ss":
            # An emoticon: the emoji is in `alt`, and the "(laugh)" between the
            # tags is the same thing spelled out. Take one, drop the other.
            self._emit(self._attr(attrs, "alt") or emoji_for(self._attr(attrs, "type")))
            self._stack.append(True)
            return
        if tag in _DROP or tag in self._CONDITIONAL:
            self._stack.append(self._opens_drop(tag, attrs))
            return
        if tag == "img":
            alt = self._attr(attrs, "alt")
            self._emit(alt if _is_emoji(alt) else "")
            return
        if tag == "at":
            self._emit("@")
            return
        if tag in _BREAK:
            self._break()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """A self-closing tag opens and closes at once, so it never stacks."""
        if tag == "ss":
            self._emit(self._attr(attrs, "alt") or emoji_for(self._attr(attrs, "type")))
            return
        if tag in _DROP or tag in self._CONDITIONAL:
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "ss" or tag in _DROP or tag in self._CONDITIONAL:
            if self._stack:
                self._stack.pop()
            if tag in self._CONDITIONAL:
                self._break()
            return
        if tag in _BREAK:
            self._break()

    def handle_data(self, data: str) -> None:
        self._emit(data)


def _clean(raw: str) -> str:
    text = _WS.sub(" ", raw.replace("\r\n", "\n").replace("\r", "\n"))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def text(message: dict) -> str:
    """What the sender actually wrote, as plain text."""
    kind = message.get("messagetype") or ""
    content = message.get("content")

    if kind in MEDIA_TYPES:
        return ""  # the attachment is the message; see the module docstring
    if kind == "RichText/Contacts":
        return _contact_text(content)
    if kind == "RichText/Location":
        return _location_text(content)
    if not isinstance(content, str) or not content:
        return ""

    parser = _TextParser()
    parser.feed(content)
    parser.close()
    return _clean("".join(parser.parts))


def _attrs_of(content: str, tag: str) -> dict[str, str]:
    """The attributes of the first `tag` in `content`."""
    found: dict[str, str] = {}

    class Grab(HTMLParser):
        def handle_starttag(self, name, attrs):
            if name == tag and not found:
                found.update({k: (v or "") for k, v in attrs})

        handle_startendtag = handle_starttag

    parser = Grab(convert_charrefs=True)
    parser.feed(content)
    parser.close()
    return found


def _contact_text(content: Any) -> str:
    """`<contacts><c f="Lukáš Kolečkář"/></contacts>` -> a readable line."""
    if not isinstance(content, str):
        return ""
    names = [name for name in re.findall(r'<c\b[^>]*\bf="([^"]*)"', content) if name]
    return f"Shared contact: {', '.join(names)}" if names else "Shared a contact"


def _location_text(content: Any) -> str:
    """A shared pin, as its street address."""
    if not isinstance(content, str):
        return ""
    attrs = _attrs_of(content, "location")
    return attrs.get("address") or attrs.get("shortAddress") or "Shared a location"


# ---------------------------------------------------------------- replies


def reply_to(message: dict) -> str | None:
    """The id of the message this one replies to, in either client's spelling."""
    props = message.get("properties") or {}
    quoted = as_list(props.get("qtdMsgs"))
    for entry in quoted:
        if isinstance(entry, dict) and entry.get("messageId"):
            return str(entry["messageId"])

    content = message.get("content")
    if isinstance(content, str):
        if "<quote" in content:
            target = _attrs_of(content, "quote").get("messageid")
            if target:
                return target
        if "<blockquote" in content:
            attrs = _attrs_of(content, "blockquote")
            if "Reply" in attrs.get("itemtype", "") and attrs.get("itemid"):
                return attrs["itemid"]
    return None


def as_list(value: Any) -> list:
    """Teams writes some fields as JSON *inside* a JSON string.

    `properties.files`, `qtdMsgs`, `urlpreviews` and a thread's member list are
    all strings holding a JSON array. Iterating one without parsing it walks its
    characters, which is silent and produces nonsense, so everything that should
    be a list comes through here.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


# ------------------------------------------------------------ attachments


@dataclass(frozen=True)
class Attachment:
    """One file a message carried.

    `doc_id` is None for a file Teams kept on OneDrive rather than putting in
    the export - there is a name and a type, and no bytes anywhere.
    """

    doc_id: str | None
    name: str
    size: int = 0
    width: int | None = None
    height: int | None = None
    url: str = ""


def attachments(message: dict) -> list[Attachment]:
    """Every file on a message, named as well as the export allows."""
    refs = [r for r in (message.get("amsreferences") or []) if isinstance(r, str)]
    props = message.get("properties") or {}
    files = [f for f in as_list(props.get("files")) if isinstance(f, dict)]
    content = message.get("content")

    uri_meta = _uriobject_meta(content) if isinstance(content, str) else {}

    out: list[Attachment] = []
    for index, doc_id in enumerate(refs):
        entry = files[index] if index < len(files) else {}
        name = entry.get("fileName") or uri_meta.get("name") or ""
        out.append(
            Attachment(
                doc_id=doc_id,
                name=name,
                size=uri_meta.get("size", 0) if len(refs) == 1 else 0,
                width=uri_meta.get("width") if len(refs) == 1 else None,
                height=uri_meta.get("height") if len(refs) == 1 else None,
                url=uri_meta.get("uri", ""),
            )
        )

    # Files listed but not stored: OneDrive-hosted, and the export carries only
    # a signed URL that expired long before it was written.
    for entry in files[len(refs):]:
        name = entry.get("fileName")
        if name:
            out.append(Attachment(doc_id=None, name=name, url=_onedrive_url(entry)))
    return out


def _onedrive_url(entry: dict) -> str:
    info = entry.get("fileInfo") or {}
    return info.get("shareUrl") or info.get("fileUrl") or entry.get("objectUrl") or ""


def _uriobject_meta(content: str) -> dict:
    """Name, size and dimensions off a `<URIObject>` wrapper."""
    if "<URIObject" not in content and "<uriobject" not in content.lower():
        return {}
    attrs = _attrs_of(content, "uriobject")
    meta: dict[str, Any] = {}
    if attrs.get("uri"):
        meta["uri"] = attrs["uri"]
    for key in ("width", "height"):
        if attrs.get(key, "").isdigit():
            meta[key] = int(attrs[key])

    name = re.search(r"<OriginalName\b[^>]*\bv=\"([^\"]*)\"", content, re.I)
    if name and name.group(1):
        meta["name"] = name.group(1)
    size = re.search(r"<FileSize\b[^>]*\bv=\"(\d+)\"", content, re.I)
    if size:
        meta["size"] = int(size.group(1))
    return meta


def card_embed(message: dict) -> dict | None:
    """A shared GIF as an embed the viewer already knows how to draw.

    Teams writes the same Tenor card two ways depending on its age: modern
    clients put the card object straight in `content`, while Skype wrapped it in
    a `<URIObject type="SWIFT.1">` with the card base64'd into a `<Swift b64=…>`
    attribute and the visible text set to "To view this card, go to: …". Both
    are read, because otherwise every GIF anyone ever sent would import as an
    empty message.
    """
    content = _card_payload(message.get("content"))
    if content is None:
        return None
    for attachment in content.get("attachments") or []:
        body = attachment.get("content") if isinstance(attachment, dict) else None
        if not isinstance(body, dict):
            continue
        for image in body.get("images") or []:
            if isinstance(image, dict) and image.get("url"):
                return {
                    "type": "card",
                    "title": image.get("alt") or body.get("subtitle") or "",
                    "description": body.get("subtitle") or "",
                    "url": image["url"],
                }
    return None


_SWIFT_B64 = re.compile(r'<Swift\b[^>]*\bb64="([^"]+)"', re.I)


def _card_payload(content: Any) -> dict | None:
    """The card object, however this client chose to carry it."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    match = _SWIFT_B64.search(content)
    if match is None:
        return None
    try:
        decoded = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def link_embeds(message: dict) -> list[dict]:
    """Link previews Teams stored alongside the message."""
    props = message.get("properties") or {}
    out = []
    for entry in as_list(props.get("urlpreviews")):
        value = entry.get("value") if isinstance(entry, dict) else None
        if not isinstance(value, dict):
            continue
        url = value.get("target_url") or value.get("url")
        if not url:
            continue
        out.append(
            {
                "type": "share",
                "url": url,
                "title": value.get("title") or "",
                "description": value.get("description") or "",
            }
        )
    return out


# ------------------------------------------------------------- identities

# Skype mri prefixes: 8: is a person, 28: a bot. `<quote author=…>` writes the
# identifier without its prefix, so one gets added back.
_ROSTER = re.compile(r'<roster\b[^>]*\bid="([^"]+)"[^>]*\bname="([^"]*)"', re.I)
_MENTION = re.compile(r'<at\b[^>]*\bid="([^"]+)"[^>]*>([^<]*)</at>', re.I)
_QUOTE_AUTHOR = re.compile(r'<quote\b[^>]*\bauthor="([^"]+)"[^>]*\bauthorname="([^"]*)"', re.I)
_CONTACT = re.compile(r'<c\b[^>]*\bs="([^"]+)"[^>]*\bf="([^"]*)"', re.I)

# Teams writes this wherever it could not resolve an account to a person. It is
# a placeholder, not a name, and letting it into the directory would fuse every
# unresolved account in the export into one imaginary participant.
PLACEHOLDER_NAMES = {"Unknown User", "Export Owner"}

# An account id is "<network>:<identifier>" - but `<quote author=…>` writes the
# identifier alone, so "live:koleckar551" and "8:live:koleckar551" are the same
# person written two ways and have to be folded together before anything counts
# them. Everything else here is Teams addressing something that is not a person.
_MRI_PREFIX = re.compile(r"^\d+:")
_NOT_A_PERSON = {"null", "*", ""}


def normalise_mri(value: object) -> str | None:
    """One spelling of an account id, or None when it does not name anyone."""
    if not isinstance(value, str):
        return None
    mri = value.strip()
    if not mri:
        return None
    if not _MRI_PREFIX.match(mri):
        mri = f"8:{mri}"
    _, _, identifier = mri.partition(":")
    return None if identifier in _NOT_A_PERSON else mri


def mri_of(message: dict) -> str | None:
    """The account id of whoever sent this message, if the export says.

    `from` is filled in only for the account that requested the export, so it
    identifies you and nobody else. Everyone else is identified by `importedBy`,
    which Skype-to-Teams migration stamped on the messages it carried over.
    """
    sender = normalise_mri(message.get("from"))
    if sender:
        return sender
    imported = (message.get("properties") or {}).get("importedBy")
    if isinstance(imported, dict):
        return normalise_mri(imported.get("RawValue"))
    return None


def identity_hints(message: dict) -> list[tuple[str, str]]:
    """(account id, display name) pairs this message reveals about anyone.

    Teams names people in several places and not consistently in any of them:
    a message may carry a `displayName` and no id, an id and no name, or - in
    the group chats Skype migrated - neither. But the markup leaks a directory.
    An @mention carries both; so does a quoted message's header; and Teams
    staples a full `<roster>` of the conversation onto any message that mentions
    somebody. Harvesting all of it up front is what lets a message that says
    only `8:jofrey12345` be filed under the name that account goes by.
    """
    hints: list[tuple[str, str]] = []

    name = message.get("displayName")
    mri = mri_of(message)
    if mri and isinstance(name, str) and name:
        hints.append((mri, name))

    content = message.get("content")
    if not isinstance(content, str) or "<" not in content:
        return hints

    for pattern in (_ROSTER, _MENTION, _QUOTE_AUTHOR, _CONTACT):
        hints.extend(pattern.findall(content))

    resolved = []
    for identifier, display in hints:
        mri = normalise_mri(identifier)
        name = display.strip()
        if mri and name and name not in PLACEHOLDER_NAMES:
            resolved.append((mri, name))
    return resolved


def handle_of(mri: str) -> str:
    """A readable last resort for an account nothing in the export ever named.

    `unknown_user_<32 hex>` is Teams saying it could not resolve the account at
    all; spelling that out in full puts a 45-character name in the sender
    column, so it is shortened. Only the *name* is - the identity stays keyed on
    the whole id, so two such accounts remain two rows however alike they read.
    """
    handle = mri.split(":", 1)[1] if ":" in mri else mri
    handle = handle.removeprefix("live:").lstrip(".")
    handle = handle.removeprefix("cid.") or mri
    if handle.startswith("unknown_user_"):
        return f"Unknown account ({handle.removeprefix('unknown_user_')[:8]})"
    return handle


# -------------------------------------------------------------- reactions

# Teams stores a reaction as the shortcode of the emoticon that was picked.
# Anything not named here keeps its own name rather than being guessed at.
_EMOJI = {
    "yes": "👍", "like": "👍", "likewithface": "👍", "no": "👎", "heart": "❤️",
    "laugh": "😆", "xd": "😆", "cwl": "😂", "rofl": "🤣", "giggle": "🤭",
    "ok": "👌", "surprised": "😮", "star": "⭐", "cry": "😢",
    "loudlycrying": "😭", "stareyes": "🤩", "hearteyes": "😍", "sad": "😞",
    "party": "🎉", "festiveparty": "🎉", "facepalm": "🤦", "clap": "👏",
    "clappinghands": "👏", "clappinghandsskype": "👏", "rock": "🤘",
    "dance": "💃", "discodancer": "🕺", "tongueout": "😛", "champagne": "🥂",
    "cheers": "🍻", "drink": "🍹", "redwine": "🍷", "monkey": "🐵",
    "coolmonkey": "🐵", "seenoevil": "🙈", "hearnoevil": "🙉",
    "highfive": "🙌", "handsinair": "🙌", "muscle": "💪", "fire": "🔥",
    "skull": "💀", "think": "🤔", "wink": "😉", "angry": "😠",
    "angryface": "😠", "smile": "🙂", "smileeyes": "😊", "happyface": "😀",
    "cool": "😎", "worry": "😟", "snowflake": "❄️", "hearthands": "🫶",
    "sun": "☀️", "fearful": "😨", "screamingfear": "😱", "praying": "🙏",
    "cake": "🎂", "music": "🎵", "flushed": "😳", "blush": "😊",
    "inlove": "🥰", "sleepy": "😴", "tired": "😫", "unamused": "😒",
    "pensive": "😔", "puke": "🤮", "sarcastic": "😏", "smirk": "😏",
    "nerdy": "🤓", "victory": "✌️", "crossedfingers": "🤞", "bow": "🙇",
    "nod": "👍", "whew": "😅", "sweatgrinning": "😅", "doh": "🤦",
    "fistbump": "👊", "vulcansalute": "🖖", "snowangel": "⛄",
    "snegovik": "⛄", "snowmanwithoutsnow": "⛄", "santa": "🎅",
    "xmastree": "🎄", "fireworks": "🎆", "movie": "🎬", "games": "🎮",
    "headphones": "🎧", "bicycle": "🚲", "desert": "🏜️", "goodluck": "🍀",
    "wonder": "🤔", "dull": "😐", "mmm": "😊", "tmi": "🙊", "solo": "🎸",
    "grinningfacewithsmilingeyes": "😄", "meltingface": "🫠",
    "peekingeye": "👀", "webheart": "❤️", "cactuslove": "🌵",
}

# Reactions Teams spells as a codepoint and a name: 2714_heavycheckmark.
_HEX_NAMED = re.compile(r"^([0-9a-f]{4,6})_")
# Skin tones and Teams' own variant suffixes sit on top of a base emoticon.
_SUFFIXES = re.compile(r"(-tone\d|MSER)$")

# Not a reaction: Teams files the "who has seen the reactions" marker in with
# them, and it would otherwise show up on messages as a nonsense emoji.
NOT_A_REACTION = {"reactionsConsumptionHorizon"}


def emoji_for(key: str) -> str:
    """The emoji a Teams reaction shortcode stands for, or the name itself."""
    key = (key or "").strip()
    if not key:
        return "?"

    hexmatch = _HEX_NAMED.match(key)
    if hexmatch:
        try:
            return chr(int(hexmatch.group(1), 16))
        except ValueError:
            pass

    base = _SUFFIXES.sub("", key)
    for candidate in (base, base.lower(), base.lower().removeprefix("xmas")):
        if candidate.lower() in _EMOJI:
            return _EMOJI[candidate.lower()]

    # A poll option, or an emoticon nobody has taught us; keep it legible.
    return f":{key}:" if key.isalnum() else key


def reactions(message: dict) -> list[tuple[str, str]]:
    """(emoji, actor display name) for every reaction on the message."""
    props = message.get("properties") or {}
    out: list[tuple[str, str]] = []
    for entry in props.get("emotions") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not key or key in NOT_A_REACTION:
            continue
        emoji = emoji_for(key)
        for actor in entry.get("users") or []:
            if isinstance(actor, dict):
                out.append((emoji, actor.get("DisplayName") or ""))
    return out
