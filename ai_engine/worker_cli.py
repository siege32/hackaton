"""Installed console entry point for the news worker."""

from __future__ import annotations

import sys

from .__main__ import main as module_main


def main() -> int:
    return module_main(["worker", *sys.argv[1:]])
