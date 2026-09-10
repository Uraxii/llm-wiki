"""`python3 -m llmwiki` entry point."""

import sys

from llmwiki.cli import main

sys.exit(main(sys.argv[1:]))
