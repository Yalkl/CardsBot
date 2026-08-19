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
      "country": "",
      "company": "",
      "job_title": "",
      "phone": "",
      "email": "",
      "address": ""
    }

    CRITICAL RULES:
    1. PHONE NUMBERS: Extract only ONE primary mobile or direct phone number with international country code.
    2. DIPLOMATIC REPRESENTATION: If the card represents an Embassy/Consulate (e.g. "Embassy of Italy in Tashkent"), country MUST be the represented country ("Italy").
    3. Missing fields should be empty strings.
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