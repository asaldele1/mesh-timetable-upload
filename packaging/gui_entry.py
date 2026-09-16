"""Точка входа оконного exe (PyInstaller): timetable-upload-gui."""
import sys

from timetable_upload.gui import main

if __name__ == "__main__":
    sys.exit(main())
