import io
import json
import time
from config import gemini_client
from google.genai import types
from PIL import Image


def extract_contact_info(image_bytes: bytes, max_retries: int = 3) -> dict:
    """Extracts structured fields from business card using Gemini Vision with retry logic."""
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
    1. BUSINESS / STORE CARDS: If there is NO individual person's name on the card (e.g. gym, spa, restaurant, clinic, hotel), leave first_name/last_name empty and extract the business/brand name into "company" (infer from text, logo, or website domain like "Scultura").
    2. PHONE NUMBERS: Extract only ONE primary mobile or direct phone number with international country code.
    3. WEBSITE: Extract full website address (e.g., "https://scultura.uz/").
    4. TELEGRAM: Extract telegram username/handle (e.g., "@fitness_spa_uz").
    5. DIPLOMATIC REPRESENTATION: If Embassy/Consulate, country MUST be the represented country.
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
