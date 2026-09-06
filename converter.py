"""
MD → DOCX Конвертер (Модуль 3 — Сборщик по ГОСТу)
==================================================
Ввод:  markdown-файл + шаблон Word (.docx)
Вывод: готовый .docx файл с ГОСТ-стилями

Зависимости:
    pip install python-docx latex2mathml lxml Pillow matplotlib
"""

import re
import sys
import tempfile
import argparse
import logging
from pathlib import Path
from typing import Optional
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from lxml import etree

# Глушим лишний шум от matplotlib
logging.getLogger('matplotlib').setLevel(logging.ERROR)

# ─────────────────────────────────────────────────────────────
# 1. LATEX → OMML  (через latex2mathml + XSLT, если есть)
# ─────────────────────────────────────────────────────────────

MATHML2OMML_XSLT = "MML2OMML.XSL"  # положить рядом со скриптом для нативных формул

# Пространство имён OMML
OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
M = f"{{{OMML_NS}}}"

# Бинарные операторы, на которых останавливаем "затаскивание" подынтегрального
# выражения внутрь m:nary. Если встретили run с одним из этих текстов —
# подынтегральное закончилось, дальше уже другое выражение.
NARY_STOP_TEXTS = {'=', '<', '>', '≤', '≥', '≠', '≈', '±',
                   '⇒', '⇔', '→', '↔', '≡', '∈', '∉', '⊂', '⊃',
                   '·', '×', '∧', '∨'}


def _split_runs_at_brackets(omml_root: etree._Element) -> None:
    """Расщепляет m:r у которых в тексте есть скобки ( ) [ ].

    XSLT часто склеивает соседние символы в один run, например:
        <m:r><m:t>M=(</m:t></m:r>
    После расщепления:
        <m:r><m:t>M=</m:t></m:r>
        <m:r><m:t>(</m:t></m:r>

    Это нужно чтобы последующие фиксы (_fix_empty_nary,
    _fix_brackets_around_big) могли видеть скобки как отдельные
    одиночные runs и обрабатывать их.
    """
    BRACKETS = set('()[]')

    all_rs = list(omml_root.iter(f"{M}r"))
    for r in all_rs:
        t = r.find(f"{M}t")
        if t is None or not t.text:
            continue
        text = t.text
        # Если в тексте нет скобок — пропускаем
        if not any(ch in BRACKETS for ch in text):
            continue

        # Расщепляем текст на части: каждая скобка — отдельная часть
        parts = []
        current = []
        for ch in text:
            if ch in BRACKETS:
                if current:
                    parts.append(''.join(current))
                    current = []
                parts.append(ch)
            else:
                current.append(ch)
        if current:
            parts.append(''.join(current))

        if len(parts) <= 1:
            continue

        # Создаём новые m:r для каждой части
        parent = r.getparent()
        if parent is None:
            continue
        idx = list(parent).index(r)
        rpr = r.find(f"{M}rPr")
        xml_space = '{http://www.w3.org/XML/1998/namespace}space'
        space_val = t.attrib.get(xml_space)

        for part in parts:
            new_r = etree.Element(f"{M}r")
            if rpr is not None:
                new_r.append(etree.fromstring(etree.tostring(rpr)))
            new_t = etree.SubElement(new_r, f"{M}t")
            new_t.text = part
            if space_val:
                new_t.set(xml_space, space_val)
            parent.insert(idx, new_r)
            idx += 1
        # Удаляем оригинал
        parent.remove(r)


def _fix_empty_nary(omml_root: etree._Element) -> None:
    """Лечит пустые <m:e/> в n-арных операторах (интегралы, суммы, произведения).

    Стандартная MML2OMML.XSL переводит `\\int_a^b f(x)dx` как
        <m:nary>...<m:e/></m:nary><m:r>f(x)dx</m:r>
    Подынтегральное выражение оказывается СНАРУЖИ оператора, а внутри —
    пустая база, на которую Word рисует placeholder-квадратик.

    Лечим эвристикой: после каждого <m:nary> с пустой <m:e/> подтягиваем
    внутрь следующие соседние элементы, пока не встретим бинарный оператор
    (=, ≤, ·, ...).

    Балансируем скобки: если в run-е встречается закрывающая скобка БЕЗ
    парной открывающей (значит, эта скобка относится к внешней группе типа
    f(...) обёрнутой вокруг интеграла) — расщепляем run в этой точке.
    Часть до лишней ')' идёт внутрь, остаток остаётся снаружи как
    отдельный run.

    Важно: обрабатываем в порядке "изнутри наружу" (от последнего nary к
    первому), чтобы вложенные операторы (двойные интегралы \\int\\int)
    собирались правильно: сначала внутренний забирает свой операнд,
    затем внешний забирает уже собранный внутренний целиком.
    """
    OPEN_BRACKETS = '([{'
    CLOSE_BRACKETS = ')]}'

    all_narys = list(omml_root.iter(f"{M}nary"))
    for nary in reversed(all_narys):
        e = nary.find(f"{M}e")
        if e is None:
            continue
        if len(e) > 0 or (e.text and e.text.strip()):
            continue

        parent = nary.getparent()
        if parent is None:
            continue
        siblings = list(parent)
        try:
            idx = siblings.index(nary)
        except ValueError:
            continue

        to_move = []
        running_depth = 0  # баланс скобок ( vs )
        stop_after = False  # надо ли прервать после текущего sibling

        for sib in siblings[idx + 1:]:
            if sib.tag == f"{M}r":
                t = sib.find(f"{M}t")
                if t is not None and t.text:
                    text = t.text
                    stripped = text.strip()
                    # Стоп на чистом бинарном операторе
                    if stripped in NARY_STOP_TEXTS:
                        break
                    # Идём по символам, ищем точку где баланс уходит в минус
                    d = running_depth
                    split_at = None
                    for k, ch in enumerate(text):
                        if ch in OPEN_BRACKETS:
                            d += 1
                        elif ch in CLOSE_BRACKETS:
                            d -= 1
                            if d < 0:
                                split_at = k
                                break
                    if split_at is not None:
                        # Расщепляем: до split_at — внутрь, после — остаётся
                        if split_at > 0:
                            new_r = etree.Element(f"{M}r")
                            # Копируем rPr если есть
                            rpr = sib.find(f"{M}rPr")
                            if rpr is not None:
                                new_r.append(etree.fromstring(etree.tostring(rpr)))
                            new_t = etree.SubElement(new_r, f"{M}t")
                            new_t.text = text[:split_at]
                            # Сохраняем xml:space если оригинал имел
                            xml_space = '{http://www.w3.org/XML/1998/namespace}space'
                            if xml_space in t.attrib:
                                new_t.set(xml_space, t.attrib[xml_space])
                            to_move.append(new_r)
                        # Урезаем оригинал — он остаётся в parent после nary
                        t.text = text[split_at:]
                        break
                    running_depth = d
            # ВНИМАНИЕ: на m:nary НЕ стопим — захватываем (вложенные интегралы).
            to_move.append(sib)

        for sib in to_move:
            if sib.getparent() is parent:
                parent.remove(sib)
            e.append(sib)


# Элементы, которые рисуются "большими" и поэтому скобки вокруг них
# тоже должны быть большими (растягивающимися) — оборачиваем в <m:d>.
BIG_OMML_ELEMENTS = ('nary', 'f', 'rad', 'sSubSup', 'box', 'eqArr', 'm')


def _fix_cases_braces(omml_root: etree._Element) -> None:
    """Чинит фигурную скобку у систем уравнений (\\begin{cases}).

    MML2OMML.XSL переводит { как обычный run-символ, не как растягивающийся
    delimiter. На выходе получаем маленькую '{' рядом с большим столбцом
    уравнений. Лечим: ищем m:eqArr → если перед ним стоит '{' — убираем его
    и оборачиваем eqArr в <m:d> с begChr="{" endChr="".
    """
    for eqArr in list(omml_root.iter(f"{M}eqArr")):
        parent = eqArr.getparent()
        if parent is None:
            continue
        siblings = list(parent)
        try:
            idx = siblings.index(eqArr)
        except ValueError:
            continue

        # Ищем '{' непосредственно перед (может быть через одну запись если что-то нейтральное)
        prev = siblings[idx - 1] if idx > 0 else None
        beg_char = ""
        if prev is not None and prev.tag == f"{M}r":
            t = prev.find(f"{M}t")
            if t is not None and t.text and t.text.strip() == '{':
                beg_char = '{'
                parent.remove(prev)
                idx -= 1

        # Создаём <m:d> с правильными свойствами
        d = etree.Element(f"{M}d")
        dPr = etree.SubElement(d, f"{M}dPr")
        if beg_char:
            etree.SubElement(dPr, f"{M}begChr").set(f"{M}val", beg_char)
        # У cases закрывающая скобка обычно пустая
        etree.SubElement(dPr, f"{M}endChr").set(f"{M}val", "")
        e = etree.SubElement(d, f"{M}e")

        # Перемещаем eqArr внутрь <m:d><m:e>
        parent.remove(eqArr)
        e.append(eqArr)
        parent.insert(idx, d)


def _fix_brackets_around_big(omml_root: etree._Element) -> None:
    """Чинит обычные скобки ( ) [ ] которые охватывают "большие" элементы
    (интегралы, дроби, корни) — оборачивает в <m:d> для автомасштабирования.

    Пример: `f(\\int_a^b xdF(x))` после XSLT даёт цепочку
        m:r 'f', m:r '(', m:nary ..., m:r ')'
    Внешняя '(' остаётся маленькой, не растягивается под высоту интеграла.
    Лечим: ищем пары ( ) которые содержат внутри хотя бы один "большой"
    элемент, и оборачиваем содержимое в <m:d>.
    """
    PAIRS = {'(': ')', '[': ']'}

    # Обрабатываем все контейнеры где могут быть последовательности
    # (m:oMath, m:e, m:num, m:den и т.д.)
    for container in list(omml_root.iter()):
        children = list(container)
        i = 0
        while i < len(children):
            child = children[i]
            # Открывающая скобка как одиночный m:r?
            open_char = _get_single_char(child)
            if open_char not in PAIRS:
                i += 1
                continue
            close_char = PAIRS[open_char]

            # Ищем парную закрывающую — простой счётчик уровня вложенности
            depth = 1
            contains_big = False
            close_idx = None
            for j in range(i + 1, len(children)):
                sib = children[j]
                c = _get_single_char(sib)
                if c == open_char:
                    depth += 1
                elif c == close_char:
                    depth -= 1
                    if depth == 0:
                        close_idx = j
                        break
                # Если попался "большой" элемент — запоминаем
                tag_local = etree.QName(sib.tag).localname
                if tag_local in BIG_OMML_ELEMENTS:
                    contains_big = True

            if close_idx is None or not contains_big:
                i += 1
                continue

            # Нашли пару скобок вокруг "большого" — заворачиваем
            inner = children[i + 1:close_idx]
            d = etree.Element(f"{M}d")
            dPr = etree.SubElement(d, f"{M}dPr")
            etree.SubElement(dPr, f"{M}begChr").set(f"{M}val", open_char)
            etree.SubElement(dPr, f"{M}endChr").set(f"{M}val", close_char)
            e = etree.SubElement(d, f"{M}e")
            for el in inner:
                container.remove(el)
                e.append(el)
            # Удаляем открывающую и закрывающую
            container.remove(children[i])
            container.remove(children[close_idx])
            container.insert(i, d)

            # Перечитываем children — структура изменилась
            children = list(container)
            i += 1


def _get_single_char(elem: etree._Element):
    """Возвращает текст из m:r/m:t если это одиночный символ, иначе None."""
    if elem.tag != f"{M}r":
        return None
    t = elem.find(f"{M}t")
    if t is None or t.text is None:
        return None
    txt = t.text.strip()
    if len(txt) == 1:
        return txt
    return None


def latex_to_omml(latex_str: str) -> Optional[etree._Element]:
    """Конвертирует LaTeX → MathML → OMML (XML-элемент)."""
    try:
        import latex2mathml.converter as l2m
        mathml_str = l2m.convert(latex_str)
    except Exception:
        return None

    xslt_path = Path(__file__).parent / MATHML2OMML_XSLT
    if not xslt_path.exists():
        return None

    try:
        mathml_doc = etree.fromstring(mathml_str.encode())
        xslt_doc = etree.parse(str(xslt_path))
        transform = etree.XSLT(xslt_doc)
        omml = transform(mathml_doc)
        root = omml.getroot()
        # Постобработка (порядок важен):
        # 1. Расщепить runs со склеенными скобками
        _split_runs_at_brackets(root)
        # 2. Затащить подынтегральные внутрь интегралов
        _fix_empty_nary(root)
        # 3. Cases → m:d с фигурной скобкой
        _fix_cases_braces(root)
        # 4. Обычные скобки вокруг "больших" → m:d
        _fix_brackets_around_big(root)
        return root
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 2. LATEX → PNG (запасной рендер через matplotlib mathtext)
# ─────────────────────────────────────────────────────────────

# matplotlib mathtext не понимает некоторые команды — мапим в эквиваленты
LATEX_REPLACEMENTS = [
    (r'\\le\b',       r'\\leq'),
    (r'\\ge\b',       r'\\geq'),
    (r'\\implies\b',  r'\\Rightarrow'),
    (r'\\iff\b',      r'\\Leftrightarrow'),
    (r'\\to\b',       r'\\rightarrow'),
    (r'\\text\b',     r'\\mathrm'),         # \text{...} → \mathrm{...}
    (r'\\dots\b',     r'\\ldots'),
]

# Многострочные окружения, которые matplotlib не тянет (\begin{cases}, etc.)
# Просто рендерим без них как fallback — будет некрасиво, но не упадёт
UNSUPPORTED_ENVS = [r'\begin{cases}', r'\end{cases}',
                    r'\begin{aligned}', r'\end{aligned}',
                    r'\begin{align}', r'\end{align}',
                    r'\begin{matrix}', r'\end{matrix}']


def normalize_latex(s: str) -> str:
    """Приводит LaTeX к виду, который matplotlib mathtext умеет рендерить."""
    for old, new in LATEX_REPLACEMENTS:
        s = re.sub(old, new, s)
    for env in UNSUPPORTED_ENVS:
        s = s.replace(env, '')
    # Убираем переносы строк — mathtext однострочный
    s = ' '.join(s.split())
    return s


def formula_as_image(latex_str: str, out_dir: Path, idx: int,
                     inline: bool = False) -> Optional[Path]:
    """Рендерит LaTeX в PNG через matplotlib.mathtext."""
    if not latex_str.strip():
        return None

    norm = normalize_latex(latex_str)
    if not norm.strip():
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import mathtext
        from matplotlib.font_manager import FontProperties

        out_path = out_dir / f"formula_{idx}.png"
        # fontsize: 12pt для inline (под текст), 14pt для блочной
        fontsize = 12 if inline else 14
        # math_to_image возвращает depth, но нам он не нужен
        mathtext.math_to_image(f'${norm}$', str(out_path),
                               dpi=200, format='png',
                               prop=FontProperties(size=fontsize))
        return out_path
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 3. ВСТАВКА ФОРМУЛЫ В ПАРАГРАФ
# ─────────────────────────────────────────────────────────────

def add_omml_to_paragraph(para, omml_elem: etree._Element):
    """Добавляет OMML-элемент в параграф."""
    para._p.append(omml_elem)


def insert_formula(para, latex_str: str, formula_idx: int,
                   tmp_dir: Path, inline: bool = False) -> bool:
    """
    Пытается вставить формулу. Возвращает True если получилось.
    Стратегии (по убыванию качества):
      1. Нативный OMML (если есть latex2mathml + XSLT)
      2. PNG через matplotlib mathtext
      3. Текст в [скобках] моноширинным шрифтом
    """
    if not latex_str.strip():
        return False

    # Стратегия 1: OMML
    omml = latex_to_omml(latex_str)
    if omml is not None:
        add_omml_to_paragraph(para, omml)
        return True

    # Стратегия 2: PNG
    img_path = formula_as_image(latex_str, tmp_dir, formula_idx, inline=inline)
    if img_path and img_path.exists():
        run = para.add_run()
        try:
            if inline:
                # Инлайн — высота под строку текста
                run.add_picture(str(img_path), height=Pt(14))
            else:
                # Блочная — естественная высота, но ширина не больше страницы
                from PIL import Image
                with Image.open(str(img_path)) as img:
                    w_px, _ = img.size
                w_in = w_px / 200  # DPI = 200
                if w_in > 6.0:
                    run.add_picture(str(img_path), width=Inches(6.0))
                else:
                    run.add_picture(str(img_path), width=Inches(w_in))
            return True
        except Exception:
            pass

    # Стратегия 3: fallback — текст
    run = para.add_run(f"[{latex_str}]")
    run.font.name = "Courier New"
    run.font.size = Pt(10)
    return False


# ─────────────────────────────────────────────────────────────
# 4. РЕГУЛЯРКИ
# ─────────────────────────────────────────────────────────────

RE_IMAGE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

# Инлайн-разметка: bold / italic / code / formula / image
# Важно: $[^$]+$ — НЕ матчит пустую формулу (внутри обязан быть хоть один символ)
RE_INLINE = re.compile(
    r'(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\$[^$]+\$|!\[[^\]]*\]\([^)]+\))'
)

RE_HTML_COMMENT = re.compile(r'^\s*<!--.*?-->\s*$')
RE_PAGE_BREAK_DIV = re.compile(r'<div[^>]*page-break[^>]*>.*?</div>', re.IGNORECASE)

# Строка-разделитель MD-таблицы: только |, -, :, пробелы; хотя бы один '-'
RE_MD_TABLE_SEPLINE = re.compile(r'^[\|\-:\s]+$')


def is_md_table_separator(line: str) -> bool:
    """Проверяет, является ли строка разделителем заголовка Markdown-таблицы.
    Пример: | :--- | :---: | ---: |"""
    s = line.strip()
    if not s or '-' not in s:
        return False
    return bool(RE_MD_TABLE_SEPLINE.match(s))


def split_md_table_row(line: str) -> list:
    """Режет строку Markdown-таблицы на ячейки по '|', игнорируя '|' внутри $...$.
    Это важно: в математическом тексте часто встречаются модули вида $|x|$,
    которые иначе бы сломали парсинг. Двойной $$...$$ обрабатывается как одно
    переключение math-режима (чтобы '|' внутри блочной формулы тоже не резал)."""
    s = line.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]

    cells = []
    buf = []
    in_math = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '$':
            # $$ — одно переключение (блочная математика)
            if i + 1 < len(s) and s[i + 1] == '$':
                in_math = not in_math
                buf.append('$$')
                i += 2
                continue
            in_math = not in_math
            buf.append(ch)
        elif ch == '|' and not in_math:
            cells.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append(''.join(buf).strip())
    return cells


# ─────────────────────────────────────────────────────────────
# 5. ПАРСЕР MARKDOWN → DOCX
# ─────────────────────────────────────────────────────────────

class MdToDocxConverter:
    HEADING_STYLES = {1: "Heading 1", 2: "Heading 2",
                      3: "Heading 3", 4: "Heading 4"}
    BODY_STYLE = "Normal"

    def __init__(self, template_path: str, images_dir: str = "."):
        self.doc = Document(template_path)
        self.images_dir = Path(images_dir)
        # Кросс-платформенная временная папка (Linux: /tmp/..., Windows: %TEMP%\...)
        self.tmp_dir = Path(tempfile.gettempdir()) / "md_docx_formulas"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        # очищаем старые формулы
        for f in self.tmp_dir.glob("formula_*.png"):
            f.unlink()
        self._formula_idx = 0

        self.stats = {
            "headings": 0, "paragraphs": 0, "tables": 0,
            "images_found": 0, "images_inserted": 0,
            "formulas_found": 0, "formulas_inserted": 0,
            "formulas_as_text": 0,
            "image_regions_skipped": 0,
            "html_comments_skipped": 0,
            "page_breaks": 0,
        }

    def _get_style(self, name: str):
        try:
            return self.doc.styles[name]
        except KeyError:
            return self.doc.styles["Normal"]

    # ── Инлайн разметка ─────────────────────────────────────

    def _apply_inline(self, para, text: str):
        """Парсит **bold**, *italic*, `code`, $formula$, ![img](path) внутри строки."""
        parts = RE_INLINE.split(text)
        for part in parts:
            if not part:
                continue

            if part.startswith('**') and part.endswith('**') and len(part) > 4:
                run = para.add_run(part[2:-2])
                run.bold = True

            elif part.startswith('*') and part.endswith('*') and len(part) > 2:
                run = para.add_run(part[1:-1])
                run.italic = True

            elif part.startswith('`') and part.endswith('`') and len(part) > 2:
                run = para.add_run(part[1:-1])
                run.font.name = "Courier New"

            elif part.startswith('$') and part.endswith('$') and len(part) > 2:
                latex = part[1:-1].strip()
                if not latex:
                    continue
                self.stats["formulas_found"] += 1
                self._formula_idx += 1
                ok = insert_formula(para, latex, self._formula_idx,
                                    self.tmp_dir, inline=True)
                if ok:
                    self.stats["formulas_inserted"] += 1
                else:
                    self.stats["formulas_as_text"] += 1

            elif m := RE_IMAGE.match(part):
                alt, path = m.group(1), m.group(2)
                self._insert_image(para, path, alt)

            else:
                para.add_run(part)

    def _insert_image(self, para, rel_path: str, alt: str = ""):
        self.stats["images_found"] += 1
        img_path = self.images_dir / rel_path
        if not img_path.exists():
            para.add_run(f"[Изображение не найдено: {rel_path}]")
            return
        try:
            run = para.add_run()
            run.add_picture(str(img_path), width=Inches(5.5))
            self.stats["images_inserted"] += 1
        except Exception:
            pass

    # ── LaTeX-таблицы ───────────────────────────────────────

    def _parse_latex_table(self, raw: str) -> list:
        """Парсит \\begin{tabular}...\\end{tabular} в список строк."""
        raw = re.sub(r'\\begin\{tabular\}\{[^}]*\}', '', raw)
        raw = re.sub(r'\\end\{tabular\}', '', raw)

        rows = []
        for row_raw in raw.split('\\\\'):
            row_raw = re.sub(r'\\hline', '', row_raw).strip()
            if not row_raw:
                continue
            cells = [c.strip() for c in row_raw.split('&')]
            if any(c for c in cells):
                # Снимаем $...$ обрамление если ячейка целиком формула — рендерим как текст
                rows.append(cells)
        return rows

    def _add_table(self, rows: list):
        if not rows:
            return
        cols = max(len(r) for r in rows)
        table = self.doc.add_table(rows=0, cols=cols)
        try:
            table.style = self.doc.styles["Table Grid"]
        except KeyError:
            pass

        for i, row_data in enumerate(rows):
            row = table.add_row()
            for j in range(cols):
                cell_text = row_data[j] if j < len(row_data) else ""
                # $$...$$ внутри ячейки не имеет смысла (блочная формула в
                # ячейке таблицы). Нормализуем в inline $...$.
                if '$$' in cell_text:
                    cell_text = cell_text.replace('$$', '$')
                cell = row.cells[j]
                # Параграф ячейки создан автоматически и пуст —
                # прогоняем через _apply_inline, чтобы $формулы$ рендерились
                # так же, как в основном тексте (OMML/PNG/fallback).
                cell_para = cell.paragraphs[0]
                self._apply_inline(cell_para, cell_text)
                # Жирный заголовок (только если в ячейке есть текстовые runs —
                # формулы-картинки или OMML делать жирными нет смысла)
                if i == 0:
                    for run in cell_para.runs:
                        if run.text:
                            run.bold = True
        self.stats["tables"] += 1

    def _parse_markdown_table(self, lines: list, start: int):
        """Парсит Markdown-таблицу начиная со строки lines[start].
        Возвращает (rows, next_idx). Предполагается, что вызывающий код
        уже проверил, что lines[start] начинается с '|' и lines[start+1]
        — это разделитель."""
        rows = [split_md_table_row(lines[start])]
        idx = start + 2  # пропускаем строку заголовка и разделитель
        while idx < len(lines):
            s = lines[idx].strip()
            if not s.startswith('|'):
                break
            # На случай, если внутри таблицы попался ещё один разделитель — пропустим
            if is_md_table_separator(s):
                idx += 1
                continue
            rows.append(split_md_table_row(s))
            idx += 1
        return rows, idx

    # ── Главный парсер ──────────────────────────────────────

    def convert(self, md_text: str):
        lines = md_text.splitlines()
        i = 0

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Пустая
            if not stripped:
                i += 1
                continue

            # HTML-комментарий (одна строка)
            if RE_HTML_COMMENT.match(line):
                self.stats["html_comments_skipped"] += 1
                i += 1
                continue

            # Разрыв страницы
            if 'page-break' in stripped or stripped == '---':
                if self.stats["page_breaks"] > 0 or stripped != '---':
                    self.doc.add_page_break()
                    self.stats["page_breaks"] += 1
                i += 1
                continue

            # Плейсхолдер картинки
            if stripped == '[IMAGE_REGION]':
                self.stats["image_regions_skipped"] += 1
                p = self.doc.add_paragraph(style=self._get_style(self.BODY_STYLE))
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                r = p.add_run("[Изображение]")
                r.italic = True
                r.font.color.rgb = None  # серый можно добавить при желании
                i += 1
                continue

            # Заголовки
            if m := re.match(r'^(#{1,4})\s+(.+)', line):
                level = len(m.group(1))
                style_name = self.HEADING_STYLES.get(level, "Heading 4")
                para = self.doc.add_paragraph(style=self._get_style(style_name))
                self._apply_inline(para, m.group(2).strip())
                self.stats["headings"] += 1
                i += 1
                continue

            # Блочная формула $$ ... $$
            if stripped.startswith('$$'):
                block = stripped[2:]
                # однострочная: $$formula$$
                if block.endswith('$$') and len(block) > 2:
                    latex = block[:-2].strip()
                    self._add_block_formula(latex)
                    i += 1
                    continue
                # многострочная
                latex_lines = [block] if block else []
                i += 1
                while i < len(lines) and '$$' not in lines[i]:
                    latex_lines.append(lines[i])
                    i += 1
                if i < len(lines):
                    latex_lines.append(lines[i].strip().replace('$$', ''))
                    i += 1
                latex = '\n'.join(latex_lines).strip()
                self._add_block_formula(latex)
                continue

            # Markdown-таблица: текущая строка — | ... |,
            # следующая — разделитель (| --- | --- |)
            if (stripped.startswith('|') and i + 1 < len(lines)
                    and is_md_table_separator(lines[i + 1])):
                rows, i = self._parse_markdown_table(lines, i)
                self._add_table(rows)
                continue

            # LaTeX-таблица (fallback на случай старого формата от Vision-агента)
            if r'\begin{tabular}' in line:
                table_lines = [line]
                while r'\end{tabular}' not in table_lines[-1] and i + 1 < len(lines):
                    i += 1
                    table_lines.append(lines[i])
                i += 1
                rows = self._parse_latex_table('\n'.join(table_lines))
                self._add_table(rows)
                continue

            # Картинка отдельной строкой
            if m := RE_IMAGE.match(stripped):
                alt, path = m.group(1), m.group(2)
                p = self.doc.add_paragraph(style=self._get_style(self.BODY_STYLE))
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                self._insert_image(p, path, alt)
                if alt:
                    cap = self.doc.add_paragraph(alt, style=self._get_style(self.BODY_STYLE))
                    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                i += 1
                continue

            # Маркированный список
            if re.match(r'^[-*]\s+', line):
                para = self.doc.add_paragraph(style=self._get_style("List Bullet"))
                self._apply_inline(para, line[2:].strip())
                i += 1
                continue

            # Нумерованный список
            if re.match(r'^\d+\.\s+', line):
                para = self.doc.add_paragraph(style=self._get_style("List Number"))
                self._apply_inline(para, re.sub(r'^\d+\.\s+', '', line))
                i += 1
                continue

            # Обычный абзац: собираем подряд идущие строки
            para_lines = [line]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                nxt_s = nxt.strip()
                # Look-ahead: текущая строка похожа на начало MD-таблицы,
                # если за ней идёт строка-разделитель
                is_md_table_start = (
                    nxt_s.startswith('|')
                    and i + 1 < len(lines)
                    and is_md_table_separator(lines[i + 1])
                )
                if (not nxt_s
                        or RE_HTML_COMMENT.match(nxt)
                        or re.match(r'^#{1,4}\s', nxt)
                        or nxt_s.startswith('$$')
                        or r'\begin{tabular}' in nxt
                        or is_md_table_start
                        or re.match(r'^[-*]\s', nxt)
                        or re.match(r'^\d+\.\s', nxt)
                        or nxt_s == '[IMAGE_REGION]'
                        or nxt_s == '---'
                        or 'page-break' in nxt_s):
                    break
                para_lines.append(nxt)
                i += 1
            para = self.doc.add_paragraph(style=self._get_style(self.BODY_STYLE))
            self._apply_inline(para, ' '.join(para_lines))
            self.stats["paragraphs"] += 1

    def _add_block_formula(self, latex: str):
        if not latex.strip():
            return
        para = self.doc.add_paragraph(style=self._get_style(self.BODY_STYLE))
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        self.stats["formulas_found"] += 1
        self._formula_idx += 1
        ok = insert_formula(para, latex, self._formula_idx,
                            self.tmp_dir, inline=False)
        if ok:
            self.stats["formulas_inserted"] += 1
        else:
            self.stats["formulas_as_text"] += 1

    # ── Финал ───────────────────────────────────────────────

    def save(self, output_path: str):
        self.doc.save(output_path)

    def print_stats(self):
        s = self.stats
        print("\n── Метрика сборки ──────────────────────")
        print(f"  Заголовки:              {s['headings']}")
        print(f"  Абзацы:                 {s['paragraphs']}")
        print(f"  Таблицы:                {s['tables']}")
        print(f"  Формул найдено:         {s['formulas_found']}")
        print(f"    └ как картинка/OMML:  {s['formulas_inserted']}")
        print(f"    └ как fallback-текст: {s['formulas_as_text']}")
        print(f"  Картинок найдено:       {s['images_found']}")
        print(f"    └ вставлено:          {s['images_inserted']}")
        print(f"  [IMAGE_REGION] заглушек:{s['image_regions_skipped']}")
        print(f"  HTML-комментариев убрано:{s['html_comments_skipped']}")
        print(f"  Разрывов страниц:       {s['page_breaks']}")

        if s['formulas_found'] > 0:
            rate = s['formulas_inserted'] / s['formulas_found']
            print(f"\n  Качество формул:        {rate:.0%}")
        print("────────────────────────────────────────")


# ─────────────────────────────────────────────────────────────
# 6. CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Конвертирует Markdown → DOCX по шаблону (ГОСТ)"
    )
    parser.add_argument("input_md",  help="Путь к .md файлу")
    parser.add_argument("template",  help="Путь к шаблону .docx")
    parser.add_argument("output",    help="Путь для результата .docx")
    parser.add_argument("--images",  default=".",
                        help="Папка с картинками (по умолчанию: рядом с md)")
    args = parser.parse_args()

    md_path = Path(args.input_md)
    if not md_path.exists():
        print(f"❌ Файл не найден: {md_path}")
        sys.exit(1)

    images_dir = args.images if args.images != "." else str(md_path.parent)

    print(f"📄 Вход:    {md_path}")
    print(f"📋 Шаблон:  {args.template}")
    print(f"🖼  Картинки:{images_dir}")

    converter = MdToDocxConverter(template_path=args.template,
                                  images_dir=images_dir)
    converter.convert(md_path.read_text(encoding="utf-8"))
    converter.save(args.output)
    print(f"\n✅ Сохранено: {args.output}")
    converter.print_stats()


if __name__ == "__main__":
    main()
