"""
pipeline.py — Запуск всех 3 модулей по цепочке:
  1. vision_agent.py        — Изображения (In_Pics/*) → сырой Markdown
  2. markdown_encoder.py    — Чистка LaTeX / таблиц / опечаток через LangGraph
  3. converter.py           — Markdown → DOCX (ГОСТ-шаблон)

Использование:
  python pipeline.py
  python pipeline.py --skip-vision           # переиспользовать output_result.md
  python pipeline.py --skip-cleanup          # пропустить пост-обработку markdown
  python pipeline.py --template my.docx --output report.docx
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

# Артефакты пайплайна
RAW_MD   = ROOT / "output_result.md"      # выход vision_agent
CLEAN_MD = ROOT / "output_document.md"    # выход markdown_encoder


def banner(n: int, total: int, title: str) -> None:
    print(f"\n{'=' * 64}")
    print(f"  ШАГ {n}/{total}  ·  {title}")
    print(f"{'=' * 64}")


def run(cmd: list) -> None:
    """Подпроцесс с CWD = корень проекта и проверкой возврата."""
    printable = " ".join(str(c) for c in cmd)
    print(f"$ {printable}")
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pipeline: Pics → MD → cleaned MD → DOCX")
    ap.add_argument("--skip-vision", action="store_true",
                    help="Не перезапускать OCR, если output_result.md уже есть")
    ap.add_argument("--skip-cleanup", action="store_true",
                    help="Не запускать markdown_encoder, использовать сырой MD")
    ap.add_argument("--template", default=str(ROOT / "template.docx"),
                    help="Шаблон .docx с ГОСТ-стилями (по умолчанию template.docx рядом)")
    ap.add_argument("--output", default=str(ROOT / "final_document.docx"),
                    help="Путь к итоговому .docx")
    args = ap.parse_args()

    template = Path(args.template)
    final_docx = Path(args.output)

    # ── 1. VISION ───────────────────────────────────────────
    if args.skip_vision and RAW_MD.exists():
        banner(1, 3, f"Vision — ПРОПУСК (используем {RAW_MD.name})")
    else:
        banner(1, 3, "Vision: In_Pics/*.{png,jpg} → output_result.md")
        in_pics = ROOT / "In_Pics"
        if not in_pics.exists() or not any(in_pics.iterdir()):
            print(f"❌ Папка {in_pics} пуста или отсутствует. Положи туда сканы.")
            sys.exit(1)
        run([sys.executable, str(ROOT / "vision_agent_review.py")])
        if not RAW_MD.exists():
            print(f"❌ vision_agent не создал {RAW_MD.name}")
            sys.exit(1)

    # ── 2. CLEANUP (LangGraph) ──────────────────────────────
    if args.skip_cleanup:
        banner(2, 3, "Cleanup — ПРОПУСК, копируем сырой MD как чистовик")
        shutil.copy(RAW_MD, CLEAN_MD)
    else:
        banner(2, 3, "Markdown Encoder: LaTeX / таблицы / опечатки")
        # markdown_encoder.py читает input_ocr.md из CWD — подготавливаем
        run([sys.executable, str(ROOT / "markdown_encoder.py")])
        if not CLEAN_MD.exists():
            print(f"❌ markdown_encoder не создал {CLEAN_MD.name}")
            sys.exit(1)

    # ── 3. CONVERTER (MD → DOCX) ────────────────────────────
    banner(3, 3, f"Converter: {CLEAN_MD.name} → {final_docx.name}")
    if not template.exists():
        print(f"❌ Не найден шаблон: {template}")
        print("   Положи .docx-шаблон с настроенными ГОСТ-стилями рядом со скриптом")
        print("   или передай путь флагом --template")
        sys.exit(1)

    run([
        sys.executable, str(ROOT / "converter.py"),
        str(CLEAN_MD), str(template), str(final_docx),
        "--images", str(ROOT),
    ])

    print(f"\n✅ ГОТОВО → {final_docx}")
    print(f"   Промежуточные артефакты:")
    print(f"     • сырой OCR:    {RAW_MD.name}")
    print(f"     • очищенный MD: {CLEAN_MD.name}")


if __name__ == "__main__":
    main()
