import asyncio
from pathlib import Path
import tempfile
import unittest

from config.config import Config
from context.symbols import extract_symbols, language_for, read_source
from tools.base import ToolInvocation
from tools.builtin.repo_map import RepoMapTool


def symbol_names(path: Path, source: str) -> list[str]:
    return [symbol.name for symbol in extract_symbols(path, source)]


class LanguageDetectionTests(unittest.TestCase):
    def test_known_suffixes(self):
        self.assertEqual(language_for(Path("a.py")), "python")
        self.assertEqual(language_for(Path("a.TSX")), "javascript")
        self.assertEqual(language_for(Path("a.go")), "go")
        self.assertEqual(language_for(Path("a.rs")), "rust")

    def test_unknown_suffix(self):
        self.assertIsNone(language_for(Path("a.txt")))
        self.assertIsNone(language_for(Path("Makefile")))


class PythonExtractionTests(unittest.TestCase):
    SOURCE = '''
MAX_SIZE = 10
lowercase = 1

class Widget(Base):
    def render(self) -> str:
        return ""

    async def load(self, path):
        pass

def build(a, b=2, *args, **kwargs) -> Widget:
    pass
'''

    def test_extracts_classes_functions_methods_and_constants(self):
        names = symbol_names(Path("w.py"), self.SOURCE)

        self.assertIn("Widget", names)
        self.assertIn("render", names)
        self.assertIn("load", names)
        self.assertIn("build", names)
        self.assertIn("MAX_SIZE", names)

    def test_lowercase_module_assignment_is_not_a_constant(self):
        self.assertNotIn("lowercase", symbol_names(Path("w.py"), self.SOURCE))

    def test_methods_are_qualified_by_class(self):
        symbols = {s.name: s for s in extract_symbols(Path("w.py"), self.SOURCE)}

        self.assertEqual(symbols["render"].parent, "Widget")
        self.assertEqual(symbols["render"].kind, "method")
        self.assertIsNone(symbols["build"].parent)
        self.assertEqual(symbols["build"].kind, "function")

    def test_signature_includes_arguments_and_return(self):
        symbols = {s.name: s for s in extract_symbols(Path("w.py"), self.SOURCE)}

        self.assertIn("a, b=2, *args, **kwargs", symbols["build"].signature)
        self.assertIn("-> Widget", symbols["build"].signature)
        self.assertIn("class Widget(Base)", symbols["Widget"].signature)

    def test_syntax_error_returns_empty_rather_than_raising(self):
        self.assertEqual(extract_symbols(Path("bad.py"), "def ("), [])

    def test_render_qualifies_methods(self):
        symbols = {s.name: s for s in extract_symbols(Path("w.py"), self.SOURCE)}

        self.assertIn("Widget.render", symbols["render"].render())
        self.assertIn("L6", symbols["render"].render())


class PatternExtractionTests(unittest.TestCase):
    def test_javascript(self):
        source = (
            "export class Store {}\n"
            "async function load(a) {}\n"
            "export const make = (x) => x\n"
            "export interface Shape {}\n"
        )
        names = symbol_names(Path("a.ts"), source)

        self.assertEqual(names, ["Store", "load", "make", "Shape"])

    def test_go(self):
        source = "type Server struct {}\nfunc (s *Server) Start() {}\nfunc New() {}\n"
        names = symbol_names(Path("a.go"), source)

        self.assertIn("Server", names)
        self.assertIn("Start", names)
        self.assertIn("New", names)

    def test_rust(self):
        source = "pub struct Config {}\npub async fn run() {}\nimpl Config {}\n"
        names = symbol_names(Path("a.rs"), source)

        self.assertIn("Config", names)
        self.assertIn("run", names)

    def test_line_numbers_are_one_based(self):
        symbols = extract_symbols(Path("a.go"), "\n\nfunc New() {}\n")

        self.assertEqual(symbols[0].line, 3)


class RepoMapToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, relative: str, content: str = "") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_tool(self, **params):
        tool = RepoMapTool(Config())
        return asyncio.run(
            tool.execute(ToolInvocation(params=params, cwd=self.root))
        )

    def test_maps_sources_with_symbols(self):
        self.write("src/app.py", "class App:\n    def run(self):\n        pass\n")
        result = self.run_tool()

        self.assertTrue(result.success)
        self.assertIn("src/app.py", result.output)
        self.assertIn("class App", result.output)
        self.assertIn("App.run", result.output)

    def test_excludes_vendored_and_gitignored_paths(self):
        self.write(".gitignore", "secret.py\n")
        self.write("src/app.py", "def keep(): pass\n")
        self.write("node_modules/pkg/index.js", "function vendored() {}\n")
        self.write(".venv/lib/dep.py", "def vendored(): pass\n")
        self.write("secret.py", "def ignored(): pass\n")

        result = self.run_tool()

        self.assertIn("keep", result.output)
        self.assertNotIn("node_modules", result.output)
        self.assertNotIn(".venv", result.output)
        self.assertNotIn("secret.py", result.output)

    def test_non_source_files_are_skipped(self):
        self.write("README.md", "# hi")
        self.write("data.json", "{}")
        result = self.run_tool()

        self.assertIn("No recognised source files", result.output)
        self.assertEqual(result.metadata["files"], 0)

    def test_symbols_false_gives_structure_only(self):
        self.write("src/app.py", "class App:\n    pass\n")
        result = self.run_tool(symbols=False)

        self.assertIn("src/app.py", result.output)
        self.assertNotIn("class App", result.output)

    def test_max_files_reports_what_was_dropped(self):
        for index in range(5):
            self.write(f"mod{index}.py", "def f(): pass\n")

        result = self.run_tool(max_files=2)

        self.assertEqual(result.metadata["files"], 2)
        self.assertEqual(result.metadata["files_found"], 5)
        self.assertIn("most relevant of 5", result.output)

    def test_missing_directory_errors(self):
        result = self.run_tool(path="nope")

        self.assertFalse(result.success)
        self.assertIn("does not exist", result.error)

    def test_unparseable_source_still_lists_the_file(self):
        self.write("broken.py", "def (")
        result = self.run_tool()

        self.assertIn("broken.py", result.output)

    def test_read_source_handles_missing_file(self):
        self.assertIsNone(read_source(self.root / "gone.py"))


if __name__ == "__main__":
    unittest.main()
