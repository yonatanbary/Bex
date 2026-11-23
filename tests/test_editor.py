"""Pure-Python tests for the on-node editor core.  Run: python3 tests/test_editor.py"""

import random
import sys
import types
from pathlib import Path

# bex/__init__.py imports bpy, so register a bare "bex" package pointing at the folder
# instead of running it. Relative imports (editor -> lang) then work outside Blender.
_BEX = Path(__file__).resolve().parent.parent / "bex"
_pkg = types.ModuleType("bex")
_pkg.__path__ = [str(_BEX)]
sys.modules.setdefault("bex", _pkg)

from bex.editor.buffer import TextBuffer  # noqa: E402
from bex.editor.highlight import KINDS, highlight, highlight_line  # noqa: E402
from bex.editor.complete import complete, match, suggest  # noqa: E402
from bex.editor.keys import EXIT, ZOOM_IN, ZOOM_OUT, ZOOM_RESET, Clipboard, View, handle_key  # noqa: E402


def buf(text, cursor=None, anchor=None):
    b = TextBuffer(text)
    if cursor:
        b.cursor = cursor
    b.anchor = anchor
    return b


def kinds(line):
    spans = highlight_line(line)
    return {line[s:e]: k for s, e, k in spans}


def check_cover(line, spans):
    pos = 0
    for s, e, k in spans:
        assert s == pos and e > s and k in KINDS, (line, spans)
        pos = e
    assert pos == len(line), (line, spans)


# -- buffer -----------------------------------------------------------------
def test_basics():
    b = TextBuffer()
    assert b.lines == [""] and b.text == "" and b.cursor == (0, 0)
    b.insert("ab\ncd\tx")
    assert b.lines == ["ab", "cd    x"] and b.cursor == (1, 7)


def test_newline_auto_indent():
    b = buf("    x = 1", (0, 9))
    b.newline()
    assert b.lines == ["    x = 1", "    "] and b.cursor == (1, 4)
    b = buf("if a > 0:  # yes", (0, 16))
    b.newline()
    assert b.lines == ["if a > 0:  # yes", "    "] and b.cursor == (1, 4)
    b = buf("  x = max()", (0, 10))
    b.newline()
    assert b.lines == ["  x = max(", "      ", "  )"] and b.cursor == (1, 6)
    b = buf("if a:\n    pass", (1, 8))
    b.newline()
    assert b.lines[2] == "" and b.cursor == (2, 0)


def test_else_dedents_to_its_if():
    b = buf("if a:\n    if b:\n        x = 1\n        else", (3, 12))
    b.insert(":")
    assert b.lines[3] == "    else:" and b.cursor == (3, 9)
    b = buf("if a:\n    x = 1\n    elif b", (2, 10))
    b.insert(":")
    assert b.lines[2] == "elif b:"
    b = buf("if a:\n    x = 1\nelse", (2, 4))
    b.insert(":")
    assert b.lines[2] == "else:"
    b = buf("    x", (0, 5))
    b.insert(":")
    assert b.lines == ["    x:"]


def test_backspace_delete():
    b = buf("ab\ncd", (1, 0))
    b.backspace()
    assert b.lines == ["abcd"] and b.cursor == (0, 2)
    b.delete()
    assert b.lines == ["abd"]
    b = buf("ab\ncd", (0, 2))
    b.delete()
    assert b.lines == ["abcd"]
    b = buf("foo bar_baz", (0, 11))
    b.backspace(word=True)
    assert b.lines == ["foo "]
    b = buf("foo bar", (0, 0))
    b.delete(word=True)
    assert b.lines == ["bar"]
    b = buf("x", (0, 0))
    v = b.version
    b.backspace()
    assert b.version == v and b.lines == ["x"]
    b = buf("        ", (0, 6))
    b.backspace()
    assert b.lines == ["      "] and b.cursor == (0, 4)  # back to previous indent stop


def test_selection_replace():
    b = buf("hello world", (0, 11), (0, 6))
    assert b.has_selection and b.selected_text() == "world"
    b.insert("there")
    assert b.text == "hello there" and not b.has_selection
    b = buf("ab\ncd\nef", (2, 1), (0, 1))
    assert b.selection_range() == ((0, 1), (2, 1)) and b.selected_text() == "b\ncd\ne"
    b.backspace()
    assert b.text == "af" and b.cursor == (0, 1)
    b.select_all()
    assert b.selected_text() == "af"
    b.clear_selection()
    assert not b.has_selection


def test_shift_movement_and_collapse():
    b = buf("abc def", (0, 0))
    b.move_right(select=True)
    b.move_right(word=True, select=True)
    assert b.anchor == (0, 0) and b.selected_text() == "abc "
    b.move_left()
    assert b.cursor == (0, 0) and not b.has_selection
    b = buf("abc def", (0, 1), (0, 5))
    b.move_right()
    assert b.cursor == (0, 5) and b.anchor is None
    b = buf("ab\ncd", (0, 1))
    b.move_down(select=True)
    assert b.selected_text() == "b\nc"
    b.move_doc_end(select=True)
    assert b.selected_text() == "b\ncd"
    b.move_doc_start()
    assert b.cursor == (0, 0) and not b.has_selection


def test_vertical_preferred_column():
    b = buf("abcdef\nab\nabcdef", (0, 5))
    b.move_down()
    assert b.cursor == (1, 2)
    b.move_down()
    assert b.cursor == (2, 5)
    b.move_up()
    b.move_up()
    assert b.cursor == (0, 5)
    b.move_up()
    assert b.cursor == (0, 0)
    b = buf("\n".join(str(i) for i in range(30)), (0, 0))
    b.page_down(10)
    assert b.cursor == (10, 0)
    b.page_up(20)
    assert b.cursor == (0, 0)


def test_smart_home_end():
    b = buf("    foo", (0, 7))
    b.move_home()
    assert b.cursor == (0, 4)
    b.move_home()
    assert b.cursor == (0, 0)
    b.move_home()
    assert b.cursor == (0, 4)
    b.move_end()
    assert b.cursor == (0, 7)


def test_indent_dedent():
    b = buf("a\n  b\nc", (1, 1), (0, 0))
    b.indent()
    assert b.lines == ["    a", "      b", "c"]
    assert b.anchor == (0, 0) and b.cursor == (1, 5)
    b.dedent()
    assert b.lines == ["a", "  b", "c"]
    b.dedent()
    assert b.lines == ["a", "b", "c"]
    b = buf("ab", (0, 1))
    b.indent()
    assert b.lines == ["a   b"] and b.cursor == (0, 4)
    b = buf("      x", (0, 7))
    b.dedent()
    assert b.lines == ["  x"] and b.cursor == (0, 3)
    v = b.version
    b = buf("x", (0, 0))
    b.dedent()
    assert b.version == 0 and v > 0


def test_toggle_comment():
    b = buf("  a = 1\n\n    b = 2", (2, 1), (0, 0))
    b.toggle_comment()
    assert b.lines == ["  # a = 1", "", "  #   b = 2"]
    b.toggle_comment()
    assert b.lines == ["  a = 1", "", "    b = 2"]
    b = buf("x", (0, 1))
    b.toggle_comment()
    assert b.lines == ["# x"] and b.cursor == (0, 3)
    b.toggle_comment()
    assert b.lines == ["x"] and b.cursor == (0, 1)


def test_undo_redo():
    b = TextBuffer()
    for ch in "foo":
        b.insert(ch)
    b.insert(" ")
    for ch in "bar":
        b.insert(ch)
    assert b.text == "foo bar"
    assert b.undo() and b.text == "foo"
    assert b.undo() and b.text == ""
    assert not b.undo()
    assert b.redo() and b.text == "foo"
    assert b.redo() and b.text == "foo bar" and b.cursor == (0, 7)
    assert not b.redo()
    b = TextBuffer()
    b.insert("a")
    b.insert("b")
    b.move_left()
    b.insert("c")
    assert b.text == "acb"
    b.undo()
    assert b.text == "ab"
    b.insert("x")  # new edit clears redo
    assert not b.redo()
    b = TextBuffer()
    for _ in range(300):
        b.newline()
    n = 0
    while b.undo():
        n += 1
    assert n == 200


def test_undo_groups():
    b = TextBuffer()
    for ch in "@P = 1":
        b.insert(ch)
    assert b.undo() and b.text == "@P"
    assert b.undo() and b.text == ""
    b = TextBuffer()
    for ch in "x = sin(@P) * 2":
        b.insert(ch)
    b.newline()
    b.insert("y")
    assert b.undo() and b.text == "x = sin(@P) * 2\n"
    assert b.undo() and b.text == "x = sin(@P) * 2"
    assert b.undo() and b.text == "x = sin(@P)"
    assert b.undo() and b.text == "x"
    b = TextBuffer("ab\ncdef")
    b.move_doc_end()
    for _ in range(5):
        b.backspace()
    assert b.text == "ab"
    assert b.undo() and b.text == "ab\n"      # the line join on its own
    assert b.undo() and b.text == "ab\ncdef"  # the run of Backspaces in one step
    b = TextBuffer("abc")
    b.move_doc_end()
    b.backspace()
    b.insert("x")
    assert b.undo() and b.text == "ab"


def test_set_text():
    b = buf("abcdef\nxyz", (1, 3), (0, 0))
    v = b.version
    b.set_text("ab")
    assert b.lines == ["ab"] and b.cursor == (0, 2) and b.anchor is None and b.version == v + 1
    b.set_text("ab")
    assert b.version == v + 1  # unchanged text is a no-op
    assert b.undo() and b.text == "abcdef\nxyz"


def test_mouse():
    b = buf("v@x = @P * 2.5;\nfoo")
    b.select_word_at(0, 7)
    assert b.selected_text() == "@P"
    b.select_word_at(0, 0)
    assert b.selected_text() == "v@x"
    b.select_word_at(0, 4)
    assert b.selected_text() == "="
    b.click(5, 99)
    assert b.cursor == (1, 3) and not b.has_selection
    b.click(-1, -4)
    assert b.cursor == (0, 0)
    b.click(0, 2, select=True)
    b.click(1, 1, select=True)
    assert b.anchor == (0, 0) and b.cursor == (1, 1)
    b.select_line(0)
    assert b.selected_text() == "v@x = @P * 2.5;" and b.cursor == (0, 15)
    b.select_line(1)
    assert b.selected_text() == "foo"


def test_scrollbar_thumb():
    from bex.editor.keys import thumb, thumb_first
    assert thumb(10, 10, 0, 100) is None            # everything fits
    assert thumb(100, 25, 0, 100) == (0, 25)
    assert thumb(100, 25, 75, 100) == (75, 25)      # at the end
    assert thumb(100, 25, 999, 100) == (75, 25)     # clamped
    assert thumb(1000, 10, 0, 100, min_size=12) == (0, 12)
    for first in (0, 10, 37, 75):
        off, size = thumb(100, 25, first, 100)
        assert thumb_first(off, size, 100, 25, 100) == first
    assert thumb_first(-50, 25, 100, 25, 100) == 0
    assert thumb_first(500, 25, 100, 25, 100) == 75


def test_version_increments():
    b = TextBuffer("a")
    v = b.version
    b.insert("b")
    b.newline()
    b.backspace()
    b.move_left()
    b.move_right()
    assert b.version == v + 3
    b.undo()
    assert b.version == v + 4


# -- highlight --------------------------------------------------------------
def test_highlight_numeric_component():
    spans = lambda l: [(l[a:b], k) for a, b, k in highlight_line(l)]  # noqa: E731
    assert spans("@P.1")[1:] == [(".", "punct"), ("1", "ident")]  # same as @P.y
    assert ("0.5", "number") in spans("x = 0.5") and (".5", "number") in spans("x = .5")


def test_highlight_kinds():
    line = 'x: float = chf("amp") * sin (@P.x) + f@mask  # hi'
    k = kinds(line)
    assert k["float"] == "type" and k["x"] == "ident" and k["="] == "operator" and k[":"] == "punct"
    assert k["chf"] == "channel" and k['"amp"'] == "string" and k["sin"] == "function"
    assert k["@P"] == "attr" and k["f@mask"] == "attr" and k["# hi"] == "comment"
    k = kinds("if True and not v: v = M_PI + 1.5e3 ** 2 // 3; elif else for i in range(3) while pass")
    assert k["if"] == "keyword" and k["True"] == "keyword" and k["M_PI"] == "constant" and k["not"] == "keyword"
    assert k["1.5e3"] == "number" and k[";"] == "punct" and k["**"] == "operator" and k["//"] == "operator"
    assert k["else"] == "keyword" and k["for"] == "keyword" and k["elif"] == "keyword" and k["range"] == "function"
    seen = set()
    for spans in highlight(['a: float = sin(PI) + chi("n")  # c', "if x: @P.y += 1; $ ok"]):
        seen.update(s[2] for s in spans)
    assert seen == set(KINDS), set(KINDS) - seen


def test_highlight_function_needs_call():
    assert kinds("sin (x)")["sin"] == "function"
    assert kinds("sin")["sin"] == "ident"
    assert kinds("x = sin")["sin"] == "ident"
    assert kinds("vector(1)")["vector"] == "type"


def test_highlight_never_raises():
    for line in ['"unterminated', "'x", "#", "@", "f@", "1.", ".5e", "\t$#`~\\", "a//b#c"]:
        check_cover(line, highlight_line(line))
    rng = random.Random(1)
    alphabet = 'ab f@P1.e"\'/*{}()$#\t sin(+-=!<>&|;:,_'
    for _ in range(500):
        line = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        check_cover(line, highlight_line(line))


def test_keys_typing_and_editing():
    b = TextBuffer("")
    for ch in "ab":
        assert handle_key(b, ch.upper(), text=ch) is True
    handle_key(b, "RET")
    handle_key(b, "TAB")
    assert b.text == "ab\n    "
    handle_key(b, "BACK_SPACE")
    assert b.text == "ab\n"
    assert handle_key(b, "ESC") == EXIT
    assert handle_key(b, "RET", ctrl=True) == EXIT
    assert handle_key(b, "F5") is False


def test_keys_platform_modifiers():
    b = TextBuffer("foo bar")
    b.click(0, 7)
    handle_key(b, "LEFT_ARROW", alt=True, mac=True)          # Option+Left: word
    assert b.cursor == (0, 4)
    handle_key(b, "LEFT_ARROW", cmd=True, mac=True)          # Cmd+Left: line start
    assert b.cursor == (0, 0)
    handle_key(b, "RIGHT_ARROW", ctrl=True, shift=True)      # Ctrl+Shift+Right elsewhere
    assert b.selected_text() == "foo "
    # Ctrl+letter types nothing on Windows/Linux.
    assert handle_key(b, "Q", text="q", ctrl=True) is False and b.text == "foo bar"


def test_keys_clipboard_undo_comment():
    clip = Clipboard()
    b = TextBuffer("x = 1")
    handle_key(b, "A", cmd=True, mac=True)
    handle_key(b, "X", cmd=True, mac=True, clipboard=clip)
    assert b.text == "" and clip.get() == "x = 1"
    handle_key(b, "V", ctrl=True, clipboard=clip)
    handle_key(b, "V", ctrl=True, clipboard=clip)
    assert b.text == "x = 1x = 1"
    handle_key(b, "Z", ctrl=True)
    assert b.text == "x = 1"
    handle_key(b, "Z", ctrl=True, shift=True)
    assert b.text == "x = 1x = 1"
    handle_key(b, "SLASH", ctrl=True)
    assert b.text == "# x = 1x = 1"


def test_keys_zoom():
    b = TextBuffer("x")
    assert handle_key(b, "EQUAL", ctrl=True) == ZOOM_IN
    assert handle_key(b, "EQUAL", cmd=True, shift=True, mac=True) == ZOOM_IN
    assert handle_key(b, "MINUS", ctrl=True) == ZOOM_OUT
    assert handle_key(b, "NUMPAD_0", cmd=True, mac=True) == ZOOM_RESET
    assert b.text == "x"


def test_select_at_like_vscode():
    b = TextBuffer('float amp = chf("amp", 0.25) * f@mask;\nif (x) [\n    @P.z += 1;\n]')

    def sel(row, col):
        b.select_at(row, col)
        return b.selected_text()

    assert sel(0, 2) == "float"
    assert sel(0, 5) == "float"          # just after a word takes the word
    assert sel(0, 16) == '"amp"'         # on a quote: the whole string
    assert sel(0, 18) == "amp"           # inside it: the word
    assert sel(0, 24) == "0.25"          # the whole number
    assert sel(0, 15) == '("amp", 0.25)' # bracket: up to its pair
    assert sel(0, 33) == "f@mask"        # attribute with its type prefix
    assert sel(0, 10) == "="
    assert sel(2, 10) == "+="            # operator run
    assert sel(1, 7) == "[\n    @P.z += 1;\n]"  # brackets across lines
    assert sel(3, 0) == "[\n    @P.z += 1;\n]"  # ... from either end


def test_suggest_inline_completion():
    def at_end(text, **kw):
        lines = text.split("\n")
        return suggest(lines, len(lines) - 1, len(lines[-1]), **kw)

    assert at_end("amplitude = 1\n@P.z = amp") == "litude"   # the script's own names first
    assert at_end("@P.z = smoothst") == "ep"                           # then language names
    assert at_end("@P.z = sin") == ""                                   # already a complete name
    assert at_end("f@mask = 1\n@P.z = @ma") == "sk"                   # attributes used in the script
    assert at_end("@P.z = @pt") == "num"                                # built-in attributes
    assert at_end("@P.z = v@ve", attributes=["velocity"]) == "locity"  # names from the geometry
    assert at_end("# amplitude\n@P.z = amp") == ""                    # comments don't count
    assert at_end("@P.z = 3") == "" and at_end("@P.z = ") == ""
    assert suggest(["x = amp;"], 0, 5) == ""                            # mid-word: nothing


def test_complete_menu():
    def names(text, **kw):
        lines = text.split("\n")
        comp = complete(lines, len(lines) - 1, len(lines[-1]), **kw)
        return None if comp is None else [i.name for i in comp.items]

    found = names("x = lerp")
    assert found[0] == "lerp" and {"slerp", "invlerp"} <= set(found)   # found inside a name too
    assert found.index("slerp") < found.index("invlerp")               # shorter first among equals
    found = names("x = fit")
    assert found[0] == "fit" and found[1].startswith("fit") and "efit" in found  # prefixes before the rest
    # The script's own names come before language names of the same rank.
    assert names("smoothie = 1\nx = smoo")[0] == "smoothie"
    comp = complete(["def wobble(p):", "    return p", "x = wob"], 2, 7)
    assert comp.items[0] == ("wobble", "function", (0, 1, 2)) and comp.start == 4
    comp = complete(["x = lerp"], 0, 8)
    kinds = {i.name: i.kind for i in comp.items}
    assert kinds["lerp"] == "function"
    assert {i.kind for i in complete(["x = ch"], 0, 6).items} >= {"channel"}
    comp = complete(["@P.z = v@ve"], 0, 11, attributes=["velocity"])
    assert comp.start == 9 and comp.items[0] == ("velocity", "attribute", (0, 1))
    assert names("x = ") is None and names("x = ", force=True)               # Ctrl+Space lists everything
    assert complete(["x = amp;"], 0, 5) is None and names("x = 3") is None
    assert len(names("x = e")) <= 50


def test_match_ranks():
    assert match("lerp", "lerp") == (0, (0, 1, 2, 3))
    assert match("ler", "lerp")[0] == 1
    assert match("lerp", "slerp") == (2, (1, 2, 3, 4))
    assert match("lrp", "lerp") == (3, (0, 2, 3))   # letters in order
    assert match("LERP", "lerp")[0] == 0             # case is ignored
    assert match("x", "lerp") is None and match("pl", "lerp") is None


def test_view_follow_and_hit():
    v = View()
    v.follow((30, 0), rows=10, cols=40)
    assert v.top == 21
    v.follow((5, 0), rows=10, cols=40)
    assert v.top == 5
    v.follow((5, 100), rows=10, cols=40)
    assert v.left == 64
    v.left = 0
    assert v.hit(0, 0, 10, 5) == (5, 0)
    assert v.hit(12, 25, 10, 5) == (7, 2)   # rounds to the nearest column gap
    assert v.hit(0, -3, 10, 5) == (4, 0)
    v.scroll(100, line_count=20, rows=10)
    assert v.top == 10



def test_signature_at():
    from bex.editor.signature import signature_at

    def at(text):  # "|" marks the cursor
        lines = text.split("\n")
        row = next(r for r, l in enumerate(lines) if "|" in l)
        col = lines[row].index("|")
        lines[row] = lines[row].replace("|", "")
        sig = signature_at(lines, row, col)
        return sig and (sig.name, sig.active, sig.row, sig.col)

    assert at("x = fit(|") == ("fit", 0, 0, 4)
    assert at("x = fit(@P.y, 0, |") == ("fit", 2, 0, 4)
    assert at("x = fit(a, b|)") == ("fit", 1, 0, 4)
    assert at("x = fit(a, b)|") is None                 # past the closing paren
    assert at("x = fit|(") is None
    assert at("x = fit(sin(a, b), |") == ("fit", 1, 0, 4)   # nested commas don't count
    assert at("x = fit((a + b), [1, 2], |") == ("fit", 2, 0, 4)
    assert at("x = fit((a|") == ("fit", 0, 0, 4)       # plain parens are looked through
    assert at("x = fit(foo(|") is None                  # inside an unknown function
    assert at("x = fit(sin(|") == ("sin", 0, 0, 8)      # the innermost call wins
    assert at('x = fit(",(", |') == ("fit", 1, 0, 4)    # strings skipped
    assert at("x = fit(a, # (,\n  b, |") == ("fit", 2, 0, 4)  # comments; over lines
    assert at("@fit(|") is None and at("f@fit(|") is None
    assert at("# fit(|") is None
    sig = signature_at(["fit(a, "], 0, 7)
    assert [r for _t, r in sig.parts()].count("active") == 1
    assert "".join(t for t, _r in sig.parts()) == "fit(value, old_min, old_max, new_min, new_max)"


def test_every_function_has_a_signature():
    from bex.lang.compiler import FUNCTION_NAMES, SIGNATURES
    assert set(SIGNATURES) == FUNCTION_NAMES, set(SIGNATURES) ^ FUNCTION_NAMES
    for name, (params, summary) in SIGNATURES.items():
        assert (params or name == "ident") and summary.endswith("."), name

def test_auto_pairs():
    b = TextBuffer("")
    for ch in "f(":
        b.insert(ch)
    assert b.text == "f()" and b.cursor == (0, 2)
    for ch in '"a':
        b.insert(ch)
    assert b.text == 'f("a")' and b.cursor == (0, 4)
    b.insert('"')  # steps over the closing quote
    b.insert(")")  # and the bracket
    assert b.text == 'f("a")' and b.cursor == (0, 6)
    b = TextBuffer("x")
    b.insert("(")  # before a word: no pair
    assert b.text == "(x"
    b = TextBuffer("")
    b.insert("[")
    b.backspace()  # [|] -> |
    assert b.text == ""
    b = TextBuffer("noise")
    b.select_all()
    b.insert("(")  # wraps the selection
    assert b.text == "(noise)" and b.selected_text() == "noise"
    b = TextBuffer("it")
    b.move_end()
    b.insert("'")  # an apostrophe after a word
    assert b.text == "it'"
    b = TextBuffer("")
    b.insert("ab()")  # a paste is left alone
    assert b.text == "ab()"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"{len(tests)} passed")
