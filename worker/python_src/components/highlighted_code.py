"""Lightweight ANSI syntax highlighting for TUI code blocks."""

from __future__ import annotations

import io
import json
import keyword
import re
import tokenize
from functools import lru_cache
from typing import Mapping

try:
    from pygments import lex as _pygments_lex
    from pygments.lexers import get_lexer_by_name as _get_pygments_lexer_by_name
    from pygments.token import Token as _PygmentsToken
    from pygments.util import ClassNotFound as _PygmentsClassNotFound
except Exception:  # pragma: no cover - exercised when pygments is unavailable
    _pygments_lex = None
    _get_pygments_lexer_by_name = None
    _PygmentsToken = None
    _PygmentsClassNotFound = Exception

ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_DIM = "\x1b[2m"
ANSI_BLACK = "\x1b[30m"
ANSI_RED = "\x1b[31m"
ANSI_BLUE = "\x1b[34m"
ANSI_CYAN = "\x1b[36m"
ANSI_GREEN = "\x1b[32m"
ANSI_MAGENTA = "\x1b[35m"
ANSI_YELLOW = "\x1b[33m"
ANSI_BRIGHT_BLACK = "\x1b[90m"

AnsiStyle = tuple[str, ...]
GroupedAnsiStyle = tuple[AnsiStyle | None, AnsiStyle | None]

_JS_TS_KEYWORDS = frozenset(
    {
        "as",
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "finally",
        "for",
        "from",
        "function",
        "if",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "let",
        "new",
        "private",
        "protected",
        "public",
        "readonly",
        "return",
        "static",
        "super",
        "switch",
        "throw",
        "try",
        "type",
        "typeof",
        "var",
        "void",
        "while",
        "yield",
    }
)
_JS_TS_LITERALS = frozenset({"true", "false", "null", "undefined"})
_C_LIKE_KEYWORDS = frozenset(
    {
        "break",
        "case",
        "const",
        "continue",
        "default",
        "else",
        "enum",
        "extern",
        "fn",
        "for",
        "func",
        "goto",
        "if",
        "impl",
        "import",
        "interface",
        "let",
        "loop",
        "match",
        "mod",
        "package",
        "pub",
        "return",
        "struct",
        "switch",
        "trait",
        "type",
        "unsafe",
        "use",
        "var",
        "while",
    }
)
_C_LIKE_LITERALS = frozenset({"true", "false", "nil", "null"})
_SQL_KEYWORDS = frozenset(
    {
        "alter",
        "and",
        "as",
        "by",
        "case",
        "create",
        "delete",
        "desc",
        "distinct",
        "drop",
        "else",
        "end",
        "from",
        "group",
        "having",
        "insert",
        "into",
        "join",
        "left",
        "limit",
        "not",
        "null",
        "offset",
        "on",
        "or",
        "order",
        "right",
        "select",
        "set",
        "table",
        "then",
        "union",
        "update",
        "values",
        "when",
        "where",
    }
)
_RUBY_KEYWORDS = frozenset(
    {
        "alias",
        "begin",
        "break",
        "case",
        "class",
        "def",
        "do",
        "else",
        "elsif",
        "end",
        "ensure",
        "false",
        "if",
        "module",
        "next",
        "nil",
        "redo",
        "require",
        "rescue",
        "return",
        "self",
        "super",
        "then",
        "true",
        "unless",
        "until",
        "when",
        "while",
        "yield",
    }
)

_THEME_PALETTES: dict[str, dict[str, AnsiStyle]] = {
    "dark": {
        "fallback": (ANSI_BRIGHT_BLACK,),
        "comment": (ANSI_BRIGHT_BLACK,),
        "preproc": (ANSI_DIM, ANSI_MAGENTA),
        "string": (ANSI_GREEN,),
        "regex": (ANSI_BOLD, ANSI_GREEN),
        "number": (ANSI_YELLOW,),
        "keyword": (ANSI_MAGENTA,),
        "type": (ANSI_BOLD, ANSI_BLUE),
        "identifier": (ANSI_CYAN,),
        "namespace": (ANSI_DIM, ANSI_CYAN),
        "function": (ANSI_CYAN,),
        "class": (ANSI_BOLD, ANSI_BLUE),
        "decorator": (ANSI_DIM, ANSI_MAGENTA),
        "builtin": (ANSI_BOLD, ANSI_CYAN),
        "literal": (ANSI_YELLOW,),
        "operator": (ANSI_BRIGHT_BLACK,),
        "command": (ANSI_BOLD, ANSI_CYAN),
        "flag": (ANSI_BLUE,),
        "variable": (ANSI_YELLOW,),
        "heading": (ANSI_BOLD, ANSI_BLUE),
        "quote": (ANSI_DIM, ANSI_BRIGHT_BLACK),
        "label": (ANSI_DIM, ANSI_BLUE),
        "code_prefix": (ANSI_DIM, ANSI_BRIGHT_BLACK),
        "strong": (ANSI_BOLD, ANSI_CYAN),
        "emphasis": (ANSI_DIM, ANSI_CYAN),
        "inserted": (ANSI_GREEN,),
        "deleted": (ANSI_MAGENTA,),
    },
    "light": {
        "fallback": (ANSI_DIM, ANSI_BLACK),
        "comment": (ANSI_DIM, ANSI_BLUE),
        "preproc": (ANSI_DIM, ANSI_MAGENTA),
        "string": (ANSI_GREEN,),
        "regex": (ANSI_BOLD, ANSI_GREEN),
        "number": (ANSI_MAGENTA,),
        "keyword": (ANSI_RED,),
        "type": (ANSI_BOLD, ANSI_MAGENTA),
        "identifier": (ANSI_BLUE,),
        "namespace": (ANSI_DIM, ANSI_BLUE),
        "function": (ANSI_BLUE,),
        "class": (ANSI_BOLD, ANSI_MAGENTA),
        "decorator": (ANSI_DIM, ANSI_RED),
        "builtin": (ANSI_BOLD, ANSI_BLUE),
        "literal": (ANSI_RED,),
        "operator": (ANSI_DIM, ANSI_BLACK),
        "command": (ANSI_BOLD, ANSI_BLUE),
        "flag": (ANSI_MAGENTA,),
        "variable": (ANSI_RED,),
        "heading": (ANSI_BOLD, ANSI_MAGENTA),
        "quote": (ANSI_DIM, ANSI_BLUE),
        "label": (ANSI_DIM, ANSI_BLUE),
        "code_prefix": (ANSI_DIM, ANSI_BLUE),
        "strong": (ANSI_BOLD, ANSI_RED),
        "emphasis": (ANSI_DIM, ANSI_MAGENTA),
        "inserted": (ANSI_GREEN,),
        "deleted": (ANSI_RED,),
    },
}

_EXTRA_LEXER_ALIASES = {
    "dart": "dart",
    "scala": "scala",
    "r": "r",
    "julia": "julia",
    "lua": "lua",
    "perl": "perl",
    "hcl": "hcl",
    "tf": "hcl",
    "dockerfile": "docker",
    "docker": "docker",
    "nginx": "nginx",
    "gradle": "groovy",
    "cmake": "cmake",
    "makefile": "makefile",
    "ini": "ini",
    "conf": "nginx",
    "vue": "html+django",
    "svelte": "html",
    "astro": "html",
    "php": "php",
}

_PYGMENTS_LEXER_ALIASES = {
    "py": "python",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "js": "javascript",
    "jsx": "jsx",
    "ts": "typescript",
    "tsx": "tsx",
    "yml": "yaml",
    "patch": "diff",
    "svg": "xml",
    "rb": "ruby",
    "md": "markdown",
    "kt": "kotlin",
    "rs": "rust",
    "cpp": "cpp",
    "c++": "cpp",
}
_PYGMENTS_LEXER_ALIASES.update(_EXTRA_LEXER_ALIASES)


def wrap_ansi(text: str, *codes: str) -> str:
    if not text or not codes:
        return text
    return "".join(codes) + text + ANSI_RESET


def normalize_highlight_theme(theme: str | None) -> str:
    normalized = (theme or "").strip().lower()
    if normalized.startswith("light"):
        return "light"
    return "dark"


def get_highlight_palette(theme: str | None = None) -> Mapping[str, AnsiStyle]:
    return _THEME_PALETTES[normalize_highlight_theme(theme)]


def highlight_code_fragment(
    text: str,
    language: str | None = None,
    *,
    theme: str | None = None,
) -> str:
    normalized = (language or "").strip().lower()
    palette = get_highlight_palette(theme)
    semantic = _highlight_with_pygments(text, normalized, palette)
    if semantic is not None:
        return semantic
    if normalized in {"python", "py"}:
        return _highlight_python(text, palette)
    if normalized in {"json", "jsonc"}:
        return _highlight_json(text, palette)
    if normalized in {"bash", "sh", "shell", "zsh"}:
        return _highlight_shell(text, palette)
    if normalized in {"javascript", "js", "jsx", "typescript", "ts", "tsx"}:
        return _highlight_c_like(text, palette, _JS_TS_KEYWORDS, _JS_TS_LITERALS)
    if normalized in {"c", "cc", "cpp", "c++", "go", "java", "kotlin", "kt", "rs", "rust", "swift"}:
        return _highlight_c_like(text, palette, _C_LIKE_KEYWORDS, _C_LIKE_LITERALS)
    if normalized in {"yaml", "yml"}:
        return _highlight_yaml(text, palette)
    if normalized in {"diff", "patch"}:
        return _highlight_diff(text, palette)
    if normalized in {"html", "xml", "svg"}:
        return _highlight_markup(text, palette)
    if normalized in {"sql"}:
        return _highlight_sql(text, palette)
    if normalized in {"toml"}:
        return _highlight_toml(text, palette)
    if normalized in {"css"}:
        return _highlight_css(text, palette)
    if normalized in {"ruby", "rb"}:
        return _highlight_ruby(text, palette)
    if normalized in {"markdown", "md"}:
        return _highlight_markdown(text, palette)
    if normalized in {"dockerfile", "docker"}:
        return _highlight_dockerfile(text, palette)
    if normalized in {"lua"}:
        return _highlight_lua(text, palette)
    if normalized in {"r"}:
        return _highlight_r(text, palette)
    if normalized in {"perl"}:
        return _highlight_perl(text, palette)
    if normalized in {"hcl", "tf"}:
        return _highlight_hcl(text, palette)
    if normalized in {"ini"}:
        return _highlight_ini(text, palette)
    if normalized in {"makefile", "make"}:
        return _highlight_makefile(text, palette)
    return wrap_ansi(text, *palette["fallback"])


@lru_cache(maxsize=64)
def _resolve_pygments_lexer_name(language: str) -> str | None:
    normalized = language.strip().lower()
    if not normalized or _get_pygments_lexer_by_name is None:
        return None
    candidates = [normalized]
    alias = _PYGMENTS_LEXER_ALIASES.get(normalized)
    if alias is not None and alias != normalized:
        candidates.append(alias)
    for candidate in candidates:
        try:
            _get_pygments_lexer_by_name(candidate)
        except _PygmentsClassNotFound:
            continue
        return candidate
    return None


def _highlight_with_pygments(
    text: str,
    language: str,
    palette: Mapping[str, AnsiStyle],
) -> str | None:
    if not text or _pygments_lex is None or _get_pygments_lexer_by_name is None:
        return None
    lexer_name = _resolve_pygments_lexer_name(language)
    if lexer_name is None:
        return None
    try:
        lexer = _get_pygments_lexer_by_name(lexer_name)
        tokens = list(_pygments_lex(text, lexer))
        return "".join(
            _style_pygments_token(
                token_text,
                token_type,
                palette,
                language=lexer_name,
            )
            for token_type, token_text in tokens
        )
    except Exception:
        return None


def _style_pygments_token(
    token_text: str,
    token_type: object,
    palette: Mapping[str, AnsiStyle],
    *,
    language: str,
) -> str:
    style = _pygments_token_style(token_type, palette, language=language)
    if style is None or not token_text.strip():
        return token_text
    return wrap_ansi(token_text, *style)


def _pygments_token_style(
    token_type: object,
    palette: Mapping[str, AnsiStyle],
    *,
    language: str,
) -> AnsiStyle | None:
    if _PygmentsToken is None:
        return None
    if token_type in _PygmentsToken.Comment.Preproc:
        return palette["preproc"]
    if token_type in _PygmentsToken.Comment:
        return palette["comment"]
    if token_type in _PygmentsToken.Generic.Inserted:
        return palette["inserted"]
    if token_type in _PygmentsToken.Generic.Deleted:
        return palette["deleted"]
    if token_type in _PygmentsToken.Generic.Heading:
        return palette["heading"]
    if token_type in _PygmentsToken.Generic.Subheading:
        return palette["label"]
    if token_type in _PygmentsToken.Generic.Strong:
        return palette["strong"]
    if token_type in _PygmentsToken.Generic.Emph:
        return palette["emphasis"]
    if token_type in _PygmentsToken.Generic.Prompt:
        return palette["label"]
    if token_type in _PygmentsToken.Literal.String.Regex:
        return palette["regex"]
    if token_type in _PygmentsToken.Literal.String.Affix:
        return palette["label"]
    if token_type in _PygmentsToken.String:
        return palette["string"]
    if token_type in _PygmentsToken.Number:
        return palette["number"]
    if token_type in _PygmentsToken.Keyword.Type:
        return palette["type"]
    if token_type in _PygmentsToken.Keyword.Constant:
        return palette["literal"]
    if token_type in _PygmentsToken.Keyword:
        return palette["keyword"]
    if token_type in _PygmentsToken.Name.Decorator:
        return palette["decorator"]
    if token_type in _PygmentsToken.Name.Namespace:
        return palette["namespace"]
    if token_type in _PygmentsToken.Name.Attribute:
        return palette["identifier"]
    if token_type in _PygmentsToken.Name.Property:
        return palette["identifier"]
    if token_type in _PygmentsToken.Name.Tag:
        if language in {"json", "jsonc", "yaml"}:
            return palette["identifier"]
        return palette["keyword"]
    if token_type in _PygmentsToken.Name.Variable:
        return palette["variable"]
    if token_type in _PygmentsToken.Name.Builtin:
        return palette["builtin"]
    if token_type in _PygmentsToken.Name.Class:
        return palette["class"]
    if token_type in _PygmentsToken.Name.Exception:
        return palette["class"]
    if token_type in _PygmentsToken.Name.Function:
        return palette["function"]
    if token_type in _PygmentsToken.Name.Constant:
        return palette["literal"]
    if token_type in _PygmentsToken.Name:
        return palette["identifier"]
    if (
        token_type in _PygmentsToken.Operator
        or token_type in _PygmentsToken.Punctuation
    ):
        return palette["operator"]
    return None


def _highlight_python(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError):
        return wrap_ansi(text, *palette["fallback"])

    parts: list[str] = []
    cursor = 0
    for token in tokens:
        token_type = token.type
        token_text = token.string
        start_col = token.start[1]
        end_col = token.end[1]
        if token_type in {
            tokenize.ENDMARKER,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
        }:
            continue
        if start_col > cursor:
            parts.append(text[cursor:start_col])
        parts.append(_style_python_token(token_type, token_text, palette))
        cursor = max(cursor, end_col)
    if cursor < len(text):
        parts.append(text[cursor:])
    return "".join(parts) if parts else text


def _style_python_token(
    token_type: int,
    token_text: str,
    palette: Mapping[str, AnsiStyle],
) -> str:
    if token_type == tokenize.COMMENT:
        return wrap_ansi(token_text, *palette["comment"])
    if token_type == tokenize.STRING:
        return wrap_ansi(token_text, *palette["string"])
    if token_type == tokenize.NUMBER:
        return wrap_ansi(token_text, *palette["number"])
    if token_type == tokenize.OP:
        return wrap_ansi(token_text, *palette["operator"])
    if token_type == tokenize.NAME:
        if keyword.iskeyword(token_text):
            return wrap_ansi(token_text, *palette["keyword"])
        if token_text in {"True", "False", "None"}:
            return wrap_ansi(token_text, *palette["literal"])
        return wrap_ansi(token_text, *palette["identifier"])
    return token_text


def _highlight_shell(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text

    stripped = text.lstrip()
    if stripped.startswith("#"):
        return wrap_ansi(text, *palette["comment"])

    patterns = [
        (r"(\"[^\"]*\"|'[^']*')", palette["string"]),
        (r"(?<!\\)(\$[A-Za-z_][A-Za-z0-9_]*)", palette["variable"]),
        (r"(?<!\S)(--?[A-Za-z0-9][A-Za-z0-9_-]*)", palette["flag"]),
        (r"(\|\||&&|[|<>])", palette["operator"]),
    ]
    return _apply_regex_styles(text, patterns, first_word_style=palette["command"])


def _highlight_json(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    try:
        json.loads(text)
    except Exception:
        pass

    patterns = [
        (r'(\"(?:\\.|[^\"])*\")(\s*:)', (palette["identifier"], None)),
        (r'(?<!: )\"(?:\\.|[^\"])*\"', palette["string"]),
        (r"\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", palette["number"]),
        (r"\b(?:true|false|null)\b", palette["literal"]),
        (r"[{}\[\],:]", palette["operator"]),
    ]
    return _apply_json_styles(text, patterns)


def _highlight_c_like(
    text: str,
    palette: Mapping[str, AnsiStyle],
    keywords: frozenset[str],
    literals: frozenset[str],
) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(
        spans,
        text,
        r"(//[^\n]*|/\*.*?\*/)",
        palette["comment"],
        flags=re.DOTALL,
    )
    _append_pattern_spans(
        spans,
        text,
        r'(\"(?:\\.|[^\"])*\"|\'(?:\\.|[^\'])*\'|`(?:\\.|[^`])*`)',
        palette["string"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b",
        palette["number"],
    )
    _append_keyword_spans(spans, text, keywords, palette["keyword"])
    _append_keyword_spans(spans, text, literals, palette["literal"])
    _append_pattern_spans(
        spans,
        text,
        r"(\|\||&&|=>|==|!=|<=|>=|[{}()[\];,.<>:+/*=\\-])",
        palette["operator"],
    )
    return _render_styled_spans(text, spans)


def _highlight_yaml(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(^\s*#.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r"(?m)(^\s*[^:#\n][^:\n]*)(\s*:)",
        (palette["identifier"], None),
    )
    _append_pattern_spans(spans, text, r'(\"(?:\\.|[^\"])*\"|\'(?:\\.|[^\'])*\')', palette["string"])
    _append_pattern_spans(
        spans,
        text,
        r"\b(?:true|false|null|yes|no|on|off)\b",
        palette["literal"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b",
        palette["number"],
    )
    _append_pattern_spans(spans, text, r"(?m)(^\s*-\s)", palette["operator"])
    return _render_styled_spans(text, spans)


def _highlight_diff(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    lines = text.splitlines(keepends=True)
    rendered: list[str] = []
    for line in lines or [text]:
        if line.startswith("+++ ") or line.startswith("--- "):
            rendered.append(wrap_ansi(line, *palette["identifier"]))
        elif line.startswith("+"):
            rendered.append(wrap_ansi(line, *palette["string"]))
        elif line.startswith("-"):
            rendered.append(wrap_ansi(line, *palette["keyword"]))
        elif line.startswith("@@") or line.startswith("diff ") or line.startswith("index "):
            rendered.append(wrap_ansi(line, *palette["comment"]))
        else:
            rendered.append(line)
    return "".join(rendered)


def _highlight_markup(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(<!--.*?-->)", palette["comment"], flags=re.DOTALL)
    _append_pattern_spans(
        spans,
        text,
        r"(<\/?[A-Za-z_:][A-Za-z0-9:._-]*)",
        palette["keyword"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"([A-Za-z_:][A-Za-z0-9:._-]*)(=)",
        (palette["identifier"], palette["operator"]),
    )
    _append_pattern_spans(
        spans,
        text,
        r'(\"(?:\\.|[^\"])*\"|\'(?:\\.|[^\'])*\')',
        palette["string"],
    )
    _append_pattern_spans(spans, text, r"(\/?>)", palette["operator"])
    return _render_styled_spans(text, spans)


def _highlight_sql(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(--[^\n]*|/\*.*?\*/)", palette["comment"], flags=re.DOTALL)
    _append_pattern_spans(
        spans,
        text,
        r'(\"(?:\\.|[^\"])*\"|\'(?:\\.|[^\'])*\'|`[^`]*`)',
        palette["string"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b-?\d+(?:\.\d+)?\b",
        palette["number"],
    )
    _append_keyword_spans(
        spans,
        text,
        frozenset(word.upper() for word in _SQL_KEYWORDS) | _SQL_KEYWORDS,
        palette["keyword"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"(,|\(|\)|;|=|<>|!=|<=|>=|<|>)",
        palette["operator"],
    )
    return _render_styled_spans(text, spans)


def _highlight_toml(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(^\s*#.*$)", palette["comment"])
    _append_pattern_spans(spans, text, r"(?m)(^\s*\[[^\]\n]+\])", palette["heading"])
    _append_pattern_spans(
        spans,
        text,
        r"(?m)(^\s*[A-Za-z0-9_.-]+)(\s*=)",
        (palette["identifier"], palette["operator"]),
    )
    _append_pattern_spans(
        spans,
        text,
        r'(\"(?:\\.|[^\"])*\")',
        palette["string"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b(?:true|false)\b",
        palette["literal"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b-?\d+(?:\.\d+)?\b",
        palette["number"],
    )
    return _render_styled_spans(text, spans)


def _highlight_css(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(/\*.*?\*/)", palette["comment"], flags=re.DOTALL)
    _append_pattern_spans(
        spans,
        text,
        r"(?m)(^[^{\n]+)(\s*\{)",
        (palette["keyword"], palette["operator"]),
    )
    _append_pattern_spans(
        spans,
        text,
        r"([A-Za-z-]+)(\s*:)",
        (palette["identifier"], palette["operator"]),
    )
    _append_pattern_spans(
        spans,
        text,
        r"(#[0-9A-Fa-f]{3,8})",
        palette["number"],
    )
    _append_pattern_spans(
        spans,
        text,
        r'(\"(?:\\.|[^\"])*\"|\'(?:\\.|[^\'])*\')',
        palette["string"],
    )
    return _render_styled_spans(text, spans)


def _highlight_ruby(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(#.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r'(\"(?:\\.|[^\"])*\"|\'(?:\\.|[^\'])*\')',
        palette["string"],
    )
    _append_pattern_spans(spans, text, r"(:[A-Za-z_][A-Za-z0-9_!?=]*)", palette["literal"])
    _append_pattern_spans(spans, text, r"\b-?\d+(?:\.\d+)?\b", palette["number"])
    _append_keyword_spans(spans, text, _RUBY_KEYWORDS, palette["keyword"])
    _append_pattern_spans(
        spans,
        text,
        r"(\|\||&&|=>|==|!=|<=|>=|[{}()[\];,.<>:+/*=\\-])",
        palette["operator"],
    )
    return _render_styled_spans(text, spans)


def _highlight_markdown(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(^#{1,6}\s+.*$)", palette["heading"])
    _append_pattern_spans(spans, text, r"(?m)(^\s*(?:[-*+]|\d+\.)\s+)", palette["label"])
    _append_pattern_spans(spans, text, r"(?m)(^>\s+.*$)", palette["quote"])
    _append_pattern_spans(spans, text, r"(`[^`]+`)", palette["string"])
    _append_pattern_spans(
        spans,
        text,
        r"(\[[^\]]+\])(\([^)]+\))",
        (palette["identifier"], palette["operator"]),
    )
    return _render_styled_spans(text, spans)


def _apply_regex_styles(
    text: str,
    patterns: list[tuple[str, AnsiStyle]],
    *,
    first_word_style: AnsiStyle | None = None,
) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    if first_word_style:
        command_match = re.match(r"^(\s*)(\S+)", text)
        if command_match:
            spans.append(
                (
                    command_match.start(2),
                    command_match.end(2),
                    first_word_style,
                )
            )
    for pattern, style in patterns:
        for match in re.finditer(pattern, text):
            spans.append(
                (
                    match.start(1) if match.lastindex else match.start(),
                    match.end(1) if match.lastindex else match.end(),
                    style,
                )
            )
    return _render_styled_spans(text, spans)


def _is_grouped_style(style: object) -> bool:
    return (
        isinstance(style, tuple)
        and len(style) == 2
        and all(item is None or isinstance(item, tuple) for item in style)
    )


def _apply_json_styles(
    text: str,
    patterns: list[tuple[str, AnsiStyle | GroupedAnsiStyle]],
) -> str:
    spans: list[tuple[int, int, AnsiStyle]] = []
    for pattern, style in patterns:
        for match in re.finditer(pattern, text):
            if _is_grouped_style(style):
                first_style, second_style = style
                if match.lastindex and match.lastindex >= 1 and first_style:
                    spans.append((match.start(1), match.end(1), first_style))
                if match.lastindex and match.lastindex >= 2 and second_style:
                    spans.append((match.start(2), match.end(2), second_style))
            else:
                spans.append((match.start(), match.end(), style))
    return _render_styled_spans(text, spans)


def _append_pattern_spans(
    spans: list[tuple[int, int, AnsiStyle]],
    text: str,
    pattern: str,
    style: AnsiStyle | GroupedAnsiStyle,
    *,
    flags: int = 0,
) -> None:
    for match in re.finditer(pattern, text, flags):
        if _is_grouped_style(style):
            first_style, second_style = style
            if match.lastindex and match.lastindex >= 1 and first_style:
                spans.append((match.start(1), match.end(1), first_style))
            if match.lastindex and match.lastindex >= 2 and second_style:
                spans.append((match.start(2), match.end(2), second_style))
            continue
        spans.append(
            (
                match.start(1) if match.lastindex else match.start(),
                match.end(1) if match.lastindex else match.end(),
                style,
            )
        )


def _append_keyword_spans(
    spans: list[tuple[int, int, AnsiStyle]],
    text: str,
    words: frozenset[str],
    style: AnsiStyle,
) -> None:
    if not words:
        return
    pattern = r"\b(?:{})\b".format("|".join(sorted(re.escape(word) for word in words)))
    _append_pattern_spans(spans, text, pattern, style)


def _render_styled_spans(
    text: str,
    spans: list[tuple[int, int, AnsiStyle]],
) -> str:
    if not spans:
        return text
    spans.sort(key=lambda item: (item[0], item[1]))
    filtered: list[tuple[int, int, AnsiStyle]] = []
    cursor = 0
    for start, end, style in spans:
        if start < cursor or end <= start:
            continue
        filtered.append((start, end, style))
        cursor = end

    parts: list[str] = []
    cursor = 0
    for start, end, style in filtered:
        if start > cursor:
            parts.append(text[cursor:start])
        parts.append(wrap_ansi(text[start:end], *style))
        cursor = end
    if cursor < len(text):
        parts.append(text[cursor:])
    return "".join(parts)


def _highlight_dockerfile(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(^\s*#.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r"(?m)(^FROM|^RUN|^COPY|^CMD|^ENV|^EXPOSE|^WORKDIR|^ADD|^ARG|^ENTRYPOINT|^VOLUME|^USER|^LABEL|^MAINTAINER|^ONBUILD|^STOPSIGNAL|^HEALTHCHECK|^SHELL)\b",
        palette["keyword"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"('[^']*'|\"[^\"]*\")",
        palette["string"],
    )
    return _render_styled_spans(text, spans)


def _highlight_lua(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(--.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r"\b(function|end|if|then|else|elseif|local|return|for|do|while|repeat|until|break|nil|true|false|and|or|not|in)\b",
        palette["keyword"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"('[^']*'|\"[^\"]*\")",
        palette["string"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b\d+\.?\d*\b",
        palette["number"],
    )
    return _render_styled_spans(text, spans)


def _highlight_r(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(#.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r"\b(function|if|else|for|while|return|library|require|ggplot|aes|geom_\w+|mutate|filter|select|arrange|summarise|group_by)\b",
        palette["keyword"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"('[^']*'|\"[^\"]*\")",
        palette["string"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b\d+\.?\d*\b",
        palette["number"],
    )
    return _render_styled_spans(text, spans)


def _highlight_perl(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(#.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r"\b(my|our|sub|use|require|if|else|elsif|foreach|while|for|unless|until|return|next|last|redo)\b",
        palette["keyword"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"(\$[A-Za-z_][A-Za-z0-9_]*)",
        palette["variable"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"(\@[A-Za-z_][A-Za-z0-9_]*)",
        palette["variable"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"(\%[A-Za-z_][A-Za-z0-9_]*)",
        palette["variable"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"('[^']*'|\"[^\"]*\")",
        palette["string"],
    )
    return _render_styled_spans(text, spans)


def _highlight_hcl(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(#.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r"\b(resource|variable|output|module|provider|data|terraform|locals|required_providers|backend)\b",
        palette["keyword"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"(\{|\})",
        palette["operator"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"('[^']*'|\"[^\"]*\")",
        palette["string"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b\d+\.?\d*\b",
        palette["number"],
    )
    return _render_styled_spans(text, spans)


def _highlight_ini(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(;.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r"(?m)^\[[^\]\n]+\]",
        palette["heading"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"(?m)(^[A-Za-z0-9_.-]+)(\s*=)",
        (palette["identifier"], palette["operator"]),
    )
    _append_pattern_spans(
        spans,
        text,
        r"('[^']*'|\"[^\"]*\")",
        palette["string"],
    )
    return _render_styled_spans(text, spans)


def _highlight_makefile(text: str, palette: Mapping[str, AnsiStyle]) -> str:
    if not text:
        return text
    spans: list[tuple[int, int, AnsiStyle]] = []
    _append_pattern_spans(spans, text, r"(?m)(#.*$)", palette["comment"])
    _append_pattern_spans(
        spans,
        text,
        r"(?m)(^[A-Za-z0-9_.-]+)(\s*:)",
        (palette["keyword"], palette["operator"]),
    )
    _append_pattern_spans(
        spans,
        text,
        r"\$\([A-Za-z_][A-Za-z0-9_]*\)",
        palette["variable"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\$\{[A-Za-z_][A-Za-z0-9_]*\}",
        palette["variable"],
    )
    _append_pattern_spans(
        spans,
        text,
        r"\b(\.PHONY|\.DEFAULT|include|ifeq|else|endif)\b",
        palette["keyword"],
    )
    return _render_styled_spans(text, spans)


__all__ = [
    "ANSI_BLUE",
    "ANSI_BOLD",
    "ANSI_BRIGHT_BLACK",
    "ANSI_BLACK",
    "ANSI_CYAN",
    "ANSI_DIM",
    "ANSI_GREEN",
    "ANSI_MAGENTA",
    "ANSI_RED",
    "ANSI_RESET",
    "ANSI_YELLOW",
    "get_highlight_palette",
    "highlight_code_fragment",
    "normalize_highlight_theme",
    "wrap_ansi",
]
