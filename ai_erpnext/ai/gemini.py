from .provider import LLMProvider
from .errors import AIProviderError, classify, log_ai_event
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
        
        started = time.perf_counter()
        meta = {"provider": "Gemini", "model": "gemini-3.6-flash"}
        for attempt in range(4):  # bounded: at most 3 retries, only for 503
            try:
                response = self.client.models.generate_content(
                    model='gemini-3.6-flash',
                    contents=[document, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    )
                )
                break
            except Exception as e:
                if getattr(e, "code", None) == 503 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                error = classify(e)
                log_ai_event(**meta, status="error", category=error.category, http_status=error.status_code,
                    elapsed_ms=round((time.perf_counter() - started) * 1000), retries=attempt)
                raise error from None
        try:
            data = json.loads(response.text)
        except (TypeError, ValueError):
            data = None
        if not isinstance(data, dict):
            log_ai_event(**meta, status="error", category="invalid_response", retries=attempt)
            raise AIProviderError("invalid_response")
        log_ai_event(**meta, status="ok", elapsed_ms=round((time.perf_counter() - started) * 1000), retries=attempt)
        return data

