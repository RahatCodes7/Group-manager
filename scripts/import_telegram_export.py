"""
Command-line entry point for importing old Telegram Desktop exports.

Usage (from the project root, with DATABASE_URL set):
    python scripts/import_telegram_export.py result.json [--dry-run] [--chat-id ID]

The logic lives in message_log/importer.py.
"""

import os
import sys

# Make the project root importable when running this file directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from message_log.importer import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
