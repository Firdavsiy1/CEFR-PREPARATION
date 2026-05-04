import json
import logging
import mimetypes
import os
import re
from io import BytesIO
from typing import Any

from django.conf import settings
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.getenv("GEMINI_READING_MODEL", "gemini-3-flash-preview")
GEMINI_LOCATION = os.getenv("GEMINI_LOCATION", "global")

def _build_gemini_client():
    from google import genai
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        if os.path.exists(adc_path):
            try:
                with open(adc_path) as f:
                    project_id = json.load(f).get("quota_project_id")
            except (OSError, json.JSONDecodeError):
                pass
    if project_id:
        return genai.Client(vertexai=True, project=project_id, location=GEMINI_LOCATION)
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        return genai.Client(api_key=api_key)
    raise RuntimeError("No Gemini credentials found for speaking service.")

def process_speaking_page(image_bytes: bytes) -> dict:
    """
    Recognize and extract speaking parts (1, 2, 3) from a single test page
    using Gemini 1.5 Flash/Pro. Returns a JSON structure for mentor validation.
    """
    try:
        from google.genai import types as genai_types
        client = _build_gemini_client()
    except Exception as exc:
        logger.warning(f"Could not build Gemini client: {exc}")
        return {"status": "error", "message": "Failed to initialize Gemini"}

    system_instruction = '''You are a precise CEFR English exam extractor.
I will pass you an image of a single CEFR Speaking test page. It usually has 3 or 4 parts (Part 1, Part 1.2, Part 2, Part 3).
Extract ALL parts into a JSON array of objects.

Follow this schema EXACTLY:
{
  "parts": [
    {
      "part_number": "1",
      "instructions": "Full instruction text for this part",
      "questions": [{"q_num": 1, "text": "Exact question text"}, {"q_num": 2, "text": "..."}]
    },
    {
      "part_number": "1.2",
      "instructions": "Full instruction text for part 1.2",
      "questions": [{"q_num": 1, "text": "..."}, ...]
    },
    {
      "part_number": "2",
      "instructions": "Describe the picture / answer questions...",
      "questions": [{"q_num": 1, "text": "..."}, ...]
    },
    {
      "part_number": "3",
      "instructions": "Discussion / Debate",
      "debate_table": {
        "topic": "The full statement/topic from the table header",
        "for_points": ["Point 1 for", "Point 2 for", ...],
        "against_points": ["Point 1 against", "Point 2 against", ...]
      },
      "questions": []
    }
  ]
}

Rules:
1. Extract EVERY question from EVERY part. Do NOT skip or omit any question.
2. The part_number must be a string. If a part is explicitly labeled 1.1, 1.2, or similar, use "1.1", "1.2". Otherwise use "1", "2", "3".
3. The questions array must contain ALL visible questions for each part, numbered sequentially.
4. Copy question text EXACTLY as printed — do not paraphrase or summarise.
5. If a part has bullet points or numbered items that are QUESTIONS (interrogative sentences or tasks like "Tell me about..."), each one is a separate question object.
6. Return cleanly formatted JSON only. No markdown fences, no extra text.
7. Include the full instructions text for each part (all sentences of the instruction block).
8. If there is no clear part number, infer from context: Part 1 = Interview about self (likes/dislikes/opinions), Part 1.2 = follow-up questions or short personal task, Part 2 = describe a picture/image, Part 3 = extended discussion / debate.
9. Never merge two different parts into one. If you see two distinct sections, create two separate part objects.

CRITICAL — Part 3 Debate Table Recognition:
10. Part 3 typically contains a TABLE or BOX with a debate topic/statement at the top, and two columns: FOR and AGAINST (or similar). Each column has bullet points with arguments/conditions.
11. These FOR/AGAINST bullet points are NOT questions. They are debate conditions/arguments that the student should discuss.
12. For Part 3 with a debate table: put the topic in "debate_table.topic", the for-arguments in "debate_table.for_points", the against-arguments in "debate_table.against_points", and leave "questions" as an EMPTY array [].
13. Only add items to "questions" if they are actual spoken questions the examiner asks (e.g. "Do you agree?", "What do you think?"). The debate table bullet points must go in "debate_table", never in "questions".

CRITICAL — Part 2 Image + Questions:
14. Part 2 usually shows a SINGLE image/photo followed by bullet-point questions about that image (e.g. "Tell me about a time...", "What made you...?"). Always extract these as questions.
15. Part 1.2 usually shows MULTIPLE images/photos with questions about what is shown. Always extract these as questions.'''

    content_parts = [
        genai_types.Part(text="Extract the speaking test parts from this image."),
        genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
    ]

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=genai_types.Content(parts=content_parts, role="user"),
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.05,
                response_mime_type="application/json",
                max_output_tokens=8192,
            )
        )
        
        raw_text = response.text
        if raw_text and raw_text.startswith("```"):
            raw_text = raw_text.split("```", 2)[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()
            
        data = json.loads(raw_text)
        return {
            "status": "success",
            "parts": data.get("parts", [])
        }
    except Exception as exc:
        logger.error(f"Gemini processing error: {exc}")
        return {"status": "error", "message": "Failed to parse image via Gemini"}



def generate_alt_text(image_bytes: bytes) -> str:
    """
    Provide an alt text description of a cropped image using Gemini.
    """
    # vertexai.init(project=settings.GCP_PROJECT, location=settings.GCP_LOCATION)
    # model = GenerativeModel("gemini-1.5-pro-vision")
    # response = model.generate_content(...)
    return "An AI-generated description of the cropped examiner image."

import os
import json
from google import genai
from google.genai import types as genai_types
from exams.models import SpeakingSubmission

GEMINI_MODEL = os.getenv("GEMINI_READING_MODEL", "gemini-3-flash-preview")

SPEAKING_EVAL_PROMPT = """
You are an expert CEFR/IELTS Speaking examiner evaluating a student's spoken response.

Each Speaking part has different difficulty expectations — calibrate your score accordingly:
- Part 1 (max 10 pts): Basic personal questions. Full marks = clear, fluent, accurate answers.
- Part 2 (max 15 pts): Structured monologue from a cue card. Full marks = extended discourse, varied vocabulary, coherent organisation.
- Part 3 (max 20 pts): Abstract two-way discussion. Full marks = sophisticated vocabulary, complex grammar, nuanced opinions.
- Part 4 (max 25 pts): Complex debate / extended discussion. Full marks = near-native lexical precision, advanced discourse markers.

Score from 0 to 10 (half-point increments allowed). This raw score will be scaled to the part's max points.
Evaluate: fluency & coherence, lexical resource, grammatical range & accuracy, pronunciation.

Provide feedback_i18n in three languages. KEEP English linguistic/CEFR terms in ALL languages
(e.g. "B2", "cohesion", "lexical range", "discourse markers" — do not translate these).

Output valid JSON only — no markdown fences:
{
    "transcript": "verbatim transcription of what the student said",
    "feedback_i18n": {
        "en": "Detailed English feedback with English terminology.",
        "ru": "Подробный отзыв на русском с сохранением английских терминов (B2, cohesion, lexical range и т.д.).",
        "uz": "O'zbek tilida batafsil fikr-mulohaza, ingliz atamalari saqlanadi (B2, cohesion, lexical range va b.)."
    },
    "estimated_level": "A1 or A2 or B1 or B2 or C1 or C2",
    "score": 7.5
}
"""

def evaluate_speaking_submission(submission: SpeakingSubmission) -> bool:
    from exams.speaking_services import _build_gemini_client, GEMINI_MODEL
    import json
    
    if not submission.audio_file:
        return False
        
    client = _build_gemini_client()
    if not client:
        return False
        
    try:
        audio_bytes = submission.audio_file.read()
        audio_part = genai_types.Part.from_bytes(data=audio_bytes, mime_type="audio/webm")
        
        part = submission.question.part
        prompt = f"""
{SPEAKING_EVAL_PROMPT}

Question context:
Part {part.part_number} (max {part.points_per_question:.0f} pts per question)
Instructions: {part.instructions}
Question: {submission.question.question_text}
"""
        
        config = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        )
        
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[audio_part, prompt],
            config=config,
        )
        
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()
        elif raw_text.startswith("```"):
            raw_text = raw_text.replace("```", "").strip()
            
        data = json.loads(raw_text)
        
        submission.transcript = data.get("transcript", "No transcript could be generated.")
        # Multilingual feedback: prefer feedback_i18n, fall back to feedback_text
        feedback_i18n = data.get("feedback_i18n")
        if isinstance(feedback_i18n, dict) and feedback_i18n:
            submission.feedback_json = feedback_i18n
            # feedback_text = English version as plain-text fallback
            submission.feedback_text = feedback_i18n.get("en") or next(iter(feedback_i18n.values()), "")
        else:
            raw_feedback = data.get("feedback_text", "No feedback provided.")
            submission.feedback_text = raw_feedback
            submission.feedback_json = {"en": raw_feedback, "ru": raw_feedback, "uz": raw_feedback}
        submission.estimated_level = str(data.get("estimated_level", "B1")).upper()[:5]
        submission.score = float(data.get("score", 0.0))
        submission.is_evaluated = True
        return True
        
    except Exception as e:
        print(f"[Speaking eval error] {e}")
        return False
