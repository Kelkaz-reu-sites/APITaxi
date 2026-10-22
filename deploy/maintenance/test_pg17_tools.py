"""Offline parser checks for the disposable image functional smoke."""

import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "pg17_tools", Path(__file__).with_name("check_pg17_tools.py")
)
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)


class Pg17ToolsTests(unittest.TestCase):
    def test_real_metric_sample_is_required_not_a_help_comment(self):
        TOOLS.validate_metrics(
            b'pgbackrest_exporter_info{version="test"} 1\n', "pgbackrest_exporter_info"
        )
        for body in (
            b"",
            b"# HELP pgbackrest_exporter_info version\n",
            b'other{version="test"} 1\n',
            b"x" * (TOOLS.LIMIT + 1),
        ):
            with self.assertRaises(ValueError):
                TOOLS.validate_metrics(body, "pgbackrest_exporter_info")


if __name__ == "__main__":
    unittest.main()
