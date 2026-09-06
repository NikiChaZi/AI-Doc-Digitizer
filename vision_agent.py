import os
import io
import time
import asyncio
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()

# Получаем API ключ из переменных окружения
API_KEY = os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    raise ValueError("Ключ API не найден. Убедитесь, что файл .env содержит переменную GOOGLE_API_KEY!")

# Отключаем цензуру, чтобы не блокировались научные/медицинские схемы и тексты
SAFETY_SETTINGS = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
]

class VisionAgent:
    def __init__(self, api_key: str):
        self.api_key = api_key
        # Настройка клиента Google Gemini
        self.client = genai.Client(api_key=self.api_key)
        
        # Системный промпт для Vision-агента, отвечающий нашим требованиям
        self.system_prompt = """
        Ты — агент оцифровки рукописных документов. Твоя задача — извлечь весь текст из предоставленного изображения, строго соблюдая следующие правила:
        1. Сохраняй исходную последовательность чтения (слева направо, сверху вниз).
        2. НЕ ПИШИ НИКАКИХ КОММЕНТАРИЕВ ОТ СЕБЯ ТОЛЬКО САМ ТЕКСТ, КОТОРЫЙ ЕСТЬ НА КАРТИНКЕ.
        3. Формулы должны быть записаны в формате LaTeX. Используй стандартный синтаксис (один $ для внутристрочных формул, два $$ для выносных). Пиши только валидный LaTeX-код, без лишних символов доллара или кавычек.
        4. Таблицы переводи в формат Markdown.
        5. Если текст зачеркнут — полностью игнорируй его. Если есть исправление со стрелкой, вставляй текст туда, куда указывает стрелка.
        6. Если на изображении есть рисунки, графики или чертежи, НЕ пытайся их описывать. Просто ставь маркер [IMAGE_REGION] на том месте, где в тексте должен быть рисунок.
        """
        
        # Конфигурация вызова
        self.config = types.GenerateContentConfig(
            system_instruction=self.system_prompt,
            safety_settings=SAFETY_SETTINGS
        )

    async def extract_text_and_structures(self, image_path: str) -> str:
        """
        Основной метод извлечения текста и структур из картинки.
        Отправляет картинку в Vision-модель Gemini с защитой от падений API асинхронно.
        """
        print(f"Начало обработки изображения: {os.path.basename(image_path)}...")
        
        img = Image.open(image_path)
        
        # Бесконечный цикл: при ошибке загрузки (особенно 429) ждем и пробуем снова, пока не добьемся успеха
        attempt = 0
        while True:
            try:
                # Вызов Google Gemini API
                response = await self.client.aio.models.generate_content(
                    model='gemini-3.1-flash-lite',
                    contents=["Оцифруй этот документ согласно инструкциям.", img],
                    config=self.config
                )
                print(f"✅ Успешно обработано: {os.path.basename(image_path)}")
                return response.text
                
            except Exception as e:
                attempt += 1
                error_str = str(e)
                
                # Если ошибка связана с лимитом запросов (429)
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "Quota" in error_str:
                    # Увеличиваем время ожидания при повторных попытках (до 60 секунд)
                    wait_time = min(10 + attempt * 5, 60)
                    print(f"   ⏳ Лимит API (429) для {os.path.basename(image_path)}. Ждем {wait_time}с и пробуем снова...")
                else:
                    # Для прочих ошибок тоже даем задержку
                    wait_time = min(2 ** attempt, 30)
                    print(f"   ⚠️ Ошибка API для {os.path.basename(image_path)}: {e}. Ждем {wait_time}с и пробуем снова...")
                
                await asyncio.sleep(wait_time)

    async def process_document(self, image_path: str):
        """
        Полный цикл обработки: 
        1. Получение текста от LLM.
        2. TODO (Архитектура): вырезание картинок (Crop) из областей, которые не являются текстом.
        """
        raw_markdown = await self.extract_text_and_structures(image_path)
        
        return raw_markdown

async def main():
    agent = VisionAgent(api_key=API_KEY)
    
    # Определяем абсолютный путь к папке скрипта (Read_pdf)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Директории для входных и выходных данных (привязываем к папке скрипта)
    input_dir = os.path.join(script_dir, "In_Pics")
    output_filepath = os.path.join(script_dir, "output_result.md")
    
    # Создаем папку для входных картинок, если её нет
    os.makedirs(input_dir, exist_ok=True)
    
    # Ищем все картинки в папке
    valid_extensions = ('.png', '.jpg', '.jpeg')
    image_files = [f for f in os.listdir(input_dir) if f.lower().endswith(valid_extensions)]
    
    if not image_files:
        print(f"Папка {input_dir} пуста или не содержит картинок. Положите туда сканы страниц.")
    else:
        # Сортируем файлы, чтобы страницы шли по порядку следования
        image_files.sort()
        
        print(f"Найдено изображений для обработки: {len(image_files)}\n")
        print("Запуск асинхронной обработки...")
        
        # Запускаем обработку всех картинок одновременно (конкурентно)
        tasks = [agent.process_document(os.path.join(input_dir, img_name)) for img_name in image_files]
        results = await asyncio.gather(*tasks)
        
        # Открываем файл на запись один раз
        with open(output_filepath, "w", encoding="utf-8") as md_file:
            for i, (img_name, result) in enumerate(zip(image_files, results)):
                
                # Записываем результат
                md_file.write(f"<!-- НАЧАЛО СТРАНИЦЫ: {img_name} -->\n\n")
                md_file.write(result)
                md_file.write("\n\n<!-- КОНЕЦ СТРАНИЦЫ -->\n")
                
                # Добавляем разделитель и HTML-разрыв страницы, если это не последняя картинка
                if i < len(image_files) - 1:
                    md_file.write("\n\n---\n<div style=\"page-break-after: always;\"></div>\n\n")
                
        print(f"\n--- Готово! Все страницы обработаны и склеены в файл: {output_filepath} ---")

if __name__ == "__main__":
    asyncio.run(main())