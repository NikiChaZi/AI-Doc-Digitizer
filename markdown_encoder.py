import os
import re
import uuid
import time
import threading
import concurrent.futures
from typing import TypedDict, List, Dict
from dotenv import load_dotenv

from langgraph.graph import StateGraph, END

from google import genai
from google.genai import types

# Загружаем ключи из .env
load_dotenv()

# Инициализируем чистый клиент Google
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# Конфигурация с отключением цензуры
def get_gemini_config(system_prompt: str):
    return types.GenerateContentConfig(
        temperature=0.0, # Ставим 0.0 для максимальной детерминированности (нам не нужен креатив)
        system_instruction=system_prompt,
        safety_settings=[
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
        ]
    )

api_lock = threading.Lock()
last_api_call_time = 0.0

def wait_for_api():
    """Выстраивает потоки в очередь. Гарантирует минимум 5 секунд между любыми запросами к API."""
    global last_api_call_time
    with api_lock:
        now = time.time()
        elapsed = now - last_api_call_time
        if elapsed < 5.0:
            time.sleep(5.0 - elapsed)
        last_api_call_time = time.time()

# ==========================================
# ОПРЕДЕЛЯЕМ СОСТОЯНИЕ (STATE)
# ==========================================
class DocumentState(TypedDict):
    original_text: str
    chunks: List[str]
    processed_chunks: List[str]
    chunk_feedbacks: Dict[int, str]
    current_text: str
    placeholders: dict
    iterations: int
    errors: List[str]

# ==========================================
# УЗЛЫ (NODES) - НАШИ АГЕНТЫ
# ==========================================

def preprocessor_node(state: DocumentState):
    print("\n" + "="*50)
    print("🛠 [1/4 Preprocessor] Запуск...")

    text = state["original_text"]
    placeholders = {}

    # 🛡️ Ищем только теги из букв, цифр и подчеркиваний (защита от захвата [0, 1])
    tags = re.findall(r'\[[A-Za-z0-9_]+\]', text)
    print(f"   🔍 Найдено тегов для защиты: {len(tags)}")

    for tag in tags:
        uid = f"__TAG_{uuid.uuid4().hex[:8]}__"
        placeholders[uid] = tag
        text = text.replace(tag, uid)

    print("   ✅ Теги успешно заменены на UUID.")

    if "<!-- НАЧАЛО СТРАНИЦЫ:" in text:
        raw_chunks = re.split(r'(?=<!-- НАЧАЛО СТРАНИЦЫ:)', text)
        chunks = [c.strip() for c in raw_chunks if c.strip()]
    else:
        paragraphs = text.split('\n\n')
        chunks = []
        current_chunk = ""
        for p in paragraphs:
            if len(current_chunk) + len(p) > 4000 and current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = p
            else:
                current_chunk += "\n\n" + p if current_chunk else p
        if current_chunk:
            chunks.append(current_chunk.strip())

    print(f"   ✂️ Документ разбит на {len(chunks)} частей (чанков).")

    # Инициализируем пустой список для обработанных чанков
    processed_chunks = [""] * len(chunks)

    return {
        "chunks": chunks,
        "processed_chunks": processed_chunks,
        "placeholders": placeholders,
        "chunk_feedbacks": {},
        "iterations": 0
    }

# ==========================================
# 2. УНИВЕРСАЛЬНЫЙ РЕДАКТОР (С ex.map)
# ==========================================

def editor_node(state: DocumentState):
    print("\n" + "="*50)
    iterations = state.get("iterations", 0)
    print(f"✍️ [2/4 UniversalEditor] Запуск (Итерация {iterations})...")

    chunks = state["chunks"]
    processed_chunks = state["processed_chunks"]
    feedbacks = state.get("chunk_feedbacks", {})

    BASE_PROMPT = r"""Ты — элитный AI-движок для постобработки OCR-документов.
    Твоя задача — взять "грязный" распознанный текст и превратить его в идеальный, чистый Markdown-документ с правильным LaTeX.

    ТВОЙ АЛГОРИТМ ДЕЙСТВИЙ:
    1. ОРФОГРАФИЯ И МУСОР: Исправь типичные ошибки сканирования (слипшиеся слова, '0' вместо 'O', '1' вместо 'l', 'експеримент' -> 'эксперимент').
       ВАЖНО: Ты технический корректор, а не писатель! КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО перефразировать текст, менять стиль, сокращать или добавлять от себя слова. Текст должен остаться оригинальным (слово в слово), но без опечаток.

    2. МАТЕМАТИКА (LaTeX): Найди все формулы (внутри $...$, $$...$$, \[...\], \begin{...}).
       - Исправь сломанный синтаксис: добавь потерянные слэши ('alpha' -> '\alpha'), закрой скобки, восстанови индексы ('x 1' -> 'x_1').
       - Убедись, что все окружения закрыты (на каждый \begin есть свой \end).
       - Обрати внимание на скобочки, соотноси размер скобок с собдержимым, если там просто уравнение, то ставь обычную, если интеграл или система - ставь большую в соответствии с математическими правилами.
       - ВАЖНО: Не меняй математический смысл и переменные! Если написано x+y, не делай y+x.

    3. ТАБЛИЦЫ: Если видишь неструктурированные данные, похожие на таблицу (колонки цифр/слов) — сверстай их в красивую Markdown-таблицу (| Колонна 1 | Колонна 2 |).
       - КРИТИЧЕСКИЙ КОСТЫЛЬ: Если внутри таблицы есть математический модуль или вертикальная черта (например, |x|), ты ОБЯЗАН заменить символ `|` на `\vert` (т.е. $\vert x \vert$). Иначе символ `|` сломает Markdown-разметку таблицы!
       - ВАЖНО: Не добавляй лишних строк или столбцов! Таблица должна точно соответствовать исходным данным, просто красиво отформатированная.
       - Если уже есть Markdown-таблица, проверь ее на синтаксис и исправь если нужно, не меняя данных.

    4. СТРУКТУРА И ЗАЩИТА:
       - Сохраняй оригинальное деление на абзацы. Не склеивай текст в один кирпич. Если нет двойного переноса строки, добавь его между абзацами. Если абзац слишком длинный (более 300 слов), попробуй найти логическое место для разбиения (например, перед новым пунктом или формулой).
       - Выдели явные заголовки через #, ##, ### (например, если абзац короткий и написан заглавными буквами, это может быть заголовок).
       - ВАЖНО: В тексте есть теги вида __TAG_a1b2c3__. Это маркеры картинок. Ты ОБЯЗАН оставить их на тех же местах. Удаление тега — это провал задачи.

    ФОРМАТ ВЫВОДА:
    ВЕРНИ ТОЛЬКО ИТОГОВЫЙ ТЕКСТ.
    Никаких "Вот исправленный текст:", "Я нашел таблицу" или других комментариев. Твой ответ пойдет напрямую в итоговый файл. Любое твое лишнее слово сломает документ. Если нет ошибок, просто верни текст без изменений. Если есть ошибки, верни исправленный текст, но НИ В КОЕМ СЛУЧАЕ не добавляй никаких комментариев или пояснений.
    """

    # Функция для обработки ОДНОГО чанка
    # Функция для обработки ОДНОГО чанка
    def process_one_editor(args):
        idx, orig_text, current_proc_text, feedback = args

        if current_proc_text and not feedback:
            print(f"   ⏩ [Чанк {idx+1}] Ошибок нет, пропускаем вызов LLM!")
            return current_proc_text

        print(f"   ⏳ [Чанк {idx+1}] Отправка запроса...")

        chunk_prompt = BASE_PROMPT
        if feedback:
            chunk_prompt += f"\n\n🚨 ВНИМАНИЕ! QA-АГЕНТ НАШЕЛ ОШИБКИ В ТВОЕМ ПРОШЛОМ ОТВЕТЕ:\n{feedback}\nИСПРАВЬ ИХ!"

        max_retries = 5
        for attempt in range(max_retries):
            try:
                wait_for_api()

                config = get_gemini_config(chunk_prompt)
                response = client.models.generate_content(
                    model='gemini-3.1-flash-lite',
                    contents=orig_text,
                    config=config
                )

                result_text = response.text
                result_text = re.sub(r'^(Вот исправленный текст:|Исправленный текст:)\s*', '', result_text, flags=re.IGNORECASE).strip()

                print(f"   ✅ [Чанк {idx+1}] Ответ успешно получен!")
                return result_text

            except Exception as e:
                error_str = str(e)

                print(f"\n🚨 [ДИАГНОСТИКА ГУГЛА] Чанк {idx+1} упал. Причина:\n{error_str}\n")

                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "Quota" in error_str:
                    sleep_time = 60 + (idx * 5)
                    print(f"   ⚠️ [Чанк {idx+1}] Лимит API (429). Ждем {sleep_time} сек... (Попытка {attempt+1}/{max_retries})")
                    time.sleep(sleep_time)
                else:
                    print(f"   ⚠️ [Чанк {idx+1}] Ошибка API ({e}). Попытка {attempt+1}/{max_retries}...")
                    time.sleep(5)
        else:
            # 🚨 Сработает, если исчерпаны все попытки
            print(f"   ❌ [Чанк {idx+1}] Не удалось получить ответ после {max_retries} попыток. Оставляем как было.")
            return current_proc_text if current_proc_text else orig_text

    # Подготавливаем аргументы для каждого потока
    args_list = [
        (i, chunks[i], processed_chunks[i], feedbacks.get(i, ""))
        for i in range(len(chunks))
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        new_processed_chunks = list(ex.map(process_one_editor, args_list))

    return {"processed_chunks": new_processed_chunks}

# ==========================================
# 3. QA-КРИТИК (С точным указанием мест ошибок)
# ==========================================

def qa_node(state: DocumentState):
    print("\n" + "="*50)
    iterations = state.get("iterations", 0) + 1
    print(f"🕵️‍♂️ [3/4 QAAgent] Запуск (Итерация {iterations})...")

    original_chunks = state["chunks"]
    processed_chunks = state["processed_chunks"]

    QA_PROMPT = r"""Ты — строгий QA-аудитор. Твоя задача — посимвольно и посмыслово сравнить ОРИГИНАЛЬНЫЙ текст (с OCR) и ОБРАБОТАННЫЙ текст (от Редактора).

    ТВОИ ПРАВИЛА (Ищи только КРИТИЧЕСКИЕ ошибки):
    1. ПОТЕРЯ ДАННЫХ: Проверь, не удалил ли Редактор целые абзацы, предложения, списки или формулы. Текст должен совпадать по структуре и объему.
    2. ОТСЕБЯТИНА (Галлюцинации): Проверь, не добавил ли Редактор свои комментарии (например, "Вот исправленный текст:", "Текст без таблиц:", "Готово").
    3. ИСКАЖЕНИЕ: Проверь, не перефразировал ли он текст своими словами.
    4. СИНТАКСИС: Проверь, не сломал ли он LaTeX-синтаксис (например, не закрыл формулу, не удалил слэш, не испортил таблицу).

    ФОРМАТ ОТВЕТА:
    - Если всё идеально, верни ровно одно слово: OK
    - Если есть ошибки, перечисли их МАКСИМАЛЬНО КОНКРЕТНО с цитатами и объяснениями, чтобы Редактор понял, где ошибся и в чем.

    ПРИМЕР ХОРОШЕГО ОТЧЕТА ОБ ОШИБКЕ:
    - УДАЛЕНО: Потерян абзац, начинающийся со слов "Таким образом, функция выигрыша..."
    - ДОБАВЛЕНО: В начале текста есть лишняя фраза "Вот ваш текст:"
    - ИСКАЖЕНО: Формула E=mc^2 заменена на E=mc^3.
    """

    def process_one_qa(args):
        idx, orig_text, proc_text = args
        chunk_errors = []
        proc_lines = proc_text.split('\n')

        # 🛠 ТУЛЗА 1: Проверка защитных тегов (С указанием контекста)
        tags_in_orig = re.findall(r'__TAG_[a-f0-9]+__', orig_text)
        for tag in tags_in_orig:
            if tag not in proc_text:
                # Ищем контекст в оригинале, чтобы подсказать Редактору, куда вернуть тег
                match = re.search(r'(.{0,30})' + re.escape(tag) + r'(.{0,30})', orig_text, re.DOTALL)
                context_str = match.group(0).replace('\n', ' ') if match else ""
                chunk_errors.append(f"СИНТАКСИС: Ты удалил тег картинки {tag}! В оригинале он стоял здесь: «...{context_str}...». Верни его на место!")

        # 🛠 ТУЛЗА 2: Проверка баланса LaTeX $ (С поиском строки)
        clean_text = proc_text.replace(r'\$', '') # Игнорируем экранированные
        if clean_text.count('$') % 2 != 0:
            suspicious_lines = []
            for i, line in enumerate(proc_lines):
                if line.replace(r'\$', '').count('$') % 2 != 0:
                    suspicious_lines.append(f"Строка {i+1}: «{line[:60]}...»")

            err_msg = "СИНТАКСИС: Нечетное количество знаков $ (формула не закрыта!)."
            if suspicious_lines:
                err_msg += f" Ошибка находится в одной из этих строк:\n  - " + "\n  - ".join(suspicious_lines)
            chunk_errors.append(err_msg)

        # 🛠 ТУЛЗА 3: Проверка окружений LaTeX (С указанием конкретного окружения)
        begins = re.findall(r'\\begin\{([^}]+)\}', proc_text)
        ends = re.findall(r'\\end\{([^}]+)\}', proc_text)
        env_counts = {}
        for b in begins: env_counts[b] = env_counts.get(b, 0) + 1
        for e in ends: env_counts[e] = env_counts.get(e, 0) - 1

        for env, balance in env_counts.items():
            if balance > 0:
                chunk_errors.append(f"СИНТАКСИС: Окружение \\begin{{{env}}} не закрыто! Не хватает \\end{{{env}}}.")
            elif balance < 0:
                chunk_errors.append(f"СИНТАКСИС: Найден лишний \\end{{{env}}} без открывающего \\begin{{{env}}}!")

        # 🛠 ТУЛЗА 4: Проверка фигурных скобок (С поиском места обрыва)
        clean_braces = proc_text.replace(r'\{', '').replace(r'\}', '')
        open_b = clean_braces.count('{')
        close_b = clean_braces.count('}')
        if open_b != close_b:
            err_msg = f"СИНТАКСИС: Несовпадение фигурных скобок! Открывающих '{{': {open_b}, закрывающих '}}': {close_b}."
            # Пытаемся найти строку, где баланс уходит в минус (закрыли лишнюю скобку)
            balance = 0
            for i, line in enumerate(proc_lines):
                cl = line.replace(r'\{', '').replace(r'\}', '')
                balance += cl.count('{') - cl.count('}')
                if balance < 0:
                    err_msg += f"\n  -> Баланс скобок ушел в минус (лишняя '}}') на строке {i+1}: «{line[:60]}...»"
                    break
            chunk_errors.append(err_msg)

        # 🧠 ТУЛЗА 5: Смысловая проверка через LLM
        qa_content = f"ОРИГИНАЛ:\n{orig_text}\n\nОБРАБОТКА:\n{proc_text}"
        config = get_gemini_config(QA_PROMPT)

        max_qa_retries = 4
        for attempt in range(max_qa_retries):
            try:
                wait_for_api()

                response = client.models.generate_content(
                    model='gemini-3.1-flash-lite',
                    contents=qa_content,
                    config=config
                )

                llm_feedback = response.text.strip()

                if llm_feedback.upper().replace('.', '').replace('*', '').strip() != "OK":
                    chunk_errors.append(f"СМЫСЛОВЫЕ ОШИБКИ (От QA-Аудитора):\n{llm_feedback}")

                break # Успешно получили ответ, выходим из цикла ретраев

            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "Quota" in error_str:
                    # 🛡️ JITTER для QA
                    sleep_time = 60 + (idx * 5)
                    print(f"   ⚠️ [QA Чанк {idx+1}] Лимит API (429). Ждем {sleep_time} сек... (Попытка {attempt+1}/{max_qa_retries})")
                    time.sleep(sleep_time)
                else:
                    print(f"   ⚠️ [QA Чанк {idx+1}] Ошибка API при проверке: {e}. Попытка {attempt+1}/{max_qa_retries}")
                    time.sleep(5)
        else:
            # 🚨 Сработает, если цикл завершился без break (API мертво)
            err_msg = "КРИТИЧЕСКАЯ ОШИБКА API: QA-агент не смог проверить текст из-за лимитов Google."
            chunk_errors.append(err_msg)
            print(f"   ❌ [QA Чанк {idx+1}] Сдаюсь. Добавлена критическая ошибка.")

        if chunk_errors:
            print(f"   ❌ [Чанк {idx+1}] Найдены ошибки: {len(chunk_errors)}")
        else:
            print(f"   ✅ [Чанк {idx+1}] Проверка пройдена!")

        return idx, chunk_errors

    args_list = [(i, original_chunks[i], processed_chunks[i]) for i in range(len(original_chunks))]

    chunk_feedbacks = {}
    global_errors = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        qa_results = list(ex.map(process_one_qa, args_list))

    for idx, errors in qa_results:
        if errors:
            error_str = "\n".join(errors)
            chunk_feedbacks[idx] = error_str
            global_errors.append(f"Чанк {idx+1}:\n{error_str}")

    if global_errors:
        print(f"\n   ⚠️ Итог QA: Найдено ошибок в {len(chunk_feedbacks)} чанках. Отправляем полный баг-репорт Редактору.")
    else:
        print("\n   ✅ Итог QA: Документ идеален!")

    return {
        "chunk_feedbacks": chunk_feedbacks,
        "errors": global_errors,
        "iterations": iterations
    }

def postprocessor_node(state: DocumentState):
    print("\n" + "="*50)
    print("✨ [4/4 Postprocessor] Запуск...")

    # Склеиваем итоговый текст из ОБРАБОТАННЫХ чанков
    full_text = "\n\n".join(state["processed_chunks"])
    placeholders = state["placeholders"]

    print(f"   🔄 Восстанавливаем {len(placeholders)} оригинальных тегов...")
    for uid, original_tag in placeholders.items():
        full_text = full_text.replace(uid, original_tag)

    print("   ✅ Теги восстановлены.")
    return {"current_text": full_text}


# ==========================================
# 4. МАРШРУТИЗАЦИЯ И СБОРКА ГРАФА
# ==========================================

def qa_router(state: DocumentState):
    print("\n🔀 [Router] Принимаю решение о дальнейшем маршруте...")

    errors = state.get("errors", [])
    iterations = state.get("iterations", 0)

    if len(errors) > 0 and iterations < 3:
        print("   🔙 Маршрут: Возврат на доработку (editor_node)!")
        return "editor"
    else:
        if iterations >= 3:
            print("   ⚠️ Маршрут: Лимит попыток исчерпан. Идем на финиш.")
        else:
            print("   ➡️ Маршрут: Всё отлично. Идем на постобработку.")
        return "postprocess"

def build_ocr_graph():
    workflow = StateGraph(DocumentState)

    workflow.add_node("preprocess", preprocessor_node)
    workflow.add_node("editor", editor_node)
    workflow.add_node("qa", qa_node)
    workflow.add_node("postprocess", postprocessor_node)

    workflow.set_entry_point("preprocess")
    workflow.add_edge("preprocess", "editor")
    workflow.add_edge("editor", "qa")

    # Условный переход: QA решает, вернуть ли чанки Редактору или закончить
    workflow.add_conditional_edges(
        "qa",
        qa_router,
        {
            "editor": "editor",
            "postprocess": "postprocess"
        }
    )

    workflow.add_edge("postprocess", END)
    return workflow.compile()


# ==========================================
# 5. ТЕСТОВЫЙ ЗАПУСК И СОХРАНЕНИЕ
# ==========================================

if __name__ == "__main__":
    app = build_ocr_graph()

    input_filename = "output_result.md"
    output_filename = "output_document.md"

    print(f"📥 Читаем текст из файла: {input_filename}...")
    with open(input_filename, "r", encoding="utf-8") as f:
        raw_ocr_text = f.read()

    initial_state = {
        "original_text": raw_ocr_text,
        "chunks": [],
        "processed_chunks": [],
        "chunk_feedbacks": {},
        "current_text": "",
        "placeholders": {},
        "errors": [],
        "iterations": 0
    }

    print("\n🚀 СТАРТ ПАЙПЛАЙНА ОЦИФРОВКИ")
    print("="*50)

    # Запускаем граф
    final_state = app.invoke(initial_state)
    final_text = final_state["current_text"]

    print("\n\n" + "="*50)
    print("🏁 ФИНАЛЬНЫЙ РЕЗУЛЬТАТ:\n")
    print(final_text[:500] + "\n... (текст обрезан для превью)")

    # Сохраняем в Markdown
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(final_text)

    print("\n" + "="*50)
    print(f"💾 Готово! Чистый Markdown сохранен в файл: {output_filename}")
