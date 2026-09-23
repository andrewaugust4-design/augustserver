"""Render a spell's tooltip description ("$s1 Nature damage for $d") from
the DB2 spell tables of one build. Ingest-only; the app reads the rendered
text from wow.db.

The two builds encode effect values differently:
- Classic Era: EffectBasePoints + a random 1..EffectDieSides, so classic
  spells store value − 1 with DieSides 1 (Thunderfury's 300 Nature damage
  is BasePoints 299). Newer spells use DieSides 0 and BasePoints = value.
- Forever: EffectBasePointsF is the value itself (retail style), with no
  integer BasePoints/DieSides columns.

Supported tokens: $sN/$SN/$mN/$MN (value or "min to max"), $oN (periodic
total), $tN (tick seconds), $d/$D (duration), $xN (chain targets), $aN
(radius), $h (proc chance), $n (proc charges), $u (max stacks), $/N;s1
(value ÷ N), ${expr}
with an optional .N decimals suffix, $l/$L singular:plural; and
$g/$G male:female; forms, and cross-spell references like $27648s1.
Unknown tokens are left as written rather than guessed.
"""

from __future__ import annotations

import ast
import csv
import operator
import re
from collections import defaultdict
from pathlib import Path


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(v: str | None, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def fmt_number(v: float) -> str:
    return str(int(round(v))) if abs(v - round(v)) < 1e-6 else f"{v:.1f}".rstrip("0").rstrip(".")


def fmt_duration(ms: float) -> str:
    sec = ms / 1000
    if sec < 60:
        return f"{fmt_number(sec)} sec"
    if sec < 3600:
        return f"{fmt_number(sec / 60)} min"
    if sec < 86400:
        return f"{fmt_number(sec / 3600)} hrs"
    return f"{fmt_number(sec / 86400)} days"


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _safe_eval(expr: str) -> float:
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(expr)
    return ev(ast.parse(expr, mode="eval"))


# $[spellid]<letter>[index]
_TOKEN = re.compile(r"\$(\d*)([a-zA-Z])(\d?)")


class SpellBook:
    def __init__(self, build_dir: Path):
        self.name = {int(r["ID"]): r["Name_lang"] for r in _read(build_dir / "SpellName.csv")}
        self.desc = {int(r["ID"]): r.get("Description_lang") or "" for r in _read(build_dir / "Spell.csv")}
        self.effects: dict[int, dict[int, dict]] = defaultdict(dict)
        for r in _read(build_dir / "SpellEffect.csv"):
            if int(r.get("DifficultyID") or 0) == 0:
                self.effects[int(r["SpellID"])][int(r["EffectIndex"])] = r
        durations = {int(r["ID"]): _num(r["Duration"]) for r in _read(build_dir / "SpellDuration.csv")}
        self.duration = {}
        for r in _read(build_dir / "SpellMisc.csv"):
            if int(r.get("DifficultyID") or 0) == 0 and int(r.get("DurationIndex") or 0):
                self.duration[int(r["SpellID"])] = durations.get(int(r["DurationIndex"]), 0)
        self.aura_opts = {int(r["SpellID"]): r for r in _read(build_dir / "SpellAuraOptions.csv")
                          if int(r.get("DifficultyID") or 0) == 0}
        self.radius = {int(r["ID"]): _num(r["Radius"]) for r in _read(build_dir / "SpellRadius.csv")}

    def has_text(self, spell_id: int) -> bool:
        return bool(self.desc.get(spell_id, "").strip())

    # ── effect values ──
    def value_range(self, spell_id: int, index: int) -> tuple[float, float] | None:
        eff = self.effects.get(spell_id, {}).get(index)
        if eff is None:
            return None
        if "EffectBasePoints" in eff:  # classic encoding
            bp, die = _num(eff["EffectBasePoints"]), _num(eff.get("EffectDieSides"))
            if die <= 0:
                return bp, bp
            return bp + 1, bp + die
        v = _num(eff.get("EffectBasePointsF"))
        var = _num(eff.get("Variance"))  # retail-style damage spread, e.g. 0.4 = ±20%
        if var > 0:
            return round(v * (1 - var / 2)), round(v * (1 + var / 2))
        return v, v

    def value(self, spell_id: int, index: int) -> float:
        r = self.value_range(spell_id, index)
        return r[0] if r else 0.0

    def _token(self, spell_id: int, letter: str, idx: int) -> str | None:
        i = max(idx - 1, 0)
        eff = self.effects.get(spell_id, {}).get(i)
        low = letter.lower()
        if low in "sm":
            r = self.value_range(spell_id, i)
            if r is None:
                return None
            lo, hi = abs(r[0]), abs(r[1])
            if letter == "M":
                return fmt_number(hi)
            if low == "m" or lo == hi:
                return fmt_number(lo)
            return f"{fmt_number(min(lo, hi))} to {fmt_number(max(lo, hi))}"
        if low == "o":
            period = _num(eff.get("EffectAuraPeriod")) if eff else 0
            dur = self.duration.get(spell_id, 0)
            if not period or not dur:
                return None
            return fmt_number(abs(self.value(spell_id, i)) * (dur // period))
        if low == "t":
            period = _num(eff.get("EffectAuraPeriod")) if eff else 0
            return fmt_number(period / 1000) if period else None
        if low == "d":
            dur = self.duration.get(spell_id)
            return fmt_duration(dur) if dur and dur > 0 else None
        if low == "x":
            return fmt_number(_num(eff.get("EffectChainTargets"))) if eff else None
        if low == "a":
            return fmt_number(self.radius.get(int(_num(eff.get("EffectRadiusIndex_0"))), 0)) if eff else None
        if low == "h":
            opts = self.aura_opts.get(spell_id)
            return fmt_number(_num(opts.get("ProcChance"))) if opts else None
        if low == "n":
            opts = self.aura_opts.get(spell_id)
            return fmt_number(_num(opts.get("ProcCharges"))) if opts else None
        if low == "u":
            opts = self.aura_opts.get(spell_id)
            return fmt_number(_num(opts.get("CumulativeAura"))) if opts else None
        return None

    def _sub_tokens(self, spell_id: int, text: str, numeric: bool = False) -> str:
        def repl(m: re.Match) -> str:
            ref, letter, idx = m.group(1), m.group(2), m.group(3)
            sid = int(ref) if ref else spell_id
            out = self._token(sid, letter, int(idx) if idx else 1)
            if out is None:
                return m.group(0)
            if numeric:  # inside ${...}: raw number, no "x to y"
                return fmt_number(self.value(sid, max(int(idx or 1) - 1, 0))) if letter.lower() in "sm" else out
            return out
        return _TOKEN.sub(repl, text)

    def render(self, spell_id: int) -> str | None:
        text = self.desc.get(spell_id, "").strip()
        if not text:
            return None

        def expr(m: re.Match) -> str:
            inner = self._sub_tokens(spell_id, m.group(1), numeric=True)
            try:
                val = _safe_eval(inner)
            except (ValueError, SyntaxError, ZeroDivisionError):
                return m.group(0)
            decimals = m.group(2)
            return f"{abs(val):.{int(decimals)}f}" if decimals else fmt_number(abs(val))
        text = re.sub(r"\$\{([^}]*)\}(?:\.(\d))?", expr, text)
        def divided(m: re.Match) -> str:  # $/1000;S1 = value ÷ 1000
            sid = int(m.group(2)) if m.group(2) else spell_id
            return fmt_number(abs(self.value(sid, max(int(m.group(4) or 1) - 1, 0))) / int(m.group(1)))
        text = re.sub(r"\$/(\d+);(\d*)([a-zA-Z])(\d?)", divided, text)
        text = re.sub(r"\$[gG]([^:;]*):([^;]*);", r"\1", text)
        text = re.sub(r"\$[lL]([^:;]*):([^;]*);", r"\2", text)  # plural form; numbers here are almost never 1
        text = self._sub_tokens(spell_id, text)
        return re.sub(r"\s{2,}", " ", text)
