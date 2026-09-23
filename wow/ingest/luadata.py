"""Parse the Lua table literals QuestieDB ships its data in.

QuestieDB's Source-mode files embed each database as a Lua chunk string:
    QuestieDB.questData = [[return {
    [7] = {"Kobold Camp Cleanup",{{197}},{{197}},1,2,77,nil,...},
    }]]
Rows are positional tables whose index is the field number in the file's own
`questKeys`/`npcKeys`/`itemKeys` enum, and `nil` holes matter (field 7 above
is empty). So tables parse to `dict[int|str, value]` with 1-based integer
keys for positional entries, never to Python lists that would shift on nil.

Supports what these files use: nested tables, positional and `[key] =`
entries, bare `name =` keys, double/single-quoted strings with escapes,
integers, floats, negatives, true/false/nil, and `--` comments. Nothing is
executed.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"-?(?:0x[0-9a-fA-F]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "\n": "\n", "a": "\a", "b": "\b",
            "f": "\f", "v": "\v"}


class LuaParseError(ValueError):
    pass


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def ws(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n,;":
                self.i += 1
            elif s.startswith("--", self.i):
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end + 1
            else:
                break

    def value(self):
        self.ws()
        s, i = self.s, self.i
        c = s[i]
        if c == "{":
            return self.table()
        if c in "\"'":
            return self.string()
        if s.startswith("nil", i) and not s[i + 3:i + 4].isalnum():
            self.i += 3
            return None
        if s.startswith("true", i):
            self.i += 4
            return True
        if s.startswith("false", i):
            self.i += 5
            return False
        m = _NUMBER.match(s, i)
        if m:
            self.i = m.end()
            tok = m.group(0)
            if "x" in tok.lower():
                return int(tok, 16)
            return float(tok) if any(ch in tok for ch in ".eE") else int(tok)
        raise LuaParseError(f"unexpected {s[i:i + 30]!r} at {i}")

    def string(self) -> str:
        q = self.s[self.i]
        self.i += 1
        out, s = [], self.s
        while True:
            c = s[self.i]
            if c == "\\":
                nxt = s[self.i + 1]
                if nxt.isdigit():
                    m = re.match(r"\d{1,3}", s[self.i + 1:])
                    out.append(chr(int(m.group(0))))
                    self.i += 1 + len(m.group(0))
                    continue
                out.append(_ESCAPES.get(nxt, nxt))
                self.i += 2
            elif c == q:
                self.i += 1
                return "".join(out)
            else:
                out.append(c)
                self.i += 1

    def table(self) -> dict:
        self.i += 1  # {
        out: dict = {}
        pos = 1
        while True:
            self.ws()
            s = self.s
            if s[self.i] == "}":
                self.i += 1
                return out
            if s[self.i] == "[":
                self.i += 1
                key = self.value()
                self.ws()
                if s[self.i] != "]":
                    raise LuaParseError(f"expected ] at {self.i}")
                self.i += 1
                self.ws()
                if s[self.i] != "=":
                    raise LuaParseError(f"expected = at {self.i}")
                self.i += 1
                out[key] = self.value()
                continue
            m = _NAME.match(s, self.i)
            if m and m.group(0) not in ("nil", "true", "false"):
                j = m.end()
                while s[j] in " \t":
                    j += 1
                if s[j] == "=" and s[j + 1] != "=":
                    self.i = j + 1
                    out[m.group(0)] = self.value()
                    continue
            val = self.value()
            if val is not None:
                out[pos] = val
            pos += 1


def parse_table(text: str) -> dict:
    """Parse one Lua table literal (text starts at or before its `{`)."""
    p = _Parser(text)
    p.i = text.index("{")
    return p.table()


def embedded_data(source: str, var: str) -> dict:
    """The table in `<var> = [[return {...}]]` (Source-mode data files)."""
    m = re.search(re.escape(var) + r"\s*=\s*\[(=*)\[", source)
    if not m:
        raise LuaParseError(f"{var} not found")
    close = "]" + m.group(1) + "]"
    body = source[m.end():source.index(close, m.end())]
    return parse_table(body)


def assigned_table(source: str, var: str) -> dict:
    """A plain `<var> = { ... }` table (e.g. the *Keys enums, QuestXP.db)."""
    m = re.search(re.escape(var) + r"\s*=\s*\{", source)
    if not m:
        raise LuaParseError(f"{var} not found")
    return parse_table(source[m.end() - 1:])
