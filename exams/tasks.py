"""
exams/tasks.py — Celery tasks for background exam processing.

Tasks:
    grade_writing_submission   — AI grading of Writing submissions via Gemini.
    evaluate_speaking          — AI evaluation of Speaking recordings via Gemini.
    create_video_lesson_task   — Create a YouTube video lesson with AI-generated quizzes.
    run_ingestion              — Ingest a Listening/Writing/Reading ZIP via OCR pipeline.
    run_speaking_ingestion     — Ingest a Speaking image via AI OCR pipeline.
"""

import json
import logging
import os

from celery import shared_task
from django.db import connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Writing grading
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def grade_writing_submission(self, attempt_id: int) -> None:
    """Grade all WritingSubmission objects for *attempt_id* using Gemini."""
    try:
        from google import genai
        from google.genai import types as T

        from exams.models import UserAttempt

        attempt = UserAttempt.objects.get(pk=attempt_id)
        submissions = attempt.writing_submissions.select_related('task').all()

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project_id:
            adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
            if os.path.exists(adc_path):
                with open(adc_path) as f:
                    project_id = json.load(f).get("quota_project_id")

        client = genai.Client(vertexai=True, project=project_id, location="global")

        for sub in submissions:
            task = sub.task

            prompt = f"""Act as an expert CEFR Examiner for B2 Level. 
Evaluate the following student submission for a writing task.
Task Type: {task.get_task_type_display()}
Target Length: {task.min_words} to {task.max_words} words.
Task Instruction: {task.prompt}
Incoming text to reply to (if any): {task.input_text}

Student Submission ({sub.word_count} words):
{sub.submitted_text}

Provide detailed feedback with grammatical, lexical, and structural analysis.
Assign an estimated CEFR Level (A1, A2, B1, B2, C1).

Provide feedback in three languages. KEEP English linguistic/CEFR terms in all languages
(e.g. "B2", "cohesion", "lexical range", "discourse markers" — do NOT translate these).

Return strictly in this JSON format:
{{
  "estimated_level": "B2",
  "feedback_i18n": {{
    "en": "Detailed English feedback with English terminology.",
    "ru": "Подробный отзыв на русском с сохранением английских терминов (B2, cohesion, lexical range и т.д.).",
    "uz": "O'zbek tilida batafsil fikr-mulohaza, ingliz atamalari saqlanadi (B2, cohesion, lexical range va b.)."
  }},
  "corrections": [
    {{
      "original": "incorrect phrase",
      "correction": "correct phrase",
      "explanation_i18n": {{
        "en": "Why this is incorrect in English.",
        "ru": "Объяснение на русском языке.",
        "uz": "O'zbek tilidagi tushuntirish."
      }}
    }}
  ]
}}"""

            generate_cfg = T.GenerateContentConfig(response_mime_type="application/json")
            resp = client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=[prompt],
                config=generate_cfg,
            )

            raw_text = resp.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.startswith("```"):
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]

            result = json.loads(raw_text.strip())

            sub.estimated_level = result.get('estimated_level', 'N/A')
            feedback_i18n = result.get('feedback_i18n')
            if isinstance(feedback_i18n, dict) and feedback_i18n:
                sub.feedback_json = feedback_i18n
                sub.feedback_text = feedback_i18n.get('en') or next(iter(feedback_i18n.values()), '')
            else:
                raw_feedback = result.get('feedback', '')
                sub.feedback_text = raw_feedback
                sub.feedback_json = {'en': raw_feedback, 'ru': raw_feedback, 'uz': raw_feedback}
            sub.corrections_json = result.get('corrections', [])
            sub.is_graded = True
            sub.save()

    except Exception as exc:
        logger.exception("[grade_writing_submission] attempt_id=%s error: %s", attempt_id, exc)
        raise self.retry(exc=exc)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Speaking evaluation
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def evaluate_speaking(self, attempt_id: int) -> None:
    """Evaluate all unevaluated SpeakingSubmissions for *attempt_id* via Gemini."""
    try:
        from exams.models import UserAttempt
        from exams.speaking_services import evaluate_speaking_submission

        attempt = UserAttempt.objects.get(pk=attempt_id)
        for sub in attempt.speaking_submissions.filter(is_evaluated=False).select_related('question'):
            if evaluate_speaking_submission(sub):
                sub.save()
            else:
                sub.is_evaluated = True
                sub.transcript = "[Auto-transcript failed]"
                sub.feedback_text = "Sorry, failed to evaluate the recording."
                sub.estimated_level = "B1"
                sub.save()
    except Exception as exc:
        logger.exception("[evaluate_speaking] attempt_id=%s error: %s", attempt_id, exc)
        raise self.retry(exc=exc)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Video lesson creation
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=1, default_retry_delay=60)
def create_video_lesson_task(self, task_id: str, youtube_url: str, user_id: int, title: str, is_public: bool) -> None:
    """Create a YouTube video lesson with AI-generated quizzes for *user_id*."""
    from django.contrib.auth import get_user_model
    from django.core.cache import cache
    from exams.video_services import create_video_lesson

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
        lesson = create_video_lesson(
            youtube_url=youtube_url,
            user=user,
            title=title,
            is_public=is_public,
            task_id=task_id,
        )
        cache.set(
            f"video_task_{task_id}",
            {"pct": 100, "label": "Done!", "lesson_id": lesson.id},
            timeout=3600,
        )
    except ValueError as exc:
        from django.core.cache import cache
        cache.set(f"video_task_{task_id}", {"error": f"Invalid URL: {exc}"}, timeout=3600)
    except RuntimeError as exc:
        from django.core.cache import cache
        cache.set(f"video_task_{task_id}", {"error": str(exc)}, timeout=3600)
    except Exception as exc:
        from django.core.cache import cache
        cache.set(f"video_task_{task_id}", {"error": f"Unexpected error: {exc}"}, timeout=3600)
        logger.exception("[create_video_lesson_task] task_id=%s error: %s", task_id, exc)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Test material ingestion (Listening / Writing / Reading ZIP)
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=0)
def run_ingestion(self, task_id: int, zip_path: str, test_name: str, user_id: int, split_parts: bool) -> None:
    """
    Ingest exam materials from *zip_path* and populate the DB.
    Delegates to _run_ingestion_background() which already owns the full pipeline.
    """
    from exams.mentor_views import _run_ingestion_background
    _run_ingestion_background(task_id, zip_path, test_name, user_id, split_parts)


# ---------------------------------------------------------------------------
# Speaking image ingestion
# ---------------------------------------------------------------------------

@shared_task(bind=True, max_retries=0)
def run_speaking_ingestion(self, task_id: int, temp_image_path: str, test_name: str, user_id: int, original_filename: str) -> None:
    """
    Ingest a Speaking image via AI OCR pipeline.
    Delegates to _run_speaking_background() which already owns the full pipeline.
    """
    from exams.mentor_views import _run_speaking_background
    _run_speaking_background(task_id, temp_image_path, test_name, user_id, original_filename)
