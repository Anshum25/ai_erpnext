from .provider import LLMProvider
import json
from google import genai
from google.genai import types

class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
    
    def extract_document_data(self, file_content: bytes, file_type: str, prompt: str) -> dict:
        import time
        mime_type = "application/pdf" if file_type.lower() == "pdf" else f"image/{file_type.lower()}"
        document = types.Part.from_bytes(
            data=file_content,
            mime_type=mime_type,
        )
        
        for attempt in range(4):
            try:
                response = self.client.models.generate_content(
                    model='gemini-3.6-flash',
                    contents=[document, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    )
                )
                return json.loads(response.text)
            except Exception as e:
                if "503" in str(e) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise e

