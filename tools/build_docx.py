#!/usr/bin/env python3
"""Word (.docx) version of the paper, built from the same sources as the PDF.

Inputs: paper/main.tex, results/macros.tex, results/tab_*.tex, results/fig_*.pdf, and the
last LaTeX build of the paper (paper/main.aux for label and citation numbers, paper/main.bbl
for the formatted references), so the Word text, numbers, numbering and references match the
PDF of the same build.  Body text and equations go through pandoc (equations become editable
OMML objects); the title block, IEEE page layout (US Letter, two columns), figures, tables,
equation numbers and references are added with python-docx and lxml.

Usage:  python3 tools/build_docx.py [output.docx]      (run `make paper` first)
"""
from __future__ import annotations

import copy
import datetime as dt
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor, Twips

ROOT = Path(__file__).resolve().parents[1]
PAPER, RESULTS = ROOT / "paper", ROOT / "results"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

FONT = "Times New Roman"
MONO = "Courier New"
COL_W = Inches(3.5)            # IEEE conference template: two 3.5" columns, 0.25" apart
FULL_W = Inches(7.25)
FLAGS = {"ifanon": True, "ifpilot": False}  # the switches of main.tex; read_flags takes them from the source


def read_flags(tex: str) -> None:
    """Set FLAGS from the switch lines of main.tex (\\newif\\ifpilot \\pilotfalse, ...)."""
    for name in ("anon", "pilot"):
        m = re.search(r"\\newif\\if" + name + r"\s*\\" + name + r"(true|false)", tex)
        if m:
            FLAGS["if" + name] = m.group(1) == "true"


# --------------------------------------------------------------------------- TeX helpers

def group(s: str, i: int) -> tuple[str, int]:
    """Balanced {...} group starting at s[i] == '{'; returns (content, index after group)."""
    assert s[i] == "{", s[i:i + 30]
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    raise ValueError("unbalanced group: " + s[i:i + 60])


def load_macros() -> dict[str, str]:
    """Result macros; the \\providecommand defaults of main.tex apply where the analysis wrote none."""
    out = {}
    pre = (PAPER / "main.tex").read_text(encoding="utf-8").split("\\begin{document}")[0]
    for m in re.finditer(r"\\providecommand\{\\(r[A-Za-z]+)\}", pre):
        val, _ = group(pre, m.end())
        out[m.group(1)] = val
    s = (RESULTS / "macros.tex").read_text(encoding="utf-8")
    for m in re.finditer(r"\\newcommand\{\\(r[A-Za-z]+)\}", s):
        val, _ = group(s, m.end())
        out[m.group(1)] = val
    return out


def load_aux() -> tuple[dict[str, str], dict[str, int]]:
    s = (PAPER / "main.aux").read_text(encoding="utf-8")
    labels, cites = {}, {}
    for m in re.finditer(r"\\newlabel\{([^}]+)\}", s):
        outer, _ = group(s, m.end())
        num, _ = group(outer, 0)
        num = re.sub(r"\\mbox\s*", "", num).replace("{", "").replace("}", "").strip()
        labels[m.group(1)] = num
    for m in re.finditer(r"\\bibcite\{([^}]+)\}\{(\d+)\}", s):
        cites[m.group(1)] = int(m.group(2))
    return labels, cites


def skip_cs_space(s: str, j: int) -> int:
    """TeX skips blanks after a control word, and one end of line, but not a blank line."""
    while j < len(s) and s[j] in " \t":
        j += 1
    if j < len(s) and s[j] == "\n":
        j += 1
        while j < len(s) and s[j] in " \t":
            j += 1
    return j


def expand_macros(s: str, macros: dict[str, str]) -> str:
    pat = re.compile(r"\\(r[A-Z][A-Za-z]*)")
    for _ in range(6):
        out, i, changed = [], 0, False
        for m in pat.finditer(s):
            name = m.group(1)
            if name not in macros:
                continue
            out.append(s[i:m.start()])
            out.append(macros[name])
            j = m.end()
            if s.startswith("{}", j):
                j += 2
            else:
                j = skip_cs_space(s, j)
            i, changed = j, True
        out.append(s[i:])
        s = "".join(out)
        if not changed:
            break
    return s


def check_expanded(s: str) -> None:
    left = sorted(set(re.findall(r"\\(r[A-Z][A-Za-z]*)", s)))
    if left:
        raise SystemExit(f"undefined result macros in the active text: {left}")


def resolve_conditionals(s: str) -> str:
    pat = re.compile(r"\\(ifnum|ifanon|ifpilot|else|fi)(?![A-Za-z])")
    num = re.compile(r"\s*(-?\d+)\s*([<=>])\s*(-?\d+)")
    out, stack, i = [], [], 0

    def active():
        return all((c if not e else not c) for c, e in stack)

    while True:
        m = pat.search(s, i)
        if not m:
            if active():
                out.append(s[i:])
            break
        if active():
            out.append(s[i:m.start()])
        cmd, j = m.group(1), m.end()
        if cmd == "ifnum":
            mm = num.match(s, j)
            if not mm and not active():
                # a condition inside a branch that is not taken: TeX skips it without expanding,
                # so its macro may be undefined (for example the lock-file results without lock files)
                mu = re.compile(r"\s*\\r[A-Za-z]+\s*[<=>]\s*-?\d+").match(s, j)
                if mu:
                    stack.append([False, False])
                    j = mu.end()
                    if j < len(s) and s[j] in " \n":
                        j += 1
                    i = j
                    continue
            if not mm:
                raise SystemExit("cannot read \\ifnum condition: " + s[j:j + 40])
            a, op, b = int(mm.group(1)), mm.group(2), int(mm.group(3))
            stack.append([{"<": a < b, "=": a == b, ">": a > b}[op], False])
            j = mm.end()
            if j < len(s) and s[j] in " \n":   # the blank that ends the number
                j += 1
        elif cmd in FLAGS:
            stack.append([FLAGS[cmd], False])
            j = skip_cs_space(s, j)
        elif cmd == "else":
            stack[-1][1] = True
            j = skip_cs_space(s, j)
        else:
            stack.pop()
            j = skip_cs_space(s, j)
        i = j
    if stack:
        raise SystemExit("unbalanced conditionals")
    return "".join(out)


def dec_in_math(s: str) -> str:
    """Word equations have no small caps: inside math, decision names become upright capitals."""
    out, i = [], 0
    pat = re.compile(r"\\begin\{equation\}.*?\\end\{equation\}|(?<!\\)\$[^$]*\$", re.S)
    for m in pat.finditer(s):
        out.append(s[i:m.start()])
        out.append(re.sub(r"\\dec\{([A-Za-z]+)\}", lambda mm: r"\mathrm{" + mm.group(1).upper() + "}", m.group(0)))
        i = m.end()
    out.append(s[i:])
    return "".join(out)


def strip_comments(s: str) -> str:
    s = re.sub(r"(?m)^[ \t]*%.*\n", "", s)          # whole-line comments vanish
    return re.sub(r"(?<!\\)%.*", "", s)              # trailing comments


ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]


def roman(n: int) -> str:
    return ROMAN[n]


def cite_text(keys: str, cites: dict[str, int]) -> str:
    """\\cite{a,b,c} as IEEEtran with the cite package prints it: sorted, ranges of three
    or more compressed to [a]--[c]."""
    nums = sorted({cites[k.strip()] for k in keys.split(",") if k.strip()})
    parts, run = [], [nums[0]]
    for n in nums[1:]:
        if n == run[-1] + 1:
            run.append(n)
        else:
            parts.append(run)
            run = [n]
    parts.append(run)
    out = []
    for r in parts:
        if len(r) >= 3:
            out.append(f"[{r[0]}]--[{r[-1]}]")
        else:
            out.extend(f"[{x}]" for x in r)
    return ", ".join(out)


# --------------------------------------------------------------------------- simple LaTeX -> runs
# (captions, table cells, the title block and the reference list; the body goes through pandoc)

GREEK = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "theta": "θ", "tau": "τ", "rho": "ρ",
         "Gamma": "Γ", "Delta": "Δ", "mu": "μ", "ell": "ℓ", "sigma": "σ", "lambda": "λ"}
SYMBOL = {"bot": "⊥", "geq": "≥", "ge": "≥", "leq": "≤", "le": "≤", "to": "→", "neq": "≠", "ne": "≠",
          "in": "∈", "wedge": "∧", "times": "×", "cdot": "·", "ldots": "…", "infty": "∞",
          "rightarrow": "→", "pm": "±", "approx": "≈", "sim": "∼"}
SANS = {"sres": "res", "sest": "est", "spin": "pin", "sprov": "prov"}
ACCENT = {'"u': "ü", '"a': "ä", '"o': "ö", "'e": "é", "`e": "è", "'a": "á", "'o": "ó", "c c": "ç",
          '"U': "Ü", "'E": "É", "^o": "ô", "~n": "ñ"}


class Run(dict):
    """text + flags: i, b, sc (small caps), mono, sup, sub, sans"""


def tex_runs(s: str, base: dict | None = None, labels: dict | None = None) -> list[Run]:
    base = dict(base or {})
    labels = labels or {}
    runs: list[Run] = []

    def emit(text, **fl):
        if not text:
            return
        f = {**base, **fl}
        if runs and {k: v for k, v in runs[-1].items() if k != "text"} == f:
            runs[-1]["text"] += text
        else:
            runs.append(Run(text=text, **f))

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "$":
            j = s.index("$", i + 1)
            for r in math_runs(s[i + 1:j], labels):
                emit(r.pop("text"), **{**r})
            i = j + 1
            continue
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)\*?", s[i:])
            if not m:
                nxt = s[i + 1] if i + 1 < n else ""
                if nxt in "%&_#$":
                    emit(nxt)
                    i += 2
                    continue
                if nxt == ",":
                    emit("\u2009")
                    i += 2
                    continue
                if nxt in " \n":
                    emit(" ")
                    i += 2
                    continue
                if nxt == "\\":                     # line break inside a cell or caption
                    emit(" ")
                    i += 2
                    continue
                if s[i + 1:i + 3] in ACCENT:
                    emit(ACCENT[s[i + 1:i + 3]])
                    i += 3
                    continue
                i += 2
                continue
            name, j = m.group(1), i + m.end()
            if name in ("texttt", "emph", "textit", "textbf", "textsc", "dec", "url", "mbox", "textrm", "textnormal",
                        "BIBforeignlanguage"):
                if name == "BIBforeignlanguage":
                    _, j = group(s, j)
                arg, j = group(s, j)
                fl = {"texttt": {"mono": True}, "url": {"mono": False}, "emph": {"i": not base.get("i")},
                      "textit": {"i": True}, "textbf": {"b": True}, "textsc": {"sc": True}, "dec": {"sc": True},
                      "mbox": {}, "textrm": {}, "textnormal": {}, "BIBforeignlanguage": {}}[name]
                for r in tex_runs(arg, {**base, **fl}, labels):
                    emit(r.pop("text"), **{k: v for k, v in r.items()})
                i = j
                continue
            if name == "ref":
                arg, j = group(s, j)
                emit(labels.get(arg, "??"))
                i = j
                continue
            if name in ("ldots", "dots", "textasciitilde", "textbackslash"):
                emit({"ldots": "…", "dots": "…", "textasciitilde": "~", "textbackslash": "\\"}[name])
                i = j + 2 if s.startswith("{}", j) else skip_cs_space(s, j)   # TeX eats the blank after a word
                continue
            if name in ("newblock", "BIBentryALTinterwordspacing", "BIBentrySTDinterwordspacing", "relax",
                        "centering", "noindent", "footnotesize", "scriptsize", "small", "normalsize"):
                i = skip_cs_space(s, j)
                continue
            if name in GREEK or name in SYMBOL:
                emit(GREEK.get(name) or SYMBOL[name])
                i = skip_cs_space(s, j)
                continue
            if name in ("c",) and j < n and s[j] == "{":
                arg, j = group(s, j)
                emit({"c": "ç"}.get(arg, arg))
                i = j
                continue
            # unknown command: keep its argument, if any
            if j < n and s[j] == "{":
                arg, j = group(s, j)
                for r in tex_runs(arg, base, labels):
                    emit(r.pop("text"), **{k: v for k, v in r.items()})
            i = j
            continue
        if c in "{}":
            i += 1
            continue
        if c == "~":
            emit("\u00a0")
            i += 1
            continue
        if s.startswith("---", i):
            emit("—")
            i += 3
            continue
        if s.startswith("--", i):
            emit("–")
            i += 2
            continue
        if s.startswith("``", i):
            emit("“")
            i += 2
            continue
        if s.startswith("''", i):
            emit("”")
            i += 2
            continue
        if c in "\n\t":
            emit(" ")
            i += 1
            continue
        emit(c)
        i += 1
    # collapse runs of blanks
    for r in runs:
        r["text"] = re.sub(r"[ ]{2,}", " ", r["text"])
    return runs


def math_runs(s: str, labels: dict) -> list[Run]:
    """Inline math of captions and table cells, as text runs (letters italic)."""
    out: list[Run] = []

    def emit(text, **fl):
        if text:
            out.append(Run(text=text, **fl))

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)", s[i:])
            if not m:
                nxt = s[i + 1] if i + 1 < n else ""
                emit({",": "\u2009", " ": " ", "{": "{", "}": "}", "%": "%"}.get(nxt, ""))
                i += 2
                continue
            name, j = m.group(1), i + m.end()
            if name in SANS:
                emit(SANS[name], sans=True)
            elif name in GREEK:
                emit(GREEK[name], i=name[0].islower())
            elif name in SYMBOL:
                emit(" " + SYMBOL[name] + " " if name in ("geq", "ge", "leq", "le", "to", "neq", "ne", "in", "wedge")
                     and False else SYMBOL[name])
            elif name in ("mathsf", "mathrm", "text", "mathit", "mathcal", "texttt", "dec"):
                arg, j = group(s, j)
                fl = {"mathsf": {"sans": True}, "mathrm": {}, "text": {}, "mathit": {"i": True},
                      "mathcal": {"i": False}, "texttt": {"mono": True}, "dec": {"sc": True}}[name]
                if name == "mathcal" and arg == "R":
                    emit("ℛ")
                elif name == "mathcal" and arg == "P":
                    emit("𝒫")
                else:
                    for r in math_runs(arg, labels) if name in ("mathit", "mathsf") else tex_runs(arg, {}, labels):
                        t = r.pop("text")
                        r.update(fl)
                        if name == "mathsf":
                            r.pop("i", None)
                        emit(t, **r)
            elif name == "ref":
                arg, j = group(s, j)
                emit(labels.get(arg, "??"))
            elif name in ("left", "right", "big", "Big", "bigl", "bigr", "quad", "qquad"):
                emit(" " if "quad" in name else "")
            else:
                emit(name)
            i = j
            continue
        if c in "^_":
            j = i + 1
            if s[j] == "{":
                arg, j = group(s, j)
            else:
                arg, j = s[j], j + 1
            for r in math_runs(arg, labels):
                t = r.pop("text")
                r["sup" if c == "^" else "sub"] = True
                emit(t, **r)
            i = j
            continue
        if c in "{}":
            i += 1
            continue
        if c == " ":
            i += 1
            continue
        if c.isalpha():
            emit(c, i=True)
        else:
            emit(c)
        i += 1
    return out


# --------------------------------------------------------------------------- docx helpers

def set_run(run, r: Run, size: float | None = None):
    run.font.name = MONO if r.get("mono") else ("Arial" if r.get("sans") and False else FONT)
    rpr = run._r.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(a), MONO if r.get("mono") else FONT)
    if r.get("i"):
        run.italic = True
    if r.get("b"):
        run.bold = True
    if r.get("sc"):
        run.font.small_caps = True
    if r.get("sup"):
        run.font.superscript = True
    if r.get("sub"):
        run.font.subscript = True
    if size:
        run.font.size = Pt(size)


def add_runs(p, runs: list[Run], size: float | None = None, **extra):
    for r in runs:
        rr = Run({**r, **extra})
        run = p.add_run(rr["text"])
        set_run(run, rr, size)


def para_fmt(p, align=None, before=0, after=0, first=None, left=None, right=None, keep_next=False, line=None):
    pf = p.paragraph_format
    if align is not None:
        p.alignment = align
    pf.space_before, pf.space_after = Pt(before), Pt(after)
    if first is not None:
        pf.first_line_indent = first
    if left is not None:
        pf.left_indent = left
    if right is not None:
        pf.right_indent = right
    if keep_next:
        pf.keep_with_next = True
    if line is not None:
        pf.line_spacing = line


def new_paragraph_after(el, doc):
    """A fresh w:p inserted right after element el (paragraph or table)."""
    p = doc.add_paragraph()
    el.addnext(p._p)
    return p


def sect_pr(cols: int, continuous: bool = True):
    s = OxmlElement("w:sectPr")
    if continuous:
        t = OxmlElement("w:type")
        t.set(qn("w:val"), "continuous")
        s.append(t)
    pg = OxmlElement("w:pgSz")
    pg.set(qn("w:w"), "12240")
    pg.set(qn("w:h"), "15840")
    s.append(pg)
    mar = OxmlElement("w:pgMar")
    for k, v in (("top", 1080), ("right", 900), ("bottom", 1440), ("left", 900), ("header", 720),
                 ("footer", 720), ("gutter", 0)):
        mar.set(qn(f"w:{k}"), str(v))
    s.append(mar)
    c = OxmlElement("w:cols")
    c.set(qn("w:num"), str(cols))
    c.set(qn("w:space"), "360")
    s.append(c)
    return s


def end_section_here(p, cols: int, continuous: bool):
    """Make paragraph p the last one of a section with `cols` columns."""
    ppr = p._p.get_or_add_pPr()
    old = ppr.find(qn("w:sectPr"))
    if old is not None:
        ppr.remove(old)
    ppr.append(sect_pr(cols, continuous))


def tiny_paragraph(p):
    para_fmt(p, before=0, after=0)
    p.paragraph_format.line_spacing = Pt(1)
    r = p.add_run("")
    r.font.size = Pt(1)


def cell_borders(cell, **edges):
    tcpr = cell._tc.get_or_add_tcPr()
    b = tcpr.find(qn("w:tcBorders"))
    if b is None:
        b = OxmlElement("w:tcBorders")
        tcpr.append(b)
    for edge in ("top", "left", "bottom", "right"):
        if edge in edges:
            e = b.find(qn(f"w:{edge}"))
            if e is None:
                e = OxmlElement(f"w:{edge}")
                b.append(e)
            sz = edges[edge]
            e.set(qn("w:val"), "single" if sz else "nil")
            if sz:
                e.set(qn("w:sz"), str(sz))
                e.set(qn("w:space"), "0")
                e.set(qn("w:color"), "000000")
    # schema order inside tcBorders: top, left/start, bottom, right/end
    order = {qn("w:top"): 0, qn("w:left"): 1, qn("w:bottom"): 2, qn("w:right"): 3}
    for e in sorted(list(b), key=lambda x: order.get(x.tag, 9)):
        b.remove(e)
        b.append(e)


def table_no_borders(tbl):
    tblpr = tbl._tbl.tblPr
    b = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "nil")
        b.append(e)
    old = tblpr.find(qn("w:tblBorders"))
    if old is not None:
        tblpr.remove(old)
    tblpr.append(b)


def set_grid(tbl, widths, fixed=True):
    """Column grid, preferred width and layout in twips (python-docx leaves an even 6.5-inch grid, which
    readers that lay out by the grid draw wider than the column)."""
    t = tbl._tbl
    for gc, w in zip(t.tblGrid.findall(qn("w:gridCol")), widths):
        gc.set(qn("w:w"), str(int(w)))
    tblpr = t.tblPr
    tw = tblpr.find(qn("w:tblW"))
    if tw is None:
        tw = OxmlElement("w:tblW")
        tblpr.append(tw)
    tw.set(qn("w:w"), str(int(sum(widths))))
    tw.set(qn("w:type"), "dxa")
    lay = tblpr.find(qn("w:tblLayout"))
    if lay is None:
        lay = OxmlElement("w:tblLayout")
        tblpr.append(lay)
    lay.set(qn("w:type"), "fixed" if fixed else "autofit")


def cell_margins(tbl, left=40, right=40, top=0, bottom=0):
    tblpr = tbl._tbl.tblPr
    m = OxmlElement("w:tblCellMar")
    for k, v in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        e = OxmlElement(f"w:{k}")
        e.set(qn("w:w"), str(v))
        e.set(qn("w:type"), "dxa")
        m.append(e)
    old = tblpr.find(qn("w:tblCellMar"))
    if old is not None:
        tblpr.remove(old)
    tblpr.append(m)


def mono_hyphens(s: str) -> str:
    """Typewriter fonts have no dash ligatures, so LaTeX prints \\texttt{--x} as two hyphens; pandoc would
    turn them into an en dash."""
    out, i = [], 0
    for mm in re.finditer(r"\\texttt\{", s):
        if mm.start() < i:
            continue
        arg, j = group(s, mm.end() - 1)
        out.append(s[i:mm.start()] + "\\texttt{" + re.sub(r"-(?=-)", "-{}", arg) + "}")
        i = j
    out.append(s[i:])
    return "".join(out)


SCRIPT_OPS = set("+\u2212-*\u2217\u00b1\u2032\u2033")
LONE_BRACKETS = {"[", "]", "(", ")"}


def fix_omml(root):
    """Repair pandoc's OMML.

    * pandoc writes m:dPr children out of schema order (m:endChr before m:sepChr).
    * Runs that a linear-format reader cannot parse on their own -- an operator alone in a script ($\\ell^{+}$,
      $S^{*}$) or a bracket pandoc did not pair (the interval $(0,1]$) -- get m:lit. Word draws them unchanged (the
      property only stops the linear format from reading them as operators); LibreOffice quotes them instead of
      reporting a syntax error and blanking the formula.
    """
    from lxml import etree
    order = ["begChr", "sepChr", "endChr", "grow", "shp", "ctrlPr"]
    for dpr in root.iter(f"{{{M_NS}}}dPr"):
        kids = list(dpr)
        kids.sort(key=lambda e: order.index(e.tag.split("}")[1]) if e.tag.split("}")[1] in order else 99)
        for k in kids:
            dpr.remove(k)
            dpr.append(k)
    # Pair the brackets pandoc leaves bare (mismatched pairs such as the half-open interval (0,1]) into one
    # delimiter object with those two characters, as Word's equation editor writes them.
    def bare(el):
        if el.tag != f"{{{M_NS}}}r":
            return None
        t = "".join(x.text or "" for x in el.iter(f"{{{M_NS}}}t")).strip()
        return t if t in LONE_BRACKETS else None

    for parent in list(root.iter()):
        if not isinstance(parent.tag, str) or not parent.tag.startswith(f"{{{M_NS}}}"):
            continue
        i = 0
        while i < len(parent):
            kids = list(parent)
            t = bare(kids[i])
            if t in ("(", "["):
                j = next((k for k in range(i + 1, len(kids)) if bare(kids[k]) is not None), None)
                if j is not None and bare(kids[j]) in (")", "]"):
                    d = etree.Element(f"{{{M_NS}}}d")
                    dpr = etree.SubElement(d, f"{{{M_NS}}}dPr")
                    etree.SubElement(dpr, f"{{{M_NS}}}begChr").set(f"{{{M_NS}}}val", t)
                    etree.SubElement(dpr, f"{{{M_NS}}}endChr").set(f"{{{M_NS}}}val", bare(kids[j]))
                    etree.SubElement(dpr, f"{{{M_NS}}}grow")
                    e = etree.SubElement(d, f"{{{M_NS}}}e")
                    for k in kids[i + 1:j]:
                        e.append(k)
                    parent.replace(kids[i], d)
                    parent.remove(kids[j])
            i += 1
    # A formula that is only a script on nothing (ZTD$_{\\mathit{JAVA}}$) is text with a sub- or superscript; as
    # math, Word would show an empty placeholder box for the missing base.
    for om in list(root.iter(f"{{{M_NS}}}oMath")):
        kids = [c for c in om if c.tag != f"{{{M_NS}}}ctrlPr"]
        if om.getparent().tag != f"{{{W_NS}}}p" or len(kids) != 1 or \
                kids[0].tag not in (f"{{{M_NS}}}sSub", f"{{{M_NS}}}sSup"):
            continue
        base = kids[0].find(f"{{{M_NS}}}e")
        if base is None or "".join(base.itertext()).replace("\u200b", "").strip():   # pandoc's empty base: U+200B
            continue
        sub = kids[0].tag.endswith("sSub")
        script = kids[0].find(f"{{{M_NS}}}{'sub' if sub else 'sup'}")
        text = "".join(script.itertext())
        upright = all((r.find(f"{{{M_NS}}}rPr/{{{M_NS}}}sty") is not None and
                       r.find(f"{{{M_NS}}}rPr/{{{M_NS}}}sty").get(f"{{{M_NS}}}val") == "p")
                      for r in script.iter(f"{{{M_NS}}}r"))
        wr = etree.Element(f"{{{W_NS}}}r")
        rpr = etree.SubElement(wr, f"{{{W_NS}}}rPr")
        if not upright:
            etree.SubElement(rpr, f"{{{W_NS}}}i")
        etree.SubElement(rpr, f"{{{W_NS}}}vertAlign").set(f"{{{W_NS}}}val", "subscript" if sub else "superscript")
        etree.SubElement(wr, f"{{{W_NS}}}t").text = text
        om.getparent().replace(om, wr)

    left = [bare(r) for r in root.iter(f"{{{M_NS}}}r") if bare(r)]
    if left:
        print(f"warning: unpaired brackets in math: {left}", file=sys.stderr)

    # Math runs carry the body size explicitly (Word takes it from the paragraph anyway; LibreOffice would
    # otherwise draw every formula at 12 pt).
    for r in root.iter(f"{{{M_NS}}}r"):
        if r.find(f"{{{W_NS}}}rPr") is None:
            wrpr = etree.Element(f"{{{W_NS}}}rPr")
            etree.SubElement(wrpr, f"{{{W_NS}}}sz").set(f"{{{W_NS}}}val", "20")
            etree.SubElement(wrpr, f"{{{W_NS}}}szCs").set(f"{{{W_NS}}}val", "20")
            mrpr = r.find(f"{{{M_NS}}}rPr")
            r.insert(0 if mrpr is None else list(r).index(mrpr) + 1, wrpr)

    scripts = (f"{{{M_NS}}}sup", f"{{{M_NS}}}sub")
    for r in list(root.iter(f"{{{M_NS}}}r")):
        t = "".join(x.text or "" for x in r.iter(f"{{{M_NS}}}t")).strip()
        if not t:
            continue
        par = r.getparent()
        alone = par is not None and par.tag in scripts and len(par.findall(f"{{{M_NS}}}r")) == 1 \
            and len([c for c in par if c.tag != f"{{{M_NS}}}ctrlPr"]) == 1
        if (alone and set(t) <= SCRIPT_OPS) or t in LONE_BRACKETS:
            rpr = r.find(f"{{{M_NS}}}rPr")
            if rpr is None:
                rpr = etree.Element(f"{{{M_NS}}}rPr")
                r.insert(0, rpr)
            if rpr.find(f"{{{M_NS}}}lit") is None:
                rpr.insert(0, etree.Element(f"{{{M_NS}}}lit"))


# --------------------------------------------------------------------------- tables from tab_*.tex

def split_top(s: str, sep: str) -> list[str]:
    parts, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            cur.append(s[i:i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def parse_tabular(tex: str, macros: dict):
    tex = expand_macros(tex, macros)
    m = re.search(r"\\begin\{tabular\}", tex)
    spec, j = group(tex, m.end())
    aligns = [c for c in re.sub(r"@\{[^}]*\}", "", spec) if c in "lrc"]
    body = tex[j:tex.index("\\end{tabular}")]
    rows, rules = [], {}          # rules[(row_index, 'above'|'below')] = list of (c1, c2, size)
    pending_above = []
    for chunk in re.split(r"\\\\", body):
        chunk = chunk.strip()
        # leading rule commands belong above the next row / below the previous one
        while True:
            mm = re.match(r"\s*\\(toprule|midrule|bottomrule|cmidrule)(\([lr]*\))?(\{(\d+)-(\d+)\})?", chunk)
            if not mm:
                break
            kind = mm.group(1)
            if kind == "cmidrule":
                a, b = int(mm.group(4)) - 1, int(mm.group(5)) - 1
                rules.setdefault(len(rows) - 1, []).append((a, b, 4))
            elif kind == "toprule":
                pending_above.append(12)
            elif kind == "midrule":
                rules.setdefault(len(rows) - 1, []).append((0, len(aligns) - 1, 6))
            else:
                rules.setdefault(len(rows) - 1, []).append((0, len(aligns) - 1, 12))
            chunk = chunk[mm.end():].strip()
        if not chunk:
            continue
        cells = []
        for c in split_top(chunk, "&"):
            c = c.strip()
            mc = re.match(r"\\multicolumn\{(\d+)\}", c)
            if mc:
                spec2, k = group(c, mc.end())
                content, _ = group(c, k)
                al = [x for x in re.sub(r"@\{[^}]*\}", "", spec2) if x in "lrc"]
                cells.append((content, int(mc.group(1)), al[0] if al else "c"))
            else:
                cells.append((c, 1, None))
        rows.append(cells)
        if pending_above:
            rules.setdefault(("top", len(rows) - 1), []).append((0, len(aligns) - 1, pending_above.pop()))
    return aligns, rows, rules


_FONTS: dict = {}


def text_width_pt(runs, size: float) -> float:
    """Width of runs set on one line, in points, from Liberation Serif / Mono (metric-compatible with Times New
    Roman / Courier New); a per-character estimate if the fonts are not installed."""
    def font(mono, b, i, sz):
        key = (mono, b, i, round(sz * 10))
        if key not in _FONTS:
            fam = "LiberationMono" if mono else "LiberationSerif"
            sty = {(False, False): "Regular", (True, False): "Bold", (False, True): "Italic",
                   (True, True): "BoldItalic"}[(b, i)]
            try:
                from PIL import ImageFont
                _FONTS[key] = ImageFont.truetype(f"{fam}-{sty}.ttf", size=max(1, round(sz * 10)))
            except Exception:
                _FONTS[key] = None
        return _FONTS[key]

    def w(text, r, sz):
        f = font(bool(r.get("mono")), bool(r.get("b")), bool(r.get("i")), sz)
        if f is None:
            return len(text) * sz * (0.6 if r.get("mono") else 0.5)
        return f.getlength(text) / 10

    total = 0.0
    for r in runs:
        sz = size * (0.7 if r.get("sup") or r.get("sub") else 1.0)
        t = r["text"]
        if r.get("sc"):                                   # small capitals: lower case drawn as capitals at 80 %
            low = "".join(c for c in t if c.islower())
            rest = "".join(c for c in t if not c.islower())
            total += w(rest, r, sz) + w(low.upper(), r, sz * 0.8)
        else:
            total += w(t, r, sz)
    return total


def build_table(doc, tex: str, macros, labels, width, size=8.0, sep=6.0):
    aligns, rows, rules = parse_tabular(tex, macros)
    ncol = len(aligns)
    tbl = doc.add_table(rows=len(rows), cols=ncol)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    table_no_borders(tbl)
    pad = int(round(sep * 20))                              # \tabcolsep on each side of a cell, in twips
    cell_margins(tbl, pad, pad)

    # widths as LaTeX sets them: every column as wide as its longest entry (l/c/r columns do not wrap); if the
    # table does not fit, columns shrink toward their longest word, as Word's autofit does
    def widths(content):
        runs = tex_runs(content, {}, labels)
        whole = text_width_pt(runs, size)
        chars = [(c, {k: v for k, v in r.items() if k != "text"}) for r in runs for c in r["text"]]
        words, cur = [], []
        for c, fl in chars + [(" ", {})]:
            if c == " ":
                if cur:
                    words.append(cur)
                cur = []
            else:
                cur.append((c, fl))
        longest = max((text_width_pt([dict(fl, text=c) for c, fl in wd], size) for wd in words), default=0.0)
        slack = 1.03                                        # rounding and kerning differences between readers
        return whole * 20 * slack + 2 * pad, longest * 20 * slack + 2 * pad

    nat, mn = [2.0 * pad + 60] * ncol, [2.0 * pad + 60] * ncol
    spans = []
    for cells in rows:
        k = 0
        for content, span, _ in cells:
            a, b = widths(content)
            if span == 1:
                nat[k], mn[k] = max(nat[k], a), max(mn[k], b)
            else:
                spans.append((k, span, a, b))
            k += span
    for k, span, a, b in spans:
        for vec, need in ((nat, a), (mn, b)):
            have = sum(vec[k:k + span])
            if need > have:
                for c in range(k, k + span):
                    vec[c] += (need - have) / span
    # @{} at either end of the column spec: no outer padding, as in LaTeX
    spec, _ = group(tex, tex.index("\\begin{tabular}") + len("\\begin{tabular}"))
    outer = (spec.startswith("@{}"), spec.endswith("@{}"))
    if outer[0]:
        nat[0] -= pad
        mn[0] -= pad
    if outer[1]:
        nat[-1] -= pad
        mn[-1] -= pad
    if sum(nat) <= width:
        col = nat
    elif sum(mn) <= width:
        f = (width - sum(mn)) / (sum(nat) - sum(mn))
        col = [m + f * (n - m) for n, m in zip(nat, mn)]
    else:
        print(f"warning: a table needs {sum(mn):.0f} twips, {width} available", file=sys.stderr)
        col = [m * width / sum(mn) for m in mn]
    col_tw = [int(c) for c in col]
    for ri, cells in enumerate(rows):
        k = 0
        for content, span, al in cells:
            cell = tbl.cell(ri, k)
            if span > 1:
                cell = cell.merge(tbl.cell(ri, k + span - 1))
            p = cell.paragraphs[0]
            a = al or aligns[k]
            p.alignment = {"l": WD_ALIGN_PARAGRAPH.LEFT, "r": WD_ALIGN_PARAGRAPH.RIGHT,
                           "c": WD_ALIGN_PARAGRAPH.CENTER}[a]
            para_fmt(p, align=p.alignment, before=0, after=0, first=Inches(0))
            add_runs(p, tex_runs(content, {}, labels), size)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            k += span
    for row in tbl.rows:                                    # a merged cell is as wide as the columns it spans
        k = 0
        for tc in row._tr.tc_lst:
            span = tc.grid_span
            tc.width = Twips(sum(col_tw[k:k + span]))
            k += span
    set_grid(tbl, col_tw, fixed=False)
    for row in tbl.rows:
        tcs = row._tr.tc_lst
        for tc, side, on in ((tcs[0], "left", outer[0]), (tcs[-1], "right", outer[1])):
            if on:
                mar = OxmlElement("w:tcMar")
                e = OxmlElement(f"w:{side}")
                e.set(qn("w:w"), "0")
                e.set(qn("w:type"), "dxa")
                mar.append(e)
                tc.get_or_add_tcPr().append(mar)
    for ri, row in enumerate(tbl.rows):
        trpr = row._tr.get_or_add_trPr()
        if trpr.find(qn("w:cantSplit")) is None:
            trpr.append(OxmlElement("w:cantSplit"))
        if ri < len(tbl.rows) - 1:
            for cell in row.cells:
                for para in cell.paragraphs:
                    para.paragraph_format.keep_with_next = True
    # rules
    for key, segs in rules.items():
        if isinstance(key, tuple):
            ri = key[1]
            for a, b, sz in segs:
                for ci in range(a, b + 1):
                    cell_borders(tbl.cell(ri, ci), top=sz)
        elif key >= 0:
            for a, b, sz in segs:
                for ci in range(a, b + 1):
                    cell_borders(tbl.cell(key, ci), bottom=sz)
    return tbl


# --------------------------------------------------------------------------- figures

def render_tikz(tikz: str, labels: dict, out_png: Path, work: Path) -> float:
    code = re.sub(r"\\ref\{([^}]+)\}", lambda m: labels.get(m.group(1), "??"), tikz)
    doc = r"""\documentclass[border=3pt]{standalone}
\usepackage{times}
\usepackage{amsmath,amssymb}
\usepackage{tikz}
\usetikzlibrary{arrows.meta,positioning,calc,fit,backgrounds}
\newcommand{\sres}{\mathsf{res}}\newcommand{\sest}{\mathsf{est}}
\newcommand{\spin}{\mathsf{pin}}\newcommand{\sprov}{\mathsf{prov}}
\newcommand{\dec}[1]{\textsc{#1}}\newcommand{\Reg}{\mathcal{R}}
\begin{document}
""" + code + "\n\\end{document}\n"
    (work / "fig1.tex").write_text(doc, encoding="utf-8")
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "fig1.tex"], cwd=work,
                   check=True, capture_output=True)
    subprocess.run(["pdftoppm", "-r", "600", "-png", "-singlefile", "fig1.pdf", str(out_png.with_suffix(""))],
                   cwd=work, check=True)
    info = subprocess.run(["pdfinfo", str(work / "fig1.pdf")], capture_output=True, text=True).stdout
    w_pt = float(re.search(r"Page size:\s+([\d.]+) x", info).group(1))
    return w_pt / 72.0


def pdf_to_png(pdf: Path, out_png: Path) -> float:
    subprocess.run(["pdftoppm", "-r", "300", "-png", "-singlefile", str(pdf), str(out_png.with_suffix(""))],
                   check=True)
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    return float(re.search(r"Page size:\s+([\d.]+) x", info).group(1)) / 72.0


# --------------------------------------------------------------------------- main build

def main(out_path: Path):
    # numbering and references come from the last LaTeX build, so it must postdate the sources
    built = [PAPER / "main.aux", PAPER / "main.bbl"]
    sources = [PAPER / "main.tex", PAPER / "refs.bib", RESULTS / "macros.tex", *RESULTS.glob("tab_*.tex")]
    if not all(p.exists() for p in built) or \
            min(p.stat().st_mtime for p in built) < max(p.stat().st_mtime for p in sources):
        raise SystemExit("paper/main.aux or paper/main.bbl is missing or older than the sources: run `make paper` first")
    macros = load_macros()
    labels, cites = load_aux()
    tex = (PAPER / "main.tex").read_text(encoding="utf-8")
    tex = strip_comments(tex)
    read_flags(tex)
    tex = tex[tex.index("\\begin{document}") + len("\\begin{document}"):tex.index("\\end{document}")]
    tex = expand_macros(tex, macros)
    tex = resolve_conditionals(tex)
    check_expanded(tex)

    title, _ = group(tex, tex.index("\\title") + len("\\title"))
    m = re.search(r"\\textbf\{Draft\.\}", tex)
    draft_txt = None
    if m:
        k = tex.index("}}", m.end())
        draft_txt = tex[m.end():k].strip()
    abstract = tex[tex.index("\\begin{abstract}") + 16:tex.index("\\end{abstract}")].strip()
    keywords = tex[tex.index("\\begin{IEEEkeywords}") + 20:tex.index("\\end{IEEEkeywords}")].strip()
    body = tex[tex.index("\\section{Introduction}"):tex.index("\\bibliographystyle")]

    # ---- floats -> markers
    floats = {}

    def take_float(mm):
        env, content = mm.group(1), mm.group(2)
        ci = content.index("\\caption")
        cap, _ = group(content, ci + len("\\caption"))
        lab = re.search(r"\\label\{([^}]+)\}", content).group(1)
        f = {"env": env, "caption": cap, "label": lab, "wide": env.endswith("*")}
        if "tikzpicture" in content:
            f["tikz"] = content[content.index("\\begin{tikzpicture}"):content.index("\\end{tikzpicture}") + 17]
        elif "\\includegraphics" in content:
            f["pdf"] = ROOT / "paper" / re.search(r"\\includegraphics\[[^]]*\]\{([^}]+)\}", content).group(1)
        else:
            # the float's tabulars in order, inline or \input from results/, and a note set after the last one
            body_ = re.sub(r"\\input\{([^}]+)\}",
                           lambda m_: (ROOT / "paper" / m_.group(1)).read_text(encoding="utf-8"), content)
            f["tabs"] = re.findall(r"\\begin\{tabular\}.*?\\end\{tabular\}", body_, flags=re.S)
            tail = body_[body_.rindex("\\end{tabular}") + len("\\end{tabular}"):].strip()
            tail = re.sub(r"^\{\\(?:footnotesize|scriptsize|small)\s*(.*)\}$", r"\1", tail, flags=re.S).strip()
            f["note"] = tail
            # the font size and column separation the LaTeX float sets (IEEEtran: \scriptsize 7 pt, \footnotesize 8 pt)
            f["size"] = 8.0
            for cmd, pt in (("\\scriptsize", 7.0), ("\\footnotesize", 8.0), ("\\small", 9.0)):
                if cmd in content:
                    f["size"] = pt
            sep = re.search(r"\\setlength\{\\tabcolsep\}\{([\d.]+)pt\}", content)
            f["sep"] = float(sep.group(1)) if sep else 6.0
        floats[lab] = f
        return f"\n\nFLOATMARK{lab}FLOATEND\n\n"

    body = re.sub(r"\\begin\{(figure\*?|table\*?)\}(?:\[[^]]*\])?(.*?)\\end\{\1\}", take_float, body, flags=re.S)

    # ---- theorem-like environments
    counters = {"proposition": 0, "definition": 0}

    def thm(mm):
        env = mm.group(1)
        counters[env] += 1
        title_ = mm.group(2)
        head = f"{env.capitalize()} {counters[env]}" + (f" ({title_})" if title_ else "")
        return f"\n\n\\textit{{{head}:}} "

    body = re.sub(r"\\begin\{(proposition|definition)\}(?:\[([^]]*)\])?", thm, body)
    body = re.sub(r"\\end\{(proposition|definition)\}", "\n\n", body)
    body = re.sub(r"\\begin\{IEEEproof\}", "\n\n\\\\textit{Proof:} ", body)
    body = re.sub(r"\\end\{IEEEproof\}", " \u25a0\n\n", body)

    # ---- sections with IEEE numbering
    sec, sub = 0, 0

    def secrep(mm):
        nonlocal sec, sub
        kind, star = mm.group(1), mm.group(2)
        text, _ = group(mm.string, mm.end() - 1)
        if star:
            return f"\\section*{{{text}}}"
        if kind == "section":
            sec += 1
            sub = 0
            return f"\\section*{{{roman(sec)}. {text}}}"
        sub += 1
        return f"\\subsection*{{{chr(64 + sub)}. {text}}}"

    out, i = [], 0
    for mm in re.finditer(r"\\(section|subsection)(\*?)\{", body):
        if mm.start() < i:
            continue
        out.append(body[i:mm.start()])
        text, j = group(body, mm.end() - 1)
        out.append(secrep(mm))
        i = j
    out.append(body[i:])
    body = "".join(out)

    # ---- references, citations, labels
    body = re.sub(r"\\ref\{([^}]+)\}", lambda mm: labels[mm.group(1)], body)
    body = re.sub(r"\\cite\{([^}]+)\}", lambda mm: cite_text(mm.group(1), cites), body)
    body = re.sub(r"\\label\{[^}]+\}", "", body)
    body = body.replace("\\noindent", "")
    body = dec_in_math(body)
    body = mono_hyphens(body)

    pre = r"""\newcommand{\sres}{\mathsf{res}}
\newcommand{\sest}{\mathsf{est}}
\newcommand{\spin}{\mathsf{pin}}
\newcommand{\sprov}{\mathsf{prov}}
\newcommand{\dec}[1]{\textsc{#1}}
\newcommand{\Reg}{\mathcal{R}}
\newcommand{\Prof}{\mathcal{P}}
\newcommand{\Dec}{\mathrm{dec}}
"""
    work = Path(tempfile.mkdtemp(prefix="docx_build_"))
    (work / "body.tex").write_text(pre + body, encoding="utf-8")
    ref = work / "ref.docx"
    make_reference(ref)
    subprocess.run(["pandoc", "body.tex", "-f", "latex", "-t", "docx", "--reference-doc", str(ref), "-o",
                    "body.docx"], cwd=work, check=True)

    doc = Document(str(work / "body.docx"))
    fix_omml(doc.element.body)
    style_body(doc)

    # ---- title block and abstract at the top
    first = doc.element.body[0]
    blocks = []
    p = doc.add_paragraph()
    para_fmt(p, align=WD_ALIGN_PARAGRAPH.CENTER, after=10, first=Inches(0), line=1.0)
    add_runs(p, tex_runs(title), 24)
    blocks.append(p)
    p = doc.add_paragraph()
    para_fmt(p, align=WD_ALIGN_PARAGRAPH.CENTER, after=10, first=Inches(0))
    add_runs(p, [Run(text="Anonymous Author(s)")], 11)
    blocks.append(p)
    if draft_txt:
        p = doc.add_paragraph()
        para_fmt(p, align=WD_ALIGN_PARAGRAPH.JUSTIFY, after=10, first=Inches(0), left=Inches(0.5),
                 right=Inches(0.5))
        box(p, "C00000", "FDF2F2")
        add_runs(p, [Run(text="Draft. ", b=True)] + tex_runs(draft_txt, {}, labels) +
                 [Run(text=" This Word version is generated from the LaTeX source of the same build; the PDF built "
                           "from LaTeX is the reference layout.")], 8)
        blocks.append(p)
    p = doc.add_paragraph()
    tiny_paragraph(p)
    end_section_here(p, 1, continuous=False)
    blocks.append(p)
    p = doc.add_paragraph()
    para_fmt(p, align=WD_ALIGN_PARAGRAPH.JUSTIFY, after=6, first=Inches(0.125))
    add_runs(p, [Run(text="Abstract", b=True, i=True), Run(text="—", b=True)], 9)
    add_runs(p, tex_runs(abstract, {}, labels), 9, b=True)
    blocks.append(p)
    p = doc.add_paragraph()
    para_fmt(p, align=WD_ALIGN_PARAGRAPH.JUSTIFY, after=8, first=Inches(0.125))
    add_runs(p, [Run(text="Index Terms", b=True, i=True), Run(text="—", b=True)], 9)
    add_runs(p, tex_runs(keywords, {}, labels), 9, b=True)
    blocks.append(p)
    for b in blocks:
        first.addprevious(b._p)

    # ---- floats
    work_img = work / "img"
    work_img.mkdir()
    fig_no = {lab: labels[lab] for lab in floats if lab.startswith("fig:")}
    for p in list(doc.paragraphs):
        mm = re.search(r"FLOATMARK(.+?)FLOATEND", p.text)
        if not mm:
            continue
        f = floats[mm.group(1)]
        anchor = p._p
        if "tabs" in f:
            width = int((FULL_W if f["wide"] else COL_W) / 635)          # EMU -> twips
            cap1 = doc.add_paragraph()
            para_fmt(cap1, align=WD_ALIGN_PARAGRAPH.CENTER, before=6, after=0, first=Inches(0), keep_next=True)
            add_runs(cap1, [Run(text=f"TABLE {labels[f['label']]}")], 8)
            cap2 = doc.add_paragraph()
            para_fmt(cap2, align=WD_ALIGN_PARAGRAPH.CENTER, after=4, first=Inches(0), keep_next=True)
            add_runs(cap2, tex_runs(f["caption"], {"sc": True}, labels), 8)
            seq = [cap1._p, cap2._p]

            def fix_tab(t_):
                # citations, daggers and check marks inside an inline table, as the PDF prints them
                t_ = re.sub(r"\\cite(?:\[[^]]*\])?\{([^}]+)\}", lambda m_: cite_text(m_.group(1), cites), t_)
                return t_.replace("$^\\dagger$", "\u2020").replace("\\checkmark", "\u2713")

            for ti, ttex in enumerate(map(fix_tab, f["tabs"])):
                if ti:
                    gap = doc.add_paragraph()
                    tiny_paragraph(gap)
                    para_fmt(gap, after=4)
                    seq.append(gap._p)
                tbl = build_table(doc, ttex, macros, labels, width, f["size"], f["sep"])
                seq.append(tbl._tbl)
            if f.get("note"):
                note = doc.add_paragraph()
                para_fmt(note, align=WD_ALIGN_PARAGRAPH.LEFT, before=2, after=0, first=Inches(0))
                add_runs(note, tex_runs(expand_macros(fix_tab(f["note"]), macros), {}, labels), f["size"])
                seq.append(note._p)
            after = doc.add_paragraph()
            tiny_paragraph(after)
            para_fmt(after, after=6)
            seq.append(after._p)
        else:
            if "tikz" in f:
                png = work_img / "fig1.png"
                w = render_tikz(f["tikz"], labels, png, work)
            else:
                png = work_img / (f["pdf"].stem + ".png")
                w = pdf_to_png(f["pdf"], png)
            pic = doc.add_paragraph()
            para_fmt(pic, align=WD_ALIGN_PARAGRAPH.CENTER, before=6, after=2, first=Inches(0), keep_next=True)
            pic.add_run().add_picture(str(png), width=Inches(min(w, 3.45)))
            cap = doc.add_paragraph()
            para_fmt(cap, align=WD_ALIGN_PARAGRAPH.JUSTIFY, after=8, first=Inches(0))
            add_runs(cap, [Run(text=f"Fig. {fig_no[f['label']]}. ")] + tex_runs(f["caption"], {}, labels), 8)
            seq = [pic._p, cap._p]
        if f.get("wide"):
            # a full-width table needs its own one-column section between two-column ones
            before = doc.add_paragraph()
            tiny_paragraph(before)
            end_section_here(before, 2, continuous=True)
            seq = [before._p] + seq
            end_section_here(after, 1, continuous=True)
        for el in seq:
            anchor.addprevious(el)
        anchor.getparent().remove(anchor)

    # ---- equation numbers: each display equation goes into a two-cell borderless table
    eq = 0
    for p in list(doc.paragraphs):
        if p._p.find(f".//{{{M_NS}}}oMathPara") is None:
            continue
        eq += 1
        tbl = doc.add_table(rows=1, cols=2)
        table_no_borders(tbl)
        cell_margins(tbl, 0, 0)
        tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
        c0, c1 = tbl.cell(0, 0), tbl.cell(0, 1)
        c0.width, c1.width = Twips(4500), Twips(540)
        set_grid(tbl, [4500, 540])
        c0.vertical_alignment = c1.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        p._p.addprevious(tbl._tbl)
        c0._tc.remove(c0.paragraphs[0]._p)
        c0._tc.append(p._p)
        ppr = p._p.get_or_add_pPr()
        for tag in ("w:numPr", "w:ind"):
            el = ppr.find(qn(tag))
            if el is not None:
                ppr.remove(el)
        para_fmt(p, align=WD_ALIGN_PARAGRAPH.CENTER, before=2, after=2, first=Inches(0))
        q = c1.paragraphs[0]
        para_fmt(q, align=WD_ALIGN_PARAGRAPH.RIGHT, first=Inches(0))
        add_runs(q, [Run(text=f"({eq})")], 10)
    n_eq_tex = len(re.findall(r"\\begin\{equation\}", body))
    if eq != n_eq_tex:
        raise SystemExit(f"equations: {eq} in Word, {n_eq_tex} in LaTeX")

    # ---- references from the LaTeX build (main.bbl): same order, numbers and formatting as the PDF
    h = doc.add_paragraph(style=style_by_name(doc, "Heading 1"))
    h.add_run("References")
    bbl = (PAPER / "main.bbl").read_text(encoding="utf-8")
    items = re.split(r"\\bibitem\{([^}]+)\}", bbl)[1:]
    for k in range(0, len(items), 2):
        key, text = items[k], items[k + 1]
        text = text.split("\\end{thebibliography}")[0].strip()
        n = cites[key]
        p = doc.add_paragraph()
        para_fmt(p, align=WD_ALIGN_PARAGRAPH.LEFT, after=0, first=-Inches(0.3), left=Inches(0.3))   # as IEEEtran
        p.paragraph_format.tab_stops.add_tab_stop(Inches(0.3))
        add_runs(p, [Run(text=f"[{n}]\t")] + tex_runs(text, {}, labels), 8)

    # ---- final section: two columns, continuous after the title block
    body_el = doc.element.body
    last = body_el.find(qn("w:sectPr"))
    if last is not None:
        body_el.remove(last)
    body_el.append(sect_pr(2, continuous=True))

    # ---- properties: anonymous
    cp = doc.core_properties
    cp.title = re.sub(r"\s+", " ", title)
    cp.author = ""
    cp.last_modified_by = ""
    cp.comments = ""
    cp.keywords = re.sub(r"\s+", " ", keywords)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None)
    cp.created = now
    cp.modified = now
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    finalize_package(out_path)
    shutil.rmtree(work, ignore_errors=True)
    print(out_path)


# --------------------------------------------------------------------------- schema order
# Child order of the containers that pandoc or python-docx can leave out of order, taken from
# the ISO/IEC 29500 strict schemas (wml.xsd, shared-math.xsd).
ORDER = {
    (W_NS, "tblPr"): ["tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize",
                      "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar",
                      "tblLook", "tblCaption", "tblDescription", "tblPrChange"],
    (W_NS, "tcPr"): ["cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
                     "textDirection", "tcFitText", "vAlign", "hideMark", "headers", "cellIns", "cellDel", "cellMerge",
                     "tcPrChange"],
    (W_NS, "pPr"): ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
                    "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
                    "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
                    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
                    "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
                    "sectPr", "pPrChange"],
    (W_NS, "rPr"): ["ins", "del", "moveFrom", "moveTo", "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps",
                    "smallCaps", "strike", "dstrike", "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid",
                    "vanish", "webHidden", "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u",
                    "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
                    "specVanish", "oMath", "rPrChange"],
    (W_NS, "style"): ["name", "aliases", "basedOn", "next", "link", "autoRedefine", "hidden", "uiPriority",
                      "semiHidden", "unhideWhenUsed", "qFormat", "locked", "personal", "personalCompose",
                      "personalReply", "rsid", "pPr", "rPr", "tblPr", "trPr", "tcPr", "tblStylePr"],
    (W_NS, "sectPr"): ["headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar",
                       "paperSrc", "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign", "noEndnote",
                       "titlePg", "textDirection", "bidi", "rtlGutter", "docGrid", "printerSettings", "sectPrChange"],
    (W_NS, "settings"): ["writeProtection", "view", "zoom", "removePersonalInformation", "removeDateAndTime",
                         "doNotDisplayPageBoundaries", "displayBackgroundShape", "printPostScriptOverText",
                         "printFractionalCharacterWidth", "printFormsData", "embedTrueTypeFonts", "embedSystemFonts",
                         "saveSubsetFonts", "saveFormsData", "mirrorMargins", "alignBordersAndEdges",
                         "bordersDoNotSurroundHeader", "bordersDoNotSurroundFooter", "gutterAtTop",
                         "hideSpellingErrors", "hideGrammaticalErrors", "activeWritingStyle", "proofState",
                         "formsDesign", "attachedTemplate", "linkStyles", "stylePaneFormatFilter",
                         "stylePaneSortMethod", "documentType", "mailMerge", "revisionView", "trackRevisions",
                         "doNotTrackMoves", "doNotTrackFormatting", "documentProtection", "autoFormatOverride",
                         "styleLockTheme", "styleLockQFSet", "defaultTabStop", "autoHyphenation",
                         "consecutiveHyphenLimit", "hyphenationZone", "doNotHyphenateCaps", "showEnvelope",
                         "summaryLength", "clickAndTypeStyle", "defaultTableStyle", "evenAndOddHeaders",
                         "bookFoldRevPrinting", "bookFoldPrinting", "bookFoldPrintingSheets",
                         "drawingGridHorizontalSpacing", "drawingGridVerticalSpacing",
                         "displayHorizontalDrawingGridEvery", "displayVerticalDrawingGridEvery",
                         "doNotUseMarginsForDrawingGridOrigin", "drawingGridHorizontalOrigin",
                         "drawingGridVerticalOrigin", "doNotShadeFormData", "noPunctuationKerning",
                         "characterSpacingControl", "printTwoOnOne", "strictFirstAndLastChars", "noLineBreaksAfter",
                         "noLineBreaksBefore", "savePreviewPicture", "doNotValidateAgainstSchema", "saveInvalidXml",
                         "ignoreMixedContent", "alwaysShowPlaceholderText", "doNotDemarcateInvalidXml",
                         "saveXmlDataOnly", "useXSLTWhenSaving", "saveThroughXslt", "showXMLTags",
                         "alwaysMergeEmptyNamespace", "updateFields", "hdrShapeDefaults", "footnotePr", "endnotePr",
                         "compat", "docVars", "rsids", "mathPr", "attachedSchema", "themeFontLang",
                         "clrSchemeMapping", "doNotIncludeSubdocsInStats", "doNotAutoCompressPictures",
                         "forceUpgrade", "captions", "readModeInkLockDown", "smartTagType", "schemaLibrary",
                         "shapeDefaults", "doNotEmbedSmartTags", "decimalSymbol", "listSeparator"],
    (M_NS, "rPr"): ["lit", "nor", "scr", "sty", "brk", "aln"],
    (M_NS, "dPr"): ["begChr", "sepChr", "endChr", "grow", "shp", "ctrlPr"],
    (M_NS, "mcPr"): ["count", "mcJc"],
    (M_NS, "mPr"): ["baseJc", "plcHide", "rSpRule", "cGpRule", "rSp", "cSp", "cGp", "mcs", "ctrlPr"],
    (M_NS, "naryPr"): ["chr", "limLoc", "grow", "subHide", "supHide", "ctrlPr"],
}
ELEMENT_ONLY = {(W_NS, "rPr"), (W_NS, "pPr"), (W_NS, "style"), (W_NS, "tblPr"), (W_NS, "tcPr")}


def schema_order(root):
    from lxml import etree
    for el in root.iter():
        if not isinstance(el.tag, str) or "}" not in el.tag:
            continue
        ns, local = el.tag[1:].split("}")
        key = (ns, local)
        if key in ELEMENT_ONLY:
            el.text = None                                    # stray characters (pandoc's reference.docx)
            for ch in el:
                ch.tail = None
        if key == (M_NS, "rPr") and el.find(f"{{{M_NS}}}nor") is not None:
            for ch in list(el):                               # CT_RPR: nor | (scr?, sty?), not both
                if ch.tag in (f"{{{M_NS}}}scr", f"{{{M_NS}}}sty"):
                    el.remove(ch)
        if key not in ORDER:
            continue
        rank = {name: i for i, name in enumerate(ORDER[key])}
        kids = list(el)
        kids.sort(key=lambda c: rank.get(c.tag.split("}")[1], len(rank)) if isinstance(c.tag, str) else len(rank))
        for c in kids:
            el.remove(c)
            el.append(c)


def finalize_package(path: Path):
    """Schema order in every WordprocessingML part, 8-digit list ids; repack."""
    import zipfile
    from lxml import etree
    src = path.read_bytes()
    zin = zipfile.ZipFile(__import__("io").BytesIO(src))
    out = __import__("io").BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename in ("word/document.xml", "word/styles.xml", "word/settings.xml", "word/numbering.xml"):
                root = etree.fromstring(data)
                if info.filename == "word/settings.xml":
                    # anonymous review copy: Word strips author names and revision times when the file is
                    # saved again (File > Info > Inspect Document does the same by hand)
                    # hyphenate the justified two-column text as LaTeX does (acronyms left whole)
                    for name in ("removePersonalInformation", "removeDateAndTime", "autoHyphenation",
                                 "doNotHyphenateCaps"):
                        if root.find(f"{{{W_NS}}}{name}") is None:
                            etree.SubElement(root, f"{{{W_NS}}}{name}")
                    # current Word layout rules (without this Word opens the file in compatibility mode)
                    if root.find(f"{{{W_NS}}}compat") is None:
                        compat = etree.SubElement(root, f"{{{W_NS}}}compat")
                        cs = etree.SubElement(compat, f"{{{W_NS}}}compatSetting")
                        cs.set(f"{{{W_NS}}}name", "compatibilityMode")
                        cs.set(f"{{{W_NS}}}uri", "http://schemas.microsoft.com/office/word")
                        cs.set(f"{{{W_NS}}}val", "15")
                if info.filename == "word/settings.xml":
                    for tag in ("rsids", "attachedTemplate"):
                        for el in root.findall(f"{{{W_NS}}}{tag}"):
                            root.remove(el)
                schema_order(root)
                if info.filename == "word/numbering.xml":
                    for nsid in root.iter(f"{{{W_NS}}}nsid"):
                        v = nsid.get(f"{{{W_NS}}}val")
                        nsid.set(f"{{{W_NS}}}val", v.upper().rjust(8, "0"))
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            if info.filename == "docProps/core.xml":
                # no creator, editor, revision count or description in the anonymous copy
                root = etree.fromstring(data)
                for el in list(root):
                    if el.tag.split("}")[1] in ("creator", "lastModifiedBy", "revision", "description",
                                                "lastPrinted", "category", "contentStatus"):
                        root.remove(el)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            if info.filename == "docProps/app.xml":
                # pandoc's reference document carries the statistics of its own sample text; Word recounts
                root = etree.fromstring(data)
                for el in list(root):
                    if el.tag.split("}")[1] in ("Words", "Lines", "Characters", "CharactersWithSpaces", "Paragraphs",
                                                "Pages", "TotalTime", "Company", "Manager", "HyperlinkBase"):
                        root.remove(el)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zi = zipfile.ZipInfo(info.filename, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(zi, data)
    path.write_bytes(out.getvalue())


def box(p, border="C00000", fill="FDF2F2"):
    ppr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    for edge in ("top", "left", "bottom", "right"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "6")
        e.set(qn("w:space"), "4")
        e.set(qn("w:color"), border)
        bdr.append(e)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    # schema order in pPr: ... pBdr, shd, tabs, ..., spacing, ind, ..., jc
    anchor = None
    for tag in ("w:tabs", "w:suppressAutoHyphens", "w:spacing", "w:ind", "w:jc"):
        anchor = ppr.find(qn(tag))
        if anchor is not None:
            break
    if anchor is not None:
        anchor.addprevious(bdr)
        anchor.addprevious(shd)
    else:
        ppr.append(bdr)
        ppr.append(shd)


def style_by_name(d, name):
    """python-docx maps 'Heading 1' to Word's internal 'heading 1'; pandoc's file keeps the UI name."""
    for st in d.styles:
        if st.name == name or st.style_id == name.replace(" ", ""):
            return st
    raise KeyError(name)


def make_reference(path: Path):
    """pandoc's reference document with IEEE conference styles."""
    data = subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"], capture_output=True,
                          check=True).stdout
    path.write_bytes(data)
    d = Document(str(path))
    for name in ("Normal", "Body Text", "First Paragraph", "Compact", "Block Text"):
        if name not in [s.name for s in d.styles]:
            continue
        st = style_by_name(d, name)
        st.font.name = FONT
        st.font.size = Pt(10)
        st.font.color.rgb = RGBColor(0, 0, 0)
        rpr = st.element.get_or_add_rPr()
        f = rpr.find(qn("w:rFonts"))
        if f is None:
            f = OxmlElement("w:rFonts")
            rpr.insert(0, f)
        for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            f.set(qn(a), FONT)
        for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
            if f.get(qn(a)) is not None:
                del f.attrib[qn(a)]
        pf = st.paragraph_format
        pf.space_before, pf.space_after = Pt(0), Pt(0)
        pf.line_spacing = 1.0
        if name in ("Body Text", "First Paragraph", "Normal"):
            pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf.first_line_indent = Inches(0.14) if name != "Normal" else None
    for name, sc, it, al, before, after in (("Heading 1", True, False, WD_ALIGN_PARAGRAPH.CENTER, 8, 4),
                                            ("Heading 2", False, True, WD_ALIGN_PARAGRAPH.LEFT, 6, 3)):
        st = style_by_name(d, name)
        st.font.name = FONT
        st.font.size = Pt(10)
        st.font.bold = False
        st.font.italic = it
        st.font.small_caps = sc
        st.font.color.rgb = RGBColor(0, 0, 0)
        rpr = st.element.get_or_add_rPr()
        f = rpr.find(qn("w:rFonts"))
        if f is None:
            f = OxmlElement("w:rFonts")
            rpr.insert(0, f)
        for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            f.set(qn(a), FONT)
        for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
            if f.get(qn(a)) is not None:
                del f.attrib[qn(a)]
        pf = st.paragraph_format
        pf.alignment = al
        pf.space_before, pf.space_after = Pt(before), Pt(after)
        pf.keep_with_next = True
    for name in ("Verbatim Char",):
        if name in [s.name for s in d.styles]:
            st = style_by_name(d, name)
            st.font.name = MONO
            st.font.size = Pt(9)
    d.save(str(path))


def style_body(doc):
    """Body paragraphs: Times 10 pt, justified; list paragraphs keep pandoc's numbering."""
    for p in doc.paragraphs:
        for r in p.runs:
            if r.font.name is None and r.style is not None and r.style.name == "Verbatim Char":
                continue


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "paper_draft.docx"
    main(out)
