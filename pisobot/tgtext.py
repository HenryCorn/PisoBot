"""Telegram's size limits, measured the way Telegram measures them: UTF-16 code units of the text a
person sees, i.e. after HTML entity parsing (tags removed, &amp; and friends decoded).

split_html cuts an HTML message into pieces that each fit, preferring line breaks, never inside a
tag, an entity or a code point, and closing every tag still open at a cut (reopened in the next
piece), so each piece is valid on its own. No network, no state.
"""
import html
import re

MESSAGE = 4096         # sendMessage / editMessageText text
CAPTION = 1024         # photo and album captions
CALLBACK_ANSWER = 200  # answerCallbackQuery text
PLACEHOLDER = 64       # ForceReply input_field_placeholder

_TAGS = re.compile(r"<[^>]*>")
_ATOMS = re.compile(r"<[^>]*>|&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);|[\s\S]")
_OPEN = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")
_CLOSE = re.compile(r"</([a-zA-Z][a-zA-Z0-9-]*)\s*>")


def units(text):
    return len(text.encode("utf-16-le")) // 2


def visible_text(text):
    return html.unescape(_TAGS.sub("", text))


def visible(text):
    """UTF-16 units Telegram counts for an HTML-formatted message."""
    return units(visible_text(text))


def _size(atom):
    return 0 if atom.startswith("<") and atom.endswith(">") and len(atom) > 1 else units(html.unescape(atom))


def _lines(text):
    """[(atoms, units)] per line, each line keeping its trailing newline atom."""
    lines, atoms, size = [], [], 0
    for atom in _ATOMS.findall(text):
        atoms.append(atom)
        size += _size(atom)
        if atom == "\n":
            lines.append((atoms, size))
            atoms, size = [], 0
    if atoms:
        lines.append((atoms, size))
    return lines


def split_html(text, limit=MESSAGE):
    """[piece, ...] each at most ``limit`` visible UTF-16 units; [text] when it already fits."""
    text = str(text)
    if visible(text) <= limit:
        return [text]
    pieces, stack = [], []          # stack: [(tag name, opening tag text)] open at this point
    state = {"opened": [], "atoms": [], "size": 0}

    def flush():
        atoms = state["atoms"]
        while atoms and atoms[-1] == "\n":
            atoms.pop()
        piece = ("".join(tag for _, tag in state["opened"]) + "".join(atoms)
                 + "".join(f"</{name}>" for name, _ in reversed(stack)))
        if visible_text(piece).strip():
            pieces.append(piece)
        state.update(opened=list(stack), atoms=[], size=0)

    def add(atom, size):
        if not state["atoms"] and atom == "\n":
            return                  # a piece never starts with a blank line
        state["atoms"].append(atom)
        state["size"] += size
        closing, opening = _CLOSE.fullmatch(atom), _OPEN.fullmatch(atom)
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0].lower() == closing[1].lower():
                    del stack[i]
                    break
        elif opening and not atom.endswith("/>"):
            stack.append((opening[1], atom))

    for atoms, size in _lines(text):
        if state["size"] + size > limit and state["atoms"]:
            flush()
        if size <= limit:
            for atom in atoms:
                add(atom, _size(atom))
            continue
        for atom in atoms:          # one line longer than a whole message: cut it by atom
            atom_size = _size(atom)
            if state["size"] + atom_size > limit:
                flush()
            add(atom, atom_size)
    flush()
    return pieces or [text[:0]]


def clip_html(text, limit=MESSAGE):
    """The first piece of ``text`` that fits, for a message that cannot be split (an edit)."""
    pieces = split_html(text, limit)
    if len(pieces) == 1:
        return pieces[0]
    return split_html(pieces[0], limit - 2)[0] + "\n…"


def clip(text, limit):
    """Plain text cut to ``limit`` UTF-16 units without splitting a code point."""
    text = str(text)
    if units(text) <= limit:
        return text
    out, size = [], 0
    for char in text:
        size += units(char)
        if size > limit - 1:
            break
        out.append(char)
    return "".join(out) + "…"
