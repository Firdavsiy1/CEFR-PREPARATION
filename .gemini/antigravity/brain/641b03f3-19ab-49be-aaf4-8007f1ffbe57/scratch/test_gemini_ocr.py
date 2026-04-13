import json
import os
from pathlib import Path
from PIL import Image
from google import genai
from google.genai import types as T

def run_test():
    image_path = Path('materials/Test 1/Listening/Part 1/questions.jpg')
    with open(image_path, 'rb') as fh:
        image_bytes = fh.read()
    
    image_part = T.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")

    prompt = """Act as an expert English CEFR exam parser.
Analyze this exam page for Part 1 of a listening test. The question type is 'multiple_choice'.

Ignore all page numbers and extraneous headers (like "TEST 11" or "PART 1").
Extract the main instructions block.
Extract all questions.
- For multiple_choice: extract the question text and all choices (A, B, C …).
- For fill_blank: extract the sentence context with the blank rendered as '_____'.
- For map_label: extract the location name.

Return STRICTLY a JSON object with this exact structure, nothing else
(no markdown fences, no preamble):
{
    "instructions": "Instructions text",
    "questions": [
        {
            "number": 1,
            "text": "Question text or sentence with blank",
            "choices": [
                {"label": "A", "text": "Choice A text"},
                {"label": "B", "text": "Choice B text"}
            ]
        }
    ]
}
If a question has no choices, return an empty list [] for "choices"."""

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        if os.path.exists(adc_path):
            with open(adc_path) as f:
                project_id = json.load(f).get("quota_project_id")
                
    client = genai.Client(vertexai=True, project=project_id, location="global")
    config = T.GenerateContentConfig(
        thinking_config=T.ThinkingConfig(thinking_level="MINIMAL"),
        response_mime_type="application/json",
    )
    
    print("Calling Gemini...")
    response = client.models.generate_content(
        model="gemini-3-flash-preview", 
        contents=[image_part, prompt],
        config=config
    )
    print("----- RESPONSE (RAW) -----")
    print(response.text)
    
if __name__ == '__main__':
    run_test()
