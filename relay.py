#!/usr/bin/env python3
"""
claude-relay: A message relay server for Claude Code instances.

This is a thin entry point. The implementation lives in the agentic_chat package.
"""

import sys
from pathlib import Path

# Ensure the src directory is on the path for direct invocation (python relay.py)
sys.path.insert(0, str(Path(__file__).parent / "src"))

from agentic_chat.cli import main

if __name__ == "__main__":
    main()
