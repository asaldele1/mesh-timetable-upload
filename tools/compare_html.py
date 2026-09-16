"""Сверка разбора .chngf с HTML-отчётом самого ХроноГрафа.

В ХроноГрафе: Экраны → Составление расписания, затем Отчёт → Расписание классов,
«По отдельности», ФИО преподавателей — все, кабинеты — все, вывод — HTML.
Затем:  python tools/compare_html.py отчёт.html файл.chngf
Сравниваются все ячейки (предмет, учитель, номер группы, кабинет).
"""
import collections
import html
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from timetable_upload import chngf  # noqa: E402

LESSON_ROW = re.compile(r'<tr>\s*<td class=T2><Strong>(\d+)</Strong></td>\s*<td class=T2><Strong>[^<]*</Strong></td>')


def parse_html(path):
    """(класс, день, урок) -> {(предмет, учитель, группа, кабинет)} из HTML-отчёта «Расписание классов»."""
    with open(path, encoding="cp1251") as f:
        s = f.read()
    out = {}
    for m in re.finditer(r"<Caption><H2>(.*?)</H2>", s):
        cls = html.unescape(m.group(1)).strip()
        start = m.end()
        nxt = s.find("<Caption><H2>", start)
        block = s[start:nxt if nxt > 0 else len(s)]
        parts = LESSON_ROW.split(block)
        for lesson_no, cells_html in zip(parts[1::2], parts[2::2]):
            cells = re.split(r"<td class=T1[^>]*>", cells_html)[1:]
            for day, cell in enumerate(cells):
                if "<table cellspacing=0 border=0" not in cell:
                    continue
                # первая строка вложенной таблицы — пары предмет/учитель, вторая — кабинеты
                inner = cell[cell.find("<table"):]
                inner = inner[:inner.lower().find("</table>")]
                trs = re.findall(r"<tr>(.*?)</tr>", inner, re.DOTALL | re.IGNORECASE)
                tds1 = re.findall(r"<td[^>]*>(.*?)</td>", trs[0], re.DOTALL)
                tds2 = re.findall(r"<td[^>]*>(.*?)</td>", trs[1], re.DOTALL) if len(trs) > 1 else []
                items = set()
                subj = ""
                for k in range(0, len(tds1), 2):
                    subj = html.unescape(tds1[k]).strip().lstrip("/").strip() or subj
                    teacher = html.unescape(tds1[k + 1]).strip()
                    grp = 0
                    mm = re.match(r"(.*?):(\d+)$", teacher)
                    if mm:
                        teacher, grp = mm.group(1), int(mm.group(2))
                    cab = html.unescape(tds2[k // 2]).strip() if k // 2 < len(tds2) else ""
                    if cab == "\xa0":
                        cab = ""
                    items.add((subj, teacher, grp, cab))
                out[(cls, day, int(lesson_no) - 1)] = items
    return out


def from_chngf(path):
    tt = chngf.load(path)
    out = collections.defaultdict(set)
    for l in tt.iter_lessons():
        t = tt.teachers[l.teacher_index] if l.teacher_index >= 0 else None
        out[(l.cls, l.day, l.lesson)].add((l.subject, t.short_name if t else "", l.group, l.cabinet))
    return out


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    h, c = parse_html(argv[1]), from_chngf(argv[2])
    print("html slots", len(h), "chngf slots", len(c))
    diff = 0
    for k in sorted(set(h) | set(c)):
        if h.get(k, set()) != c.get(k, set()):
            diff += 1
            if diff <= 25:
                print(k, "\n   html :", sorted(h.get(k, set())), "\n   chngf:", sorted(c.get(k, set())))
    print("differences:", diff)
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
