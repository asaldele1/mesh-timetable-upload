"""Общие элементы графического интерфейса: фоновые задачи, вывод журнала, редакторы ячеек."""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
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
class _FitColumns:
    """Подгонка столбцов под содержимое и ширину окна (общая для QTableWidget и QTableView).

    :meth:`fit_columns` подгоняет столбцы: столбец не шире MAX_COL; если столбцы не помещаются,
    широкие ужимаются (не уже заголовка и MIN_COL), а последний столбец (обычно «Состояние»)
    получает остаток ширины, но не меньше STRETCH_MIN — длинные записи переносятся на несколько
    строк, а не обрезаются и не уезжают за край.
    """
    MAX_COL = 380
    MIN_COL = 110
    STRETCH_MIN = 260

    def _init_fit(self) -> None:
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.setWordWrap(True)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setTextElideMode(Qt.ElideRight)
        hh.setStretchLastSection(True)
        # ширину столбца по содержимому оценивать по первым 200 строкам, а не по всем
        self.verticalHeader().setResizeContentsPrecision(200)
        hh.setResizeContentsPrecision(200)
        self._max_col = self.MAX_COL
        self._fit_pending = False          # подгонка нужна, но таблица ещё не показана (ширина неизвестна)
        self._fit_scheduled = False

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
        self.resizeColumnsToContents()
        n, hh, model = self.model().columnCount(), self.horizontalHeader(), self.model()
        stretch = n - 1
        fm = hh.fontMetrics()
        widths = [min(self.columnWidth(c), self._max_col) for c in range(n)]
        floors = [max(self.MIN_COL, min(fm.horizontalAdvance(str(model.headerData(c, Qt.Horizontal) or "")) + 16, 180))
                  for c in range(n)]
        others = [c for c in range(n) if c != stretch]
        avail = self.viewport().width() - self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        overflow = sum(widths[c] for c in others) + min(widths[stretch], self.STRETCH_MIN) - avail
        shrinkable = [c for c in others if widths[c] > floors[c]]
        excess = sum(widths[c] - floors[c] for c in shrinkable)
        if overflow > 0 and excess > 0:
            k = min(1.0, overflow / excess)
            for c in shrinkable:
                widths[c] -= round((widths[c] - floors[c]) * k)
        # растягиваемому столбцу — остаток (Qt не даёт ему стать уже заданной здесь ширины)
        widths[stretch] = min(widths[stretch], max(avail - sum(widths[c] for c in others), self.STRETCH_MIN))
        for c, w in enumerate(widths):
            self.setColumnWidth(c, w)
        hh.resizeSections()                   # растянуть последний столбец сразу, не дожидаясь изменения размера окна
        self._fit_pending = False
        self._fit_rows()

    def _fit_rows(self) -> None:
        raise NotImplementedError


class Table(_FitColumns, QTableWidget):
    """Небольшая таблица на QTableWidget (десятки строк) с переносом текста и подгонкой столбцов.
    Для больших таблиц — :class:`TableView` с :class:`RowModel`."""

    def __init__(self, headers: list[str]):
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels(headers)
        self._init_fit()
        self.horizontalHeader().sectionResized.connect(lambda *_: self.resizeRowsToContents())

    def _fit_rows(self) -> None:
        self.resizeRowsToContents()


def make_table(headers: list[str]) -> Table:
    return Table(headers)


class Cell:
    """Ячейка :class:`RowModel`: текст, значение (Qt.UserRole; в столбце с флажком — отмечено ли) и подсказка."""
    __slots__ = ("text", "value", "tip")

    def __init__(self, text="", value=None, tip: str = ""):
        self.text = "" if text is None else str(text)
        self.value = value
        self.tip = tip


_BLACK = QBrush(QColor("black"))
_brushes: dict[str, QBrush] = {}
_DISPLAY, _EDIT, _TOOLTIP, _USER = Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole, Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.UserRole
_BACKGROUND, _FOREGROUND, _CHECK = Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.CheckStateRole
_CHECKED, _UNCHECKED = Qt.CheckState.Checked, Qt.CheckState.Unchecked
KEEP_COLOR = object()               # put(): цвет строки не менять


def _same_color(a: QColor | None, b: QColor | None) -> bool:
    return a is b or (a is not None and b is not None and a == b)
_FLAGS_RO = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
_FLAGS_EDIT = _FLAGS_RO | Qt.ItemFlag.ItemIsEditable
_EDITABLE, _CHECKABLE = Qt.ItemFlag.ItemIsEditable, Qt.ItemFlag.ItemIsUserCheckable


def _brush(color: QColor) -> QBrush:
    key = color.name()
    b = _brushes.get(key)
    if b is None:
        b = _brushes[key] = QBrush(color)
    return b


class RowModel(QAbstractTableModel):
    """Табличная модель поверх списков :class:`Cell`: данные лежат в Python, а виджет запрашивает
    только то, что видно на экране, — таблица на тысячи строк строится мгновенно (у QTableWidget на
    каждую ячейку заводится свой объект).

    Столбцы ``editable_cols`` редактируются (обычно через ChoiceDelegate), ``check_cols`` показывают
    флажок (значение ячейки — bool).  ``keys`` — ключ строки (имя сущности, занятие).  Правки
    пользователя (редактор, флажок) приходят сигналом ``edited``; программные изменения
    (:meth:`put`, :meth:`set_rows`) его не вызывают.  Массовое заполнение —
    внутри ``with model.bulk():`` — одна перезагрузка представления вместо тысяч уведомлений.
    """
    edited = Signal(int, int)

    def __init__(self, headers: list[str], editable_cols=(), check_cols=(), parent=None):
        super().__init__(parent)
        self.headers = list(headers)
        self.editable_cols = frozenset(editable_cols)
        self.check_cols = frozenset(check_cols)
        self.rows: list[list[Cell]] = []
        self.keys: list = []
        self.colors: list[QColor | None] = []
        self._bulk = False

    # --- QAbstractTableModel
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.headers):
            return self.headers[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        c = index.column()
        f = _FLAGS_RO
        if c in self.editable_cols:
            f |= _EDITABLE
        if c in self.check_cols:
            f |= _CHECKABLE
        return f

    def data(self, index, role=_DISPLAY):
        # представление зовёт data() десятки тысяч раз; сокращённые имена вида Qt.DisplayRole ищутся
        # по всем перечислениям Qt (~7 мкс каждое) — поэтому роли сравниваются с константами модуля
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if role == _DISPLAY or role == _EDIT:
            return None if c in self.check_cols else self.rows[r][c].text
        if role == _BACKGROUND:
            color = self.colors[r]
            return _brush(color) if color is not None else None
        if role == _FOREGROUND:
            return _BLACK if self.colors[r] is not None else None
        if role == _CHECK:
            if c in self.check_cols:
                return _CHECKED if self.rows[r][c].value else _UNCHECKED
            return None
        if role == _TOOLTIP:
            return self.rows[r][c].tip or None
        if role == _USER:
            return self.rows[r][c].value
        return None

    def setData(self, index, value, role=_EDIT):
        if not index.isValid() or not self._apply(index.row(), index.column(), role, value):
            return False
        self.dataChanged.emit(index, index)
        self.edited.emit(index.row(), index.column())
        return True

    def setItemData(self, index, roles):
        # ChoiceDelegate пишет значение и текст одним вызовом — одно уведомление, а не по одному на роль
        if not index.isValid():
            return False
        ok = False
        for role, value in roles.items():
            ok |= self._apply(index.row(), index.column(), role, value)
        if ok:
            self.dataChanged.emit(index, index)
            self.edited.emit(index.row(), index.column())
        return ok

    def _apply(self, r: int, c: int, role, value) -> bool:
        cell = self.rows[r][c]
        role = int(role)
        if role == _CHECK and c in self.check_cols:
            cell.value = value == _CHECKED if isinstance(value, Qt.CheckState) else int(value) == _CHECKED.value
        elif role == _EDIT or role == _DISPLAY:
            cell.text = "" if value is None else str(value)
        elif role == _USER:
            cell.value = value
        else:
            return False
        return True

    # --- программный доступ
    def bulk(self):
        """Контекст массового изменения: представление перезагружается один раз по выходу."""
        return _Bulk(self)

    def set_rows(self, rows: list[list[Cell]], keys: list | None = None) -> None:
        if not self._bulk:
            self.beginResetModel()
        self.rows = rows
        self.keys = list(keys) if keys is not None else [None] * len(rows)
        self.colors = [None] * len(rows)
        if not self._bulk:
            self.endResetModel()

    def cell(self, row: int, col: int) -> Cell:
        return self.rows[row][col]

    def text(self, row: int, col: int) -> str:
        return self.rows[row][col].text

    def value(self, row: int, col: int):
        return self.rows[row][col].value

    def key(self, row: int):
        return self.keys[row]

    def put(self, row: int, col: int, text, value=None, tip: str = "", color=KEEP_COLOR) -> bool:
        """Записать текст, значение и подсказку ячейки (и, если задан ``color``, цвет строки: None —
        обычный).  True, если что-то изменилось; представление уведомляется один раз."""
        cell = self.rows[row][col]
        text = "" if text is None else str(text)
        changed = not (cell.text == text and cell.value == value and cell.tip == tip)
        cell.text, cell.value, cell.tip = text, value, tip
        if color is not KEEP_COLOR and not _same_color(self.colors[row], color):
            self.colors[row] = color
            self._changed(row, 0, len(self.headers) - 1)
            return True
        if changed:
            self._changed(row, col, col)
        return changed

    def notify_rows(self, first: int, last: int) -> None:
        """Сообщить представлению об изменении строк ``first..last`` (после серии тихих правок)."""
        if not self._bulk and last >= first:
            self.dataChanged.emit(self.index(first, 0), self.index(last, len(self.headers) - 1))

    def _changed(self, row: int, c0: int, c1: int) -> None:
        if not self._bulk:
            self.dataChanged.emit(self.index(row, c0), self.index(row, c1))


class _Bulk:
    def __init__(self, model: RowModel):
        self.model = model

    def __enter__(self):
        self.model.beginResetModel()
        self.model._bulk = True

    def __exit__(self, *exc):
        self.model._bulk = False
        self.model.endResetModel()


class TableView(_FitColumns, QTableView):
    """Представление для :class:`RowModel`.  Высота строк (с переносом текста) считается лениво —
    только для строк в видимой области, при прокрутке и изменении ширины столбцов: пересчёт всех
    строк большой таблицы занимал бы секунды."""

    def __init__(self, model: RowModel):
        super().__init__()
        self.setModel(model)
        self._init_fit()
        # полоса прокрутки всегда: иначе её появление после измерения строк сужает область просмотра,
        # растягиваемый столбец меняет ширину, и высоты строк приходится мерить заново
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._sized: set[int] = set()          # строки, высота которых уже подогнана
        self._one_line = 0                     # высота однострочной ячейки (для строк, ещё не измеренных)
        self._rows_timer = QTimer(self)
        self._rows_timer.setSingleShot(True)
        self._rows_timer.setInterval(0)
        self._rows_timer.timeout.connect(self._size_visible_rows)
        self.horizontalHeader().sectionResized.connect(self._columns_changed)
        self.verticalScrollBar().valueChanged.connect(lambda *_: self._rows_timer.start())
        model.modelReset.connect(self._reset)
        model.dataChanged.connect(self._data_changed)
        model.rowsInserted.connect(self._reset)
        model.rowsRemoved.connect(self._reset)

    def _fit_rows(self) -> None:
        self._sized.clear()
        self._size_visible_rows()

    def _reset(self, *_) -> None:
        self._sized.clear()
        self._rows_timer.start()

    def _data_changed(self, tl, br, *_) -> None:
        self._sized.difference_update(range(tl.row(), br.row() + 1))
        self._rows_timer.start()

    def _columns_changed(self, _logical, old: int, new: int) -> None:
        # при перетаскивании границы столбца сигналы идут непрерывно — пересчёт с задержкой
        # (столбцы, выставленные самой подгонкой, учтёт _fit_rows)
        if old != new and not self._fit_pending:
            self._sized.clear()
            self._rows_timer.start(150)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rows_timer.start()

    def filter(self, text: str, only_problems: bool = False, problem_col: int | None = None) -> int:
        """Скрыть строки, не содержащие текст (в любой ячейке) либо не помеченные как проблемные.
        Проблемной считается строка с непустым текстом в столбце problem_col.  Возвращает число видимых строк."""
        needle = text.strip().lower()
        shown = 0
        for r, row in enumerate(self.model().rows):
            hide = False
            if needle:
                hide = not any(needle in c.text.lower() for c in row)
            if not hide and only_problems and problem_col is not None:
                hide = not row[problem_col].text.strip()
            self.setRowHidden(r, hide)
            shown += not hide
        self._rows_timer.start()
        return shown

    def _size_visible_rows(self) -> None:
        self._rows_timer.stop()
        n = self.model().rowCount()
        if not self.isVisible() or not n or self._fit_pending:      # строки измерит подгонка столбцов
            return
        vh = self.verticalHeader()
        for _ in range(8):                     # подогнанные строки меняют высоту — видимый диапазон сдвигается
            top = max(0, self.rowAt(0))
            bottom = self.rowAt(self.viewport().height())
            if bottom < 0:
                bottom = n - 1
            todo = [r for r in range(top, bottom + 1) if r not in self._sized and not self.isRowHidden(r)]
            if not todo:
                break
            for r in todo:
                self.resizeRowToContents(r)
                self._sized.add(r)
            if not self._one_line:
                # неизмеренные строки — высотой с однострочную (самая низкая ячейка первой строки),
                # чтобы при прокрутке строки не «прыгали»
                opt = QStyleOptionViewItem()
                self.initViewItemOption(opt)
                model, dlg = self.model(), self.itemDelegate()
                self._one_line = max(vh.minimumSectionSize(),
                                     min(dlg.sizeHint(opt, model.index(todo[0], c)).height() for c in range(model.columnCount())))
            if vh.defaultSectionSize() != self._one_line:       # перезагрузка модели сбрасывает высоту по умолчанию
                vh.setDefaultSectionSize(self._one_line)


def item(text, value=None, editable: bool = False, color: QColor | None = None, tip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    if value is not None:
        it.setData(Qt.UserRole, value)
    it.setFlags(_FLAGS_EDIT if editable else _FLAGS_RO)
    if color is not None:
        it.setBackground(QBrush(color))
        it.setForeground(QBrush(QColor("black")))
    if tip:
        it.setToolTip(tip)
    return it


def check_item(checked: bool, tip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem()
    it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
    it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
    if tip:
        it.setToolTip(tip)
    return it


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
    """Выбор нескольких значений из списка с фильтром по подстроке.

    ``ordered=True`` — порядок отмеченных имеет значение (приоритет): они нумеруются, вновь отмеченный
    встаёт в конец, кнопки «Выше»/«Ниже» переставляют; ``values()`` возвращает их в этом порядке.
    """

    def __init__(self, title: str, options: list[tuple[str, object]], checked: list, parent=None, hint: str = "",
                 ordered: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 560)
        self.ordered = ordered
        self._busy = False
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
        checked = list(checked)
        checked_set = set(checked)
        # отмеченные — сверху (в упорядоченном режиме — в порядке приоритета)
        for label, value in sorted(options, key=lambda o: (o[1] not in checked_set,
                                                           checked.index(o[1]) if ordered and o[1] in checked_set else 0,
                                                           o[0].lower())):
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, value)
            it.setData(Qt.UserRole + 1, label)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if value in checked_set else Qt.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        clear = QPushButton("Снять все")
        clear.clicked.connect(self._clear)
        row.addWidget(clear)
        if ordered:
            up, down = QPushButton("Выше"), QPushButton("Ниже")
            up.clicked.connect(lambda: self._move(-1))
            down.clicked.connect(lambda: self._move(1))
            row.addWidget(up)
            row.addWidget(down)
            self.list.itemChanged.connect(self._on_changed)
            self._renumber()
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
        self._busy = True
        try:
            for i in range(self.list.count()):
                self.list.item(i).setCheckState(Qt.Unchecked)
        finally:
            self._busy = False
        if self.ordered:
            self._renumber()

    def _checked_rows(self) -> list[int]:
        return [i for i in range(self.list.count()) if self.list.item(i).checkState() == Qt.Checked]

    def _on_changed(self, it):
        if self._busy:
            return
        self._busy = True
        try:
            if it.checkState() == Qt.Checked:
                # вновь отмеченный — в конец отмеченных
                row = self.list.row(it)
                target = max((i for i in self._checked_rows() if i != row), default=-1) + 1
                if target > row:
                    target -= 1
                if target != row:
                    self.list.insertItem(target, self.list.takeItem(row))
                    self.list.setCurrentItem(it)
            self._renumber()
        finally:
            self._busy = False

    def _move(self, d: int):
        it = self.list.currentItem()
        if it is None or it.checkState() != Qt.Checked:
            return
        row = self.list.row(it)
        target = row + d
        if not (0 <= target < self.list.count()) or self.list.item(target).checkState() != Qt.Checked:
            return
        self._busy = True
        try:
            self.list.insertItem(target, self.list.takeItem(row))
            self.list.setCurrentItem(it)
            self._renumber()
        finally:
            self._busy = False

    def _renumber(self):
        was, self._busy = self._busy, True         # setText тоже испускает itemChanged
        try:
            n = 0
            for i in range(self.list.count()):
                it = self.list.item(i)
                label = it.data(Qt.UserRole + 1)
                if it.checkState() == Qt.Checked:
                    n += 1
                    it.setText(f"{n}. {label}")
                else:
                    it.setText(label)
        finally:
            self._busy = was

    def values(self) -> list:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


class PickDialog(QDialog):
    """Выбор одного значения из таблицы с поиском по подстроке.

    ``rows`` — список ``(ячейки, значение, подходит)``: строки с ``подходит=False`` показываются
    серым, ниже подходящих, и скрываются переключателем «только подходящие».  Кнопка ``auto_label``
    (если задана) закрывает диалог с ``value() is None`` — «сбросить на автоматический выбор».
    """

    def __init__(self, title: str, headers: list[str], rows: list[tuple[list[str], object, bool]], current,
                 parent=None, hint: str = "", auto_label: str = "", only_fit_label: str = "только подходящие"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 560)
        self._value = current
        lay = QVBoxLayout(self)
        if hint:
            lab = QLabel(hint)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("поиск…")
        self.filter.textChanged.connect(self._apply_filter)
        top.addWidget(self.filter, 1)
        n_fit = sum(1 for _, _, fit in rows if fit)
        self.only_fit = QCheckBox(f"{only_fit_label} ({n_fit})")
        self.only_fit.setChecked(0 < n_fit < len(rows))
        self.only_fit.setEnabled(0 < n_fit < len(rows))
        self.only_fit.toggled.connect(self._apply_filter)
        top.addWidget(self.only_fit)
        lay.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(headers)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setAllColumnsShowFocus(True)
        grey = QBrush(QColor("#808080"))
        bold = QFont()
        bold.setBold(True)
        current_item = None
        for cells, value, fit in sorted(rows, key=lambda r: not r[2]):
            it = QTreeWidgetItem([str(c) for c in cells])
            it.setData(0, Qt.UserRole, value)
            it.setData(0, Qt.UserRole + 1, fit)
            if not fit:
                for c in range(len(cells)):
                    it.setForeground(c, grey)
            if current is not None and value == current:
                it.setFont(0, bold)
                current_item = it
            self.tree.addTopLevelItem(it)
        for c in range(len(headers)):
            self.tree.resizeColumnToContents(c)
        self.tree.itemDoubleClicked.connect(lambda *_: self.accept())
        self.tree.itemActivated.connect(lambda *_: self.accept())
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        if auto_label:
            auto = QPushButton(auto_label)
            auto.clicked.connect(self._auto)
            row.addWidget(auto)
        row.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)
        self._apply_filter()
        if current_item is not None:
            self.tree.setCurrentItem(current_item)
            self.tree.scrollToItem(current_item)
        self.filter.setFocus()

    def _apply_filter(self, *_):
        needle = self.filter.text().strip().lower()
        only_fit = self.only_fit.isChecked()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            text = " ".join(it.text(c) for c in range(it.columnCount())).lower()
            hide = (bool(needle) and needle not in text) or (only_fit and not it.data(0, Qt.UserRole + 1))
            it.setHidden(hide and it.data(0, Qt.UserRole) != self._value)

    def _auto(self):
        self._value = None
        self.tree.clearSelection()
        self.done(QDialog.Accepted + 1)       # отличается от Accepted: вызывающий проверяет is_auto()

    def accept(self):
        it = self.tree.currentItem()
        if it is None or it.isHidden():
            return
        self._value = it.data(0, Qt.UserRole)
        super().accept()

    def is_auto(self) -> bool:
        return self.result() == QDialog.Accepted + 1

    def value(self):
        return self._value


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
