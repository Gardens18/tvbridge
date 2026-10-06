"""Virtual key codes (US ANSI layout) and character -> key mapping.

Wine maps keyboard input by *key code*, not by the Unicode string attached to an event,
so tvbridge types every character as a physical key press (plus modifiers). Only the
characters needed for symbols, prices, lot sizes and comments are supported; anything
else raises ``ValueError`` before a single key is sent.

Pure data + helpers: no pyobjc imports here.
"""

from typing import Dict, Iterable, List, Tuple

#: macOS virtual key codes (``kVK_ANSI_*`` / ``kVK_*``) for the US ANSI layout.
KEYCODES = {
    # letters
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32,
    "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    # digits (top row)
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    # punctuation
    "period": 47, "comma": 43, "minus": 27, "equal": 24, "slash": 44, "semicolon": 41,
    # whitespace / editing
    "space": 49, "return": 36, "tab": 48, "escape": 53, "delete": 51, "forwarddelete": 117,
    # navigation
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    # function keys
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}  # type: Dict[str, int]

#: Quartz ``CGEventFlags`` bits for each modifier.
MODIFIER_FLAGS = {
    "shift": 0x20000,   # kCGEventFlagMaskShift
    "ctrl": 0x40000,    # kCGEventFlagMaskControl
    "alt": 0x80000,     # kCGEventFlagMaskAlternate
    "cmd": 0x100000,    # kCGEventFlagMaskCommand
}  # type: Dict[str, int]

#: Key codes of the (left) modifier keys themselves, pressed around a modified key.
MODIFIER_KEYCODES = {"shift": 56, "ctrl": 59, "alt": 58, "cmd": 55}  # type: Dict[str, int]

#: Accepted spellings for key names that are not canonical ``KEYCODES`` entries.
KEY_ALIASES = {
    "esc": "escape",
    "enter": "return",
    "backspace": "delete",
    "del": "forwarddelete",
    ".": "period",
    ",": "comma",
    "-": "minus",
    "=": "equal",
    "/": "slash",
    ";": "semicolon",
    " ": "space",
}  # type: Dict[str, str]

#: Accepted spellings for modifiers.
MODIFIER_ALIASES = {
    "control": "ctrl",
    "option": "alt",
    "opt": "alt",
    "command": "cmd",
}  # type: Dict[str, str]

# Unshifted characters typed with a named key.
_PLAIN_CHARS = {
    ".": "period",
    ",": "comma",
    "-": "minus",
    "/": "slash",
    " ": "space",
}  # type: Dict[str, str]

# Characters that need shift + a named key.
_SHIFTED_CHARS = {
    "_": "minus",
    "#": "3",
    ":": "semicolon",
}  # type: Dict[str, str]


def keycode(name: str) -> int:
    """Virtual key code for a key name (case-insensitive; aliases such as "esc" accepted).

    Raises ``ValueError`` for an unknown name.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("key name must be a non-empty string, got %r" % (name,))
    low = name if name == " " else name.strip().lower()
    low = KEY_ALIASES.get(low, low)
    try:
        return KEYCODES[low]
    except KeyError:
        raise ValueError("unknown key name %r (see tvbridge.gui.keys.KEYCODES)" % (name,))


def normalize_mods(mods: Iterable[str]) -> Tuple[str, ...]:
    """Canonical modifier names (shift/ctrl/alt/cmd), de-duplicated, order preserved.

    Raises ``ValueError`` for an unknown modifier.
    """
    out = []  # type: List[str]
    for m in mods or ():
        low = str(m).strip().lower()
        low = MODIFIER_ALIASES.get(low, low)
        if low not in MODIFIER_FLAGS:
            raise ValueError("unknown modifier %r (use shift, ctrl, alt or cmd)" % (m,))
        if low not in out:
            out.append(low)
    return tuple(out)


def modifier_mask(mods: Iterable[str]) -> int:
    """OR of the Quartz flag bits for ``mods`` (0 for no modifiers)."""
    mask = 0
    for m in normalize_mods(mods):
        mask |= MODIFIER_FLAGS[m]
    return mask


def char_to_key(ch: str) -> Tuple[str, Tuple[str, ...]]:
    """Map one character to ``(key_name, modifiers)`` on a US keyboard.

    Supported: a-z, A-Z (shift), 0-9, ".", ",", "-", "_" (shift+minus), "#" (shift+3),
    ":" (shift+semicolon), "/", " ". Anything else raises ``ValueError``.
    """
    if not isinstance(ch, str) or len(ch) != 1:
        raise ValueError("char_to_key expects a single character, got %r" % (ch,))
    if "a" <= ch <= "z" or "0" <= ch <= "9":
        return ch, ()
    if "A" <= ch <= "Z":
        return ch.lower(), ("shift",)
    if ch in _PLAIN_CHARS:
        return _PLAIN_CHARS[ch], ()
    if ch in _SHIFTED_CHARS:
        return _SHIFTED_CHARS[ch], ("shift",)
    raise ValueError("cannot type character %r: only letters, digits and . , - _ # : / space "
                     "are supported" % (ch,))


def text_to_keys(text: str) -> List[Tuple[str, Tuple[str, ...]]]:
    """``char_to_key`` for every character, validating the whole string first.

    Raises ``ValueError`` (naming the offending character) before anything is typed.
    """
    return [char_to_key(ch) for ch in text]
