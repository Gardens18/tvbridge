import unittest

from tvbridge.gui import keys
from tvbridge.gui.keys import KEYCODES, MODIFIER_FLAGS, char_to_key


class KeycodeTableTests(unittest.TestCase):
    def test_letters_and_digits_present(self):
        for ch in "abcdefghijklmnopqrstuvwxyz0123456789":
            self.assertIn(ch, KEYCODES)
        # spot-check the US ANSI layout
        self.assertEqual((KEYCODES["a"], KEYCODES["s"], KEYCODES["q"], KEYCODES["z"]), (0, 1, 12, 6))
        self.assertEqual((KEYCODES["1"], KEYCODES["5"], KEYCODES["9"], KEYCODES["0"]), (18, 23, 25, 29))
        self.assertEqual(len({KEYCODES[c] for c in "abcdefghijklmnopqrstuvwxyz0123456789"}), 36)

    def test_named_keys_match_spec(self):
        expected = {
            "period": 47, "comma": 43, "minus": 27, "equal": 24, "slash": 44, "semicolon": 41,
            "space": 49, "return": 36, "tab": 48, "escape": 53, "delete": 51, "forwarddelete": 117,
            "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
            "left": 123, "right": 124, "down": 125, "up": 126,
        }
        for name, code in expected.items():
            self.assertEqual(KEYCODES[name], code, name)

    def test_function_keys_match_spec(self):
        codes = [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111]
        for i, code in enumerate(codes, start=1):
            self.assertEqual(KEYCODES["f%d" % i], code)

    def test_modifier_flags(self):
        self.assertEqual(MODIFIER_FLAGS, {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "cmd": 0x100000})
        self.assertEqual(keys.modifier_mask(("shift", "cmd")), 0x120000)
        self.assertEqual(keys.modifier_mask(()), 0)
        self.assertEqual(keys.modifier_mask(["Command", "option"]), 0x180000)
        with self.assertRaises(ValueError):
            keys.modifier_mask(("hyper",))

    def test_keycode_lookup_and_aliases(self):
        self.assertEqual(keys.keycode("F9"), 101)
        self.assertEqual(keys.keycode("esc"), 53)
        self.assertEqual(keys.keycode("Enter"), 36)
        self.assertEqual(keys.keycode("end"), 119)
        with self.assertRaises(ValueError):
            keys.keycode("f13")
        with self.assertRaises(ValueError):
            keys.keycode("")


class CharToKeyTests(unittest.TestCase):
    def test_letters(self):
        self.assertEqual(char_to_key("e"), ("e", ()))
        self.assertEqual(char_to_key("E"), ("e", ("shift",)))
        self.assertEqual(char_to_key("h"), ("h", ()))

    def test_digits(self):
        for d in "0123456789":
            self.assertEqual(char_to_key(d), (d, ()))

    def test_punctuation(self):
        self.assertEqual(char_to_key("."), ("period", ()))
        self.assertEqual(char_to_key(","), ("comma", ()))
        self.assertEqual(char_to_key("-"), ("minus", ()))
        self.assertEqual(char_to_key("_"), ("minus", ("shift",)))
        self.assertEqual(char_to_key("#"), ("3", ("shift",)))
        self.assertEqual(char_to_key(":"), ("semicolon", ("shift",)))
        self.assertEqual(char_to_key("/"), ("slash", ()))
        self.assertEqual(char_to_key(" "), ("space", ()))

    def test_every_mapping_uses_a_known_key(self):
        for ch in "azAZ09.,-_#:/ ":
            name, mods = char_to_key(ch)
            self.assertIn(name, KEYCODES)
            for m in mods:
                self.assertIn(m, MODIFIER_FLAGS)

    def test_unsupported(self):
        for ch in ("@", "+", "é", "\n", "$", "%"):
            with self.assertRaises(ValueError, msg=repr(ch)):
                char_to_key(ch)
        with self.assertRaises(ValueError):
            char_to_key("ab")
        with self.assertRaises(ValueError):
            char_to_key("")

    def test_text_to_keys_validates_whole_string(self):
        self.assertEqual(keys.text_to_keys("EURUSD.h"),
                         [("e", ("shift",)), ("u", ("shift",)), ("r", ("shift",)), ("u", ("shift",)),
                          ("s", ("shift",)), ("d", ("shift",)), ("period", ()), ("h", ())])
        self.assertEqual(keys.text_to_keys("1.08100"),
                         [("1", ()), ("period", ()), ("0", ()), ("8", ()), ("1", ()), ("0", ()), ("0", ())])
        with self.assertRaises(ValueError):
            keys.text_to_keys("1.08@")


if __name__ == "__main__":
    unittest.main()
