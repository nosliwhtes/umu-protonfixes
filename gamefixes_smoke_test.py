"""Zero-dependency check that every gamefix parses and defines a stage."""

import ast
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STORE_DIRS = sorted(ROOT.glob('gamefixes-*'))
STAGES = {'main', 'early', 'main_with_id', 'early_with_id'}


def gamefix_files():
    """Return gamefix modules, skipping package markers."""
    files = []
    for store in STORE_DIRS:
        files.extend(
            path
            for path in sorted(store.glob('*.py'))
            if path.name != '__init__.py'
        )
    return files


class GamefixSmokeTest(unittest.TestCase):
    """Parse gamefixes without importing protonfixes or its dependencies."""

    def test_every_gamefix_parses_and_defines_a_stage(self) -> None:
        """Each gamefix is LF, parses, and defines main or early."""
        missing = []
        for path in gamefix_files():
            data = path.read_bytes()
            self.assertNotIn(
                b'\r\n',
                data,
                f'{path.relative_to(ROOT)} has CRLF line endings',
            )
            tree = ast.parse(data.decode('utf-8'), filename=str(path))
            names = {
                node.name
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
            }
            if not names & STAGES:
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [])


if __name__ == '__main__':
    result = unittest.main(exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
