"""Общие элементы графического интерфейса: фоновые задачи, вывод журнала, редакторы ячеек."""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

EMPTY_LABEL = "— не задано —"

# цвета состояния строк (светлые, с чёрным текстом — одинаково читаются в светлой и тёмной теме)
COLOR_OK = QColor("#dff0d8")
COLOR_WARN = QColor("#fcf8e3")
COLOR_BAD = QColor("#f2dede")
COLOR_OFF = QColor("#e8e8e8")


# ---------------------------------------------------------------------- фоновые задачи
class Worker(QThread):
    """Выполняет функцию в отдельном потоке.  Функция получает ``progress(done, total)``
    и ``message(text)`` для обратной связи; результат приходит в сигнал ``done``.
    ``cancel`` — событие «остановись»: задача, умеющая останавливаться, опрашивает его между запросами."""
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)
    message = Signal(str)

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel = threading.Event()

    def run(self):
        try:
            result = self.fn(self.progress.emit, self.message.emit)
        except Exception as e:  # любая ошибка задачи показывается пользователю
            logging.getLogger(__name__).debug("ошибка задачи:\n%s", traceback.format_exc())
            self.failed.emit(str(e))
            return
        self.done.emit(result)


class LogHandler(logging.Handler):
    """Обработчик logging, передающий строки в интерфейс через сигнал (потокобезопасно)."""

    class _Emitter(QObject):
        text = Signal(str)

    def __init__(self):
        super().__init__()
        self.emitter = self._Emitter()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.emitter.text.emit(self.format(record))
        except RuntimeError:
            pass


# ---------------------------------------------------------------------- компоновка
def scrollable(body: QWidget) -> QScrollArea:
    """Обернуть виджет в область прокрутки: на невысоком экране появляется полоса прокрутки,
    а не сжатые до нечитаемости поля (минимальная высота формы больше не ограничивает окно)."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidget(body)
    return scroll


# ---------------------------------------------------------------------- таблицы
class Table(QTableWidget):
    """Таблица с переносом текста в ячейках.

    :meth:`fit_columns` подгоняет столбцы под содержимое и ширину окна: столбец не шире MAX_COL; если
    столбцы не помещаются, широкие ужимаются (не уже заголовка и MIN_COL), а последний столбец
    (обычно «Состояние») получает остаток ширины, но не меньше STRETCH_MIN — длинные записи
    переносятся на несколько строк, а не обрезаются и не уезжают за край.  Высота строк следует за
    перенесённым текстом, в том числе после перетаскивания границ столбцов и изменения размера окна.
    """
    MAX_COL = 380
    MIN_COL = 110
    STRETCH_MIN = 260

    def __init__(self, headers: list[str]):
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.setWordWrap(True)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setResizeContentsPrecision(200)     # ширину по содержимому оценивать по первым 200 строкам: в 7 раз быстрее
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setTextElideMode(Qt.ElideRight)
        hh.setStretchLastSection(True)
        self._stretch = len(headers) - 1
        self._max_col = self.MAX_COL
        self._fit_pending = False          # подгонка нужна, но таблица ещё не показана (ширина неизвестна)
        self._fit_scheduled = False
        # высота строк после изменения ширины столбцов — с задержкой: при перетаскивании границы
        # столбца сигналы идут непрерывно, а пересчёт полутора тысяч строк занимает заметное время
        self._rows_timer = QTimer(self)
        self._rows_timer.setSingleShot(True)
        self._rows_timer.setInterval(150)
        self._rows_timer.timeout.connect(self.resizeRowsToContents)
        hh.sectionResized.connect(lambda *_: self._rows_timer.start())

    def fit_columns(self, max_width: int | None = None) -> None:
        """Подогнать столбцы и высоту строк (после текущих событий; для скрытой таблицы — при показе)."""
        self._max_col = max_width or self.MAX_COL
        self._fit_pending = True
        self._schedule_fit()

    def showEvent(self, event):
        super().showEvent(event)
        if self._fit_pending:
            self._schedule_fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_pending:
            self._schedule_fit()

    def _schedule_fit(self) -> None:
        # через очередь событий: при показе вкладки showEvent приходит раньше окончательного размера
        if not self._fit_scheduled:
            self._fit_scheduled = True
            QTimer.singleShot(0, self._fit_now)

    def _fit_now(self) -> None:
        self._fit_scheduled = False
        if not self._fit_pending or not self.isVisible():
            return
        self._fit_pending = False
        self.resizeColumnsToContents()
        n, hh = self.columnCount(), self.horizontalHeader()
        fm = hh.fontMetrics()
        widths = [min(self.columnWidth(c), self._max_col) for c in range(n)]
        floors = [max(self.MIN_COL, min(fm.horizontalAdvance(self.horizontalHeaderItem(c).text()) + 16, 180))
                  for c in range(n)]
        others = [c for c in range(n) if c != self._stretch]
        avail = self.viewport().width() - self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        overflow = sum(widths[c] for c in others) + min(widths[self._stretch], self.STRETCH_MIN) - avail
        shrinkable = [c for c in others if widths[c] > floors[c]]
        excess = sum(widths[c] - floors[c] for c in shrinkable)
        if overflow > 0 and excess > 0:
            k = min(1.0, overflow / excess)
            for c in shrinkable:
                widths[c] -= round((widths[c] - floors[c]) * k)
        # растягиваемому столбцу — остаток (Qt не даёт ему стать уже заданной здесь ширины)
        widths[self._stretch] = min(widths[self._stretch], max(avail - sum(widths[c] for c in others), self.STRETCH_MIN))
        for c, w in enumerate(widths):
            self.setColumnWidth(c, w)
        hh.resizeSections()                   # растянуть последний столбец сразу, не дожидаясь изменения размера окна
        self._rows_timer.stop()
        self.resizeRowsToContents()


def make_table(headers: list[str]) -> Table:
    return Table(headers)


def cell(text, value=None, editable: bool = False, color: QColor | None = None, tip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    if value is not None:
        it.setData(Qt.UserRole, value)
    flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
    if editable:
        flags |= Qt.ItemIsEditable
    it.setFlags(flags)
    if color is not None:
        it.setBackground(QBrush(color))
        it.setForeground(QBrush(QColor("black")))
    if tip:
        it.setToolTip(tip)
    return it


def check_cell(checked: bool, tip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem()
    it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
    it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
    if tip:
        it.setToolTip(tip)
    return it


def paint_row(table: QTableWidget, row: int, color: QColor | None) -> None:
    for c in range(table.columnCount()):
        it = table.item(row, c)
        if it is None:
            continue
        if color is None:
            it.setData(Qt.BackgroundRole, None)
            it.setData(Qt.ForegroundRole, None)
        else:
            it.setBackground(QBrush(color))
            it.setForeground(QBrush(QColor("black")))


def filter_rows(table: QTableWidget, text: str, only_problems: bool = False, problem_col: int | None = None) -> int:
    """Скрывает строки, не содержащие текст (в любой ячейке) либо не помеченные как проблемные.
    Проблемной считается строка с непустым текстом в столбце problem_col.  Возвращает число видимых строк."""
    needle = text.strip().lower()
    shown = 0
    for r in range(table.rowCount()):
        hide = False
        if needle:
            hide = not any((table.item(r, c) is not None and needle in table.item(r, c).text().lower())
                           for c in range(table.columnCount()))
        if not hide and only_problems and problem_col is not None:
            it = table.item(r, problem_col)
            hide = it is None or not it.text().strip()
        table.setRowHidden(r, hide)
        shown += not hide
    return shown


class WrapDelegate(QStyledItemDelegate):
    """Переносит длинный текст ячейки дерева по словам, увеличивая высоту строки.

    Сам QTreeView с ``setWordWrap(True)`` текст рисует с переносом, но высоту строки не меняет — она
    считается без учёта ширины колонки.  Делегат берёт ширину у дерева; при изменении ширины колонок
    дерево просят пересчитать раскладку.
    """

    def __init__(self, tree: QTreeWidget):
        super().__init__(tree)
        self.tree = tree
        tree.setWordWrap(True)
        tree.header().sectionResized.connect(lambda *_: tree.scheduleDelayedItemsLayout())

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        text = index.data(Qt.DisplayRole)
        width = self.tree.columnWidth(index.column())
        if not text or width <= 0:
            return hint
        rect = option.fontMetrics.boundingRect(QRect(0, 0, max(width - 8, 40), 0), Qt.TextWordWrap, str(text))
        return QSize(hint.width(), max(hint.height(), rect.height() + 4))


class ChoiceDelegate(QStyledItemDelegate):
    """Редактор ячейки — выпадающий список с поиском по подстроке.

    ``choices_for(row)`` возвращает список ``(подпись, значение)``; выбранное значение
    кладётся в Qt.UserRole ячейки, подпись — в текст.  Пустой текст = «не задано» (None).
    """

    def __init__(self, choices_for: Callable[[int], list[tuple[str, object]]], parent=None):
        super().__init__(parent)
        self.choices_for = choices_for

    def createEditor(self, parent, option, index):
        cb = QComboBox(parent)
        cb.setEditable(True)
        cb.setInsertPolicy(QComboBox.NoInsert)
        cb.addItem(EMPTY_LABEL, None)
        for label, value in self.choices_for(index.row()):
            cb.addItem(label, value)
        comp = QCompleter(cb.model(), cb)
        comp.setCompletionColumn(0)
        comp.setFilterMode(Qt.MatchContains)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        cb.setCompleter(comp)
        return cb

    def setEditorData(self, cb, index):
        value = index.data(Qt.UserRole)
        i = 0
        if value is not None:
            for k in range(1, cb.count()):
                if cb.itemData(k) == value:
                    i = k
                    break
        cb.setCurrentIndex(i)
        cb.lineEdit().selectAll()

    def setModelData(self, cb, model, index):
        text, j = cb.currentText(), cb.currentIndex()
        if j < 0 or cb.itemText(j) != text:        # текст набран, а не выбран из списка — ищем его среди пунктов
            text = text.strip()
            j = 0 if not text or text == EMPTY_LABEL else cb.findText(text, Qt.MatchFixedString)
            if j < 0:       # текста нет в списке — оставляем прежнее значение
                return
        value = cb.itemData(j)
        if value == index.data(Qt.UserRole):     # выбрано то же самое — не трогаем модель (и не помечаем правку)
            return
        # обе роли одним вызовом: itemChanged придёт один раз, а не на каждую роль
        model.setItemData(index, {Qt.UserRole: value, Qt.EditRole: "" if j == 0 else cb.itemText(j)})


class CheckListDialog(QDialog):
    """Выбор нескольких значений из списка с фильтром по подстроке."""

    def __init__(self, title: str, options: list[tuple[str, object]], checked: list, parent=None, hint: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 560)
        lay = QVBoxLayout(self)
        if hint:
            lab = QLabel(hint)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("поиск…")
        self.filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter)
        self.list = QListWidget()
        checked_set = set(checked)
        # отмеченные — сверху
        for label, value in sorted(options, key=lambda o: (o[1] not in checked_set, o[0].lower())):
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, value)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if value in checked_set else Qt.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        clear = QPushButton("Снять все")
        clear.clicked.connect(self._clear)
        row.addWidget(clear)
        row.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)

    def _apply_filter(self, text: str):
        needle = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(needle) and needle not in it.text().lower() and it.checkState() != Qt.Checked)

    def _clear(self):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.Unchecked)

    def values(self) -> list:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


def ask(parent: QWidget, title: str, text: str, danger: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.setDefaultButton(QMessageBox.No)
    box.button(QMessageBox.Yes).setText("Да, выполнить")
    box.button(QMessageBox.No).setText("Отмена")
    return box.exec() == QMessageBox.Yes


def error(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def info(parent: QWidget, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)
