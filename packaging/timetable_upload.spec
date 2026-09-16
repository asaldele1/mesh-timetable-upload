# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: два самостоятельных исполняемых файла (onefile).

    pyinstaller packaging/timetable_upload.spec

  timetable-upload      — командная строка (консольное приложение)
  timetable-upload-gui  — графический интерфейс (без консоли; PySide6 с QtWebEngine — окно входа mos.ru)
"""
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
DATAS = [(os.path.join(ROOT, "timetable_upload", "defaults.json"), "timetable_upload")]
# тяжёлые модули Qt, которые интерфейсу не нужны. QtWebEngineCore/Widgets нужны (окно входа через mos.ru),
# а с ними — QtWebChannel, QtPrintSupport, QtNetwork, QtPositioning, QtQml/QtQuick (зависимости WebEngine)
QT_EXCLUDES = [
    "PySide6.QtWebEngineQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtLocation",
    "PySide6.QtRemoteObjects", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtSvgWidgets", "PySide6.QtNetworkAuth", "PySide6.QtWebSockets",
    "PySide6.QtHttpServer", "PySide6.QtGraphs", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech", "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtUiTools",
]
COMMON_EXCLUDES = ["tkinter", "unittest", "pydoc", "pytest"]

cli = Analysis(
    [os.path.join(SPECPATH, "cli_entry.py")],
    pathex=[ROOT],
    datas=DATAS,
    excludes=COMMON_EXCLUDES + ["PySide6", "shiboken6"],
)
gui = Analysis(
    [os.path.join(SPECPATH, "gui_entry.py")],
    pathex=[ROOT],
    datas=DATAS,
    excludes=COMMON_EXCLUDES + QT_EXCLUDES,
)

EXE(
    PYZ(cli.pure), cli.scripts, cli.binaries, cli.datas,
    name="timetable-upload", console=True, upx=False, strip=False,
)
EXE(
    PYZ(gui.pure), gui.scripts, gui.binaries, gui.datas,
    name="timetable-upload-gui", console=False, upx=False, strip=False,
)
