#!/usr/bin/env python3
"""Entry point: python run.py [--config FILE] gather | execute RUN_DIR | tasks | check | jira ..."""
import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is required.")

from agent_runner.cli import main  # noqa: E402

sys.exit(main())
