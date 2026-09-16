"""Точка входа консольного exe (PyInstaller): timetable-upload."""
import sys

from timetable_upload.cli import main

if __name__ == "__main__":
    sys.exit(main())
