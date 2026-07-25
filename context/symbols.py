from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re

MAX_SOURCE_BYTES = 2_000_000


@dataclass
class Symbol:
    kind: str
    name: str
    line: int
    signature: str
    parent: str | None = None

    def render(self) -> str:
        prefix = f"{self.parent}." if self.parent else ""
        return f"{self.signature.replace(self.name, prefix + self.name, 1)} (L{self.line})"


def _python_signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(base) for base in node.bases]
        suffix = f"({', '.join(bases)})" if bases else ""
        return f"class {node.name}{suffix}"

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""

    return f"{prefix} {node.name}({args}){returns}"


def _extract_python(source: str) -> list[Symbol]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    symbols: list[Symbol] = []

    def visit(node: ast.AST, parent: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                symbols.append(
                    Symbol("class", child.name, child.lineno, _python_signature(child))
                )
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    Symbol(
                        "method" if parent else "function",
                        child.name,
                        child.lineno,
                        _python_signature(child),
                        parent,
                    )
                )
            elif parent is None and isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        symbols.append(
                            Symbol("const", target.id, child.lineno, target.id)
                        )

    visit(tree, None)
    return symbols


PATTERNS: dict[str, list[tuple[str, str]]] = {
    "javascript": [
        ("class", r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)"),
        ("function", r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"),
        ("function", r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"),
        ("type", r"^\s*(?:export\s+)?(?:interface|type|enum)\s+(\w+)"),
    ],
    "go": [
        ("function", r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"),
        ("type", r"^\s*type\s+(\w+)"),
    ],
    "rust": [
        ("function", r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"),
        ("type", r"^\s*(?:pub\s+)?(?:struct|enum|trait|union)\s+(\w+)"),
        ("impl", r"^\s*impl(?:<[^>]*>)?\s+(?:\w+\s+for\s+)?(\w+)"),
    ],
    "java": [
        ("class", r"^\s*(?:public\s+|private\s+|protected\s+)?(?:final\s+|abstract\s+)?(?:class|interface|enum|record)\s+(\w+)"),
        ("method", r"^\s+(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\("),
    ],
    "ruby": [
        ("class", r"^\s*(?:class|module)\s+(\w+)"),
        ("method", r"^\s*def\s+(?:self\.)?(\w+)"),
    ],
    "php": [
        ("class", r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+(\w+)"),
        ("function", r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)"),
    ],
    "c": [
        ("type", r"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+(\w+)"),
        ("function", r"^[\w*]+(?:\s+[\w*]+)*\s+\*?(\w+)\s*\([^;]*\)\s*\{"),
    ],
}

SUFFIX_LANGUAGE = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "c",
    ".cpp": "c",
    ".hpp": "c",
}


def language_for(path: Path) -> str | None:
    return SUFFIX_LANGUAGE.get(path.suffix.lower())


def _extract_with_patterns(source: str, language: str) -> list[Symbol]:
    patterns = [(kind, re.compile(pattern)) for kind, pattern in PATTERNS[language]]
    symbols: list[Symbol] = []
    seen: set[tuple[str, int]] = set()

    for number, line in enumerate(source.splitlines(), start=1):
        if len(line) > 500:
            continue

        for kind, pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue

            name = match.group(1)
            if (name, number) in seen:
                continue

            seen.add((name, number))
            symbols.append(Symbol(kind, name, number, line.strip().rstrip("{").strip()))
            break

    return symbols


def extract_symbols(path: Path, source: str) -> list[Symbol]:
    language = language_for(path)

    if language is None or len(source) > MAX_SOURCE_BYTES:
        return []

    if language == "python":
        return _extract_python(source)

    return _extract_with_patterns(source, language)


def read_source(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return None
        return path.read_text("utf-8", errors="replace")
    except OSError:
        return None
