
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STRATEGY = ROOT / "strategy.py"


class TestStrategy(unittest.TestCase):

    def test_strategy_file_exists(self):
        self.assertTrue(STRATEGY.exists())

    def test_strategy_syntax(self):
        source = STRATEGY.read_text(encoding="utf-8")
        ast.parse(source)

    def test_strategy_not_empty(self):
        source = STRATEGY.read_text(encoding="utf-8")
        self.assertGreater(len(source.strip()), 100)


if __name__ == "__main__":
    unittest.main()
