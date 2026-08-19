import io
import json
import time
from config import gemini_client
from google.genai import types
from PIL import Image


def extract_contact_info(image_bytes: bytes, max_retries: int = 3) -> dict:
    """Extracts structured fields from business card using Gemini Vision with Russian-to-English translation & retry logic."""
    image = Image.open(io.BytesIO(image_bytes))
    prompt = """
    Extract contact information from this business card image.
    Return ONLY a JSON object with these exact keys:
    {
      "first_name": "",
      "last_name": "",
      "company": "",
      "job_title": "",
      "country": "",
      "phone": "",
      "email": "",
      "website": "",
      "telegram": "",
      "address": ""
    }

    CRITICAL RULES:
    1. LANGUAGE & TRANSLATION (RUSSIAN / CYRILLIC TO ENGLISH):
       - All extracted text MUST be translated or transliterated into English / Latin characters.
       - Person Names in Russian: Transliterate to standard Latin/English spelling (e.g., "Иван Иванов" -> "Ivan Ivanov", "Дмитрий Карпов" -> "Dmitry Karpov").
       - Job Titles in Russian: Translate accurately to English (e.g., "Генеральный директор" -> "General Manager / CEO", "Руководитель отдела" -> "Head of Department").
       - Companies / Institutions in Russian: Translate or standardly transliterate (e.g., "Посольство Болгарии" -> "Embassy of Bulgaria").
       - Countries / Cities / Addresses: Translate into English (e.g., "Узбекистан, Ташкент" -> "Tashkent, Uzbekistan").

    2. BUSINESS / STORE CARDS:
       - If there is NO individual person's name on the card (e.g. gym, spa, restaurant, clinic, hotel, salon), leave first_name/last_name empty and extract the business/brand name into "company" (infer from logo, text, or website domain).

    3. PHONE NUMBERS:
       - Extract only ONE primary mobile or direct phone number with international country code.

    4. LINKS & HANDLES:
       - WEBSITE: Extract full website address (e.g., "https://scultura.uz/").
       - TELEGRAM: Extract telegram username/handle (e.g., "@fitness_spa_uz").

    5. DIPLOMATIC REPRESENTATION:
       - If the card represents an Embassy/Consulate, "country" MUST be the represented nation (e.g., "Bulgaria", "Italy").

    6. Missing fields should be empty strings.
    """

    for attempt in range(max_retries):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[image, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            err_msg = str(e)
            is_transient = any(
                code in err_msg for code in ["503", "UNAVAILABLE", "429", "500"]
            )
            if is_transient and attempt < max_retries - 1:
                time.sleep((attempt + 1) * 3)
                continue
            raise e
