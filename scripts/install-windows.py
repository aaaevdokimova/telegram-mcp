#!/usr/bin/env python3
"""Bootstrap the reviewed Windows installer without importing a user site."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from telegram_search_mcp.windows_install import main

if __name__ == '__main__':
    main()
