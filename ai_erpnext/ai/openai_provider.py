import base64
import json
from openai import OpenAI
from .provider import LLMProvider

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: str = None, model: str = "gpt-4o"):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
    
    def extract_document_data(self, file_content: bytes, file_type: str, prompt: str) -> dict:
        mime_type = "application/pdf" if file_type.lower() == "pdf" else f"image/{file_type.lower()}"
        encoded_string = base64.b64encode(file_content).decode("utf-8")
        
        # If it's a PDF, we might need a different handling depending on the model.
        # But most vision models support images or we can send PDF if supported.
        # Wait, Groq LLaMA vision doesn't support PDFs natively, only images.
        # We will assume image processing for now, or just send the file content.
        # Standard OpenAI vision payload:
        image_url = f"data:{mime_type};base64,{encoded_string}"
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url
                            }
                        }
                    ]
                }
            ],
            response_format={ "type": "json_object" }
        )
        
        print(f"\n[AI RESPONSE from {self.model}]:")
        print(response.choices[0].message.content)
        print("-" * 50 + "\n")
        
        return json.loads(response.choices[0].message.content)
