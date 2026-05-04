"""
exams/reading_services.py — AI-powered Reading Test Parser and Database Ingestion.

Public API
----------
parse_reading_materials(parts_data)  ->  dict (validated JSON from LLM)
ingest_reading_json(test, parsed_json)  ->  dict (summary stats)

How the LLM handles separated answer keys
------------------------------------------
Reading tests are packaged as:
  - Several page images  (the passage + question images, potentially multi-page)
  - A plain-text answer key file  (e.g. "1. killers\\n2. chemicals\\n...")

Both are passed to the model in a single prompt so it can:
  1. Read the passage across all images and concatenate pages seamlessly.
  2. Extract each question's text, type, and options from the images.
  3. Look up the correct answer from the answer key by question number.
  4. Return a single unified JSON — no post-processing merge step needed.

The call is made ONCE per reading test (all 5 parts in one request) so that
the model has full context for global question numbering (Q1–35).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from django.db import transaction

from .models import (
    Test,
    ReadingTest,
    ReadingPart,
    ReadingPassage,
    ReadingQuestion,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The model that handles multi-image + long-context tasks well.
# Override via GEMINI_READING_MODEL environment variable.
GEMINI_MODEL = os.getenv("GEMINI_READING_MODEL", "gemini-3-flash-preview")
GEMINI_LOCATION = os.getenv("GEMINI_LOCATION", "global")

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

READING_SYSTEM_PROMPT = """
You are an expert Educational Data Engineer and AI Parser specialized in English language exams (CEFR).
Your task is to analyze the provided raw test materials (images of reading passages and questions) and the
separate answer keys, and extract them into a strictly formatted JSON object.

You are working on educational assessment tools — your output is used to build practice quizzes for students.
The passage text will be provided separately — you do NOT need to include the full passage in your output.
For the passage field, just include a brief title and leave content as an empty string "".

CRITICAL INSTRUCTIONS:

1. Multi-page Passages: Reading texts often span multiple pages (images) and may use multi-column layouts.
   You do NOT need to transcribe the passage text — it will be extracted separately.
   Set passage.content to an empty string. Only set passage.title if you can identify the title.
   IMPORTANT: If an image belongs to a DIFFERENT part (e.g. labeled "Part 4" in a Part 5 folder), IGNORE it
   and only use images relevant to the current part number.
   For fill-in-the-blank questions, note the gap positions in question_text, not in passage content.

2. Combine Questions with Answers: The answer keys are provided as plain text (e.g. "1. killers\\n2. chemicals").
   You MUST match each answer to its question number and set it as the "correct_answer" field.

3. Determine Question Types — assign ONE of the following to every question's "question_type":
   - "multiple_choice"   → the question has labelled options (A/B/C/D)
   - "fill_in_the_blank" → the student writes a word or phrase; the gap may or may not have options
   - "matching"          → match short texts (7-14) or paragraphs (I-VI) to a list of statements
   - "true_false_ni"     → decide True / False / No Information
   - "insert_sentence"   → choose where a sentence fits in a numbered paragraph list

4. Options Array:
   - For multiple_choice: include every option as a string, e.g. ["A) text...", "B) text...", "C) text...", "D) text..."]
   - For matching: include the full list of statements/headings, e.g. ["A) Jailbreak with creative thinking", ...]
   - For true_false_ni and fill_in_the_blank with no options: use an empty array []

5. Explanation: For EVERY question write a detailed explanation (4-6 sentences) of WHY the correct answer is right.
   Structure your explanation like a tutor:
   a) State what the correct answer is and why it is right.
   b) Quote or reference the specific part of the passage that proves it.
   c) Explain why each incorrect option is wrong (or why the alternative does not fit).
   d) Mention any vocabulary, grammar, or reasoning clue that helps identify the answer.
   Do NOT leave this field empty.

6. Passage vs Questions separation:
    - The "passage.content" field must contain ONLY the reading passage itself (clean text for reading).
    - Do NOT append question prompts, summary-completion statements, or answer lines into passage.content.
    - Any fill-in-the-blank statements belong in each question's "question_text", not in passage.content.

7. Output format: Return ONLY a valid JSON object matching the schema below. Do NOT wrap it in markdown
   code fences (```json) or add any explanatory text outside the JSON.

TARGET JSON SCHEMA:
{
  "part_number": <int>,
  "instruction": "<string>",
  "question_number_start": <int>,
  "question_number_end": <int>,
  "passage": {
    "title": "<string or empty string>",
    "content": "<full concatenated text>"
  },
  "questions": [
    {
      "question_number": <int>,
      "question_type": "<one of the 5 types above>",
      "question_text": "<stem or statement text>",
      "options": ["<option A>", "<option B>", ...],
      "correct_answer": "<letter or word from answer key>",
      "explanation": "<1-2 sentence explanation of WHY this is the correct answer, referencing the passage>"
    }
  ]
}
""".strip()


# ---------------------------------------------------------------------------
# Gemini-based passage transcription (replaces Tesseract OCR)
# ---------------------------------------------------------------------------

PASSAGE_TRANSCRIPTION_PROMPT = """
You are a precise text transcription assistant.
Your task: transcribe the reading passage from these scanned exam page images.

RULES:
1. Transcribe the passage text EXACTLY as it appears — word for word, preserving original wording.
2. For multi-page passages, concatenate pages seamlessly in reading order.
3. For two-column layouts, read left column first, then right column.
4. Preserve paragraph breaks using double newlines (\n\n).
5. Remove ONLY:
   - Page numbers (e.g. "157")
   - Headers like "TEST 119 READING PASSAGE", "PART 5"
   - Instructions like "Read the following text for questions 30-35"
   - Footer/watermark artifacts
6. Do NOT include any questions, answer options, or exam instructions.
   Only the passage (article/text) itself.
7. For fill-in-the-blank passages, mark each numbered blank as [GAP].
8. Return ONLY the transcribed passage text, nothing else.
""".strip()


def _extract_passage_via_gemini(image_paths: list[Path], part_number: int) -> str:
    """
    Use Gemini vision to transcribe the reading passage from page images.

    Makes a separate lightweight call that avoids RECITATION by returning
    plain text (not JSON with structured data).

    Returns the passage text or empty string on failure.
    """
    if not image_paths:
        return ""

    try:
        from google.genai import types as genai_types
    except ImportError:
        return ""

    try:
        client = _build_gemini_client()
    except Exception as exc:
        logger.warning("Cannot build Gemini client for passage extraction: %s", exc)
        return ""

    content_parts: list[Any] = []
    content_parts.append(
        genai_types.Part(
            text=f"Transcribe the reading passage from these Part {part_number} exam page images. "
                 "Skip any pages that only contain questions/answer options — extract only the passage text."
        )
    )

    for img_path in image_paths:
        if not img_path.exists():
            continue
        mime_type, _ = mimetypes.guess_type(str(img_path))
        if mime_type is None:
            mime_type = "image/jpeg"
        content_parts.append(
            genai_types.Part.from_bytes(
                data=img_path.read_bytes(), mime_type=mime_type
            )
        )

    config = genai_types.GenerateContentConfig(
        system_instruction=PASSAGE_TRANSCRIPTION_PROMPT,
        temperature=0.0,
    )

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=genai_types.Content(parts=content_parts, role="user"),
                config=config,
            )
            raw_text = response.text
            if not raw_text:
                if response.candidates:
                    text_parts = [
                        p.text for p in (response.candidates[0].content.parts or [])
                        if hasattr(p, "text") and p.text
                    ]
                    raw_text = "".join(text_parts)
            if raw_text and raw_text.strip():
                logger.info(
                    "Part %d: Gemini passage extraction succeeded (%d chars)",
                    part_number, len(raw_text.strip()),
                )
                return raw_text.strip()
            logger.warning(
                "Part %d: empty passage extraction response (attempt %d)",
                part_number, attempt + 1,
            )
        except Exception as exc:
            logger.warning(
                "Part %d: passage extraction failed (attempt %d): %s",
                part_number, attempt + 1, exc,
            )
        # Increase temperature on retry
        config = genai_types.GenerateContentConfig(
            system_instruction=PASSAGE_TRANSCRIPTION_PROMPT,
            temperature=0.2 * (attempt + 1),
        )

    logger.warning("Part %d: all passage extraction attempts failed, will use parsed passage", part_number)
    return ""


# ---------------------------------------------------------------------------
# Gemini client factory
# ---------------------------------------------------------------------------

def _build_gemini_client():
    """
    Initialise and return a google-genai client.

    Priority:
      1. Vertex AI (if GOOGLE_CLOUD_PROJECT is set or ADC is available)
      2. Direct Gemini API key (GEMINI_API_KEY env var)

    Raises ImportError if google-genai is not installed.
    Raises RuntimeError if neither auth method is available.
    """
    try:
        from google import genai  # google-genai >= 1.51
    except ImportError as exc:
        raise ImportError(
            "google-genai is not installed. "
            "Run: pip install 'google-genai>=1.51.0'"
        ) from exc

    # --- Vertex AI path ---
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        adc_path = os.path.expanduser(
            "~/.config/gcloud/application_default_credentials.json"
        )
        if os.path.exists(adc_path):
            try:
                with open(adc_path) as f:
                    project_id = json.load(f).get("quota_project_id")
            except (OSError, json.JSONDecodeError):
                pass

    if project_id:
        logger.debug("Building Gemini client via Vertex AI (project=%s)", project_id)
        return genai.Client(
            vertexai=True,
            project=project_id,
            location=GEMINI_LOCATION,
        )

    # --- Direct API key fallback ---
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        logger.debug("Building Gemini client via direct API key")
        return genai.Client(api_key=api_key)

    raise RuntimeError(
        "No Gemini credentials found. Set GOOGLE_CLOUD_PROJECT (Vertex AI) "
        "or GEMINI_API_KEY environment variable."
    )


# ---------------------------------------------------------------------------
# Task 2 — AI Parsing Service
# ---------------------------------------------------------------------------

def parse_reading_materials(parts_data: list[dict]) -> dict:
    """
    Send reading test pages + answer keys to the LLM (one call per part)
    and return the parsed JSON as a Python dict.

    Each part is processed individually to avoid RECITATION blocks
    that occur when too much text is sent at once.

    Parameters
    ----------
    parts_data : list[dict]
        One dict per part with keys: part_number, image_paths, answer_key.

    Returns
    -------
    dict  with key "parts" containing one entry per part.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from google.genai import types as genai_types

    client = _build_gemini_client()

    thinking_cfg = genai_types.ThinkingConfig(thinking_level="MINIMAL")
    config = genai_types.GenerateContentConfig(
        system_instruction=READING_SYSTEM_PROMPT,
        thinking_config=thinking_cfg,
        response_mime_type="application/json",
    )

    def _parse_single_part(pd: dict) -> dict:
        """Parse a single part via Gemini and return the part JSON dict."""
        part_number = pd["part_number"]
        image_paths = [Path(p) for p in pd.get("image_paths", [])]
        answer_key = pd.get("answer_key", "").strip()

        content_parts: list[Any] = []
        content_parts.append(
            genai_types.Part(
                text=f"Below are the scanned pages for PART {part_number} of a CEFR Reading Test. "
                     "Extract questions, passage text, and answers into the JSON schema "
                     "specified in your system instructions.\n\n"
            )
        )

        for img_path in image_paths:
            if not img_path.exists():
                logger.warning("Image not found, skipping: %s", img_path)
                continue
            mime_type, _ = mimetypes.guess_type(str(img_path))
            if mime_type is None:
                mime_type = "image/jpeg"
            content_parts.append(
                genai_types.Part.from_bytes(
                    data=img_path.read_bytes(), mime_type=mime_type
                )
            )

        if answer_key:
            content_parts.append(
                genai_types.Part(
                    text=f"\n--- ANSWER KEY (match to question numbers above) ---\n"
                         f"{answer_key}\n"
                )
            )
        else:
            content_parts.append(
                genai_types.Part(text="\n--- No answer key provided ---\n")
            )

        content_parts.append(
            genai_types.Part(
                text=f"\nNow produce the JSON object for Part {part_number}. "
                     "Set passage.content to an empty string — passage text is extracted separately. "
                     "Focus on extracting questions, their types, options, correct answers, and explanations. "
                     "Do NOT place question lines in passage.content."
            )
        )

        MAX_RETRIES = 3
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            # On retries, use higher thinking and different temperature to avoid RECITATION
            if attempt > 0:
                retry_thinking = genai_types.ThinkingConfig(thinking_level="MEDIUM")
                retry_config = genai_types.GenerateContentConfig(
                    system_instruction=READING_SYSTEM_PROMPT,
                    thinking_config=retry_thinking,
                    temperature=0.3 * attempt,
                    response_mime_type="application/json",
                )
            else:
                retry_config = config

            try:
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=genai_types.Content(parts=content_parts, role="user"),
                    config=retry_config,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Part %d: API call failed (attempt %d): %s",
                    part_number, attempt + 1, exc,
                )
                continue

            raw_text = response.text
            if raw_text is None:
                finish = "unknown"
                if response.candidates:
                    finish = getattr(response.candidates[0], "finish_reason", "unknown")
                    text_parts = [
                        p.text for p in (response.candidates[0].content.parts or [])
                        if hasattr(p, "text") and p.text
                    ]
                    raw_text = "".join(text_parts)
                if not raw_text:
                    last_error = RuntimeError(
                        f"Part {part_number}: empty response (finish_reason={finish})"
                    )
                    logger.warning(
                        "Part %d: empty response (attempt %d, finish=%s)",
                        part_number, attempt + 1, finish,
                    )
                    continue

            raw_text = raw_text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```", 2)[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
                raw_text = raw_text.rstrip("`").strip()

            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                last_error = exc
                logger.warning(
                    "Part %d: invalid JSON (attempt %d): %s",
                    part_number, attempt + 1, exc,
                )
                continue

            # Ensure part_number is set
            parsed.setdefault("part_number", part_number)

            # Extract passage text via dedicated Gemini call (verbatim transcription).
            gemini_passage = _extract_passage_via_gemini(image_paths, part_number)
            if gemini_passage:
                parsed.setdefault("passage", {})
                parsed["passage"]["content"] = gemini_passage

            logger.info(
                "Part %d parsed: %d questions",
                part_number, len(parsed.get("questions", [])),
            )
            return parsed

        raise RuntimeError(
            f"Part {part_number}: all {MAX_RETRIES + 1} attempts failed. "
            f"Last error: {last_error}"
        )

    # ------------------------------------------------------------------ #
    # Run all parts in parallel
    # ------------------------------------------------------------------ #
    logger.info(
        "Calling Gemini (%s) to parse reading test — %d parts individually…",
        GEMINI_MODEL, len(parts_data),
    )

    all_parts: list[dict] = [None] * len(parts_data)  # type: ignore[list-item]
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(4, len(parts_data))) as executor:
        future_to_idx = {
            executor.submit(_parse_single_part, pd): i
            for i, pd in enumerate(parts_data)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                all_parts[idx] = future.result()
            except Exception as exc:
                errors.append(str(exc))
                logger.error("Part parsing failed: %s", exc)

    if errors:
        raise RuntimeError(
            f"{len(errors)} part(s) failed to parse:\n" + "\n".join(errors)
        )

    total_q = sum(len(p.get("questions", [])) for p in all_parts)
    logger.info("Parsing complete: %d parts, %d total questions", len(all_parts), total_q)

    return {"test_title": "", "parts": all_parts}


# ---------------------------------------------------------------------------
# Task 3 — Database Ingestion Logic
# ---------------------------------------------------------------------------

def ingest_reading_json(test: Test, parsed_json: dict) -> dict:
    """
    Persist the AI-generated reading test structure into the Django database.

    Strategy
    --------
    * All DB writes are wrapped in ``transaction.atomic()`` so a failure
      in any part or question rolls back the entire operation — no partial saves.
    * If the test already has reading data, it is cleared first (re-ingestion).
    * The function creates/updates:
        ReadingTest  (extension metadata)
        ReadingPart  (one per part in the JSON)
        ReadingPassage  (if the part has a non-null passage)
        ReadingQuestion  (one per question in the JSON)

    Parameters
    ----------
    test : Test
        An existing Test instance with ``test_type='reading'``.
    parsed_json : dict
        The validated dict returned by ``parse_reading_materials()``.

    Returns
    -------
    dict
        Summary stats::

            {
                "parts_created": 5,
                "questions_created": 35,
                "passages_created": 4,
            }

    Raises
    ------
    ValueError
        On data validation errors (missing keys, invalid question_type, etc.).
    django.db.IntegrityError
        On duplicate question numbers (propagated from DB unique constraint).
    """
    _validate_parsed_json(parsed_json)

    parts_list: list[dict] = parsed_json["parts"]

    parts_created = 0
    questions_created = 0
    passages_created = 0

    with transaction.atomic():
        # ------------------------------------------------------------------ #
        # 1. Create or refresh the ReadingTest extension record
        # ------------------------------------------------------------------ #
        reading_meta, _ = ReadingTest.objects.update_or_create(
            test=test,
            defaults={"total_parts": len(parts_list)},
        )

        # ------------------------------------------------------------------ #
        # 2. Clear stale reading data on re-ingestion so we start clean.
        #    Deleting ReadingPart cascades to ReadingPassage + ReadingQuestion.
        # ------------------------------------------------------------------ #
        stale_count = test.reading_parts.count()
        if stale_count:
            logger.info(
                "Re-ingesting '%s': deleting %d existing reading parts.", test.name, stale_count
            )
            test.reading_parts.all().delete()

        # ------------------------------------------------------------------ #
        # 3. Insert new data from the JSON
        # ------------------------------------------------------------------ #
        for part_data in parts_list:
            part_number: int = int(part_data["part_number"])
            instruction: str = part_data.get("instruction", "").strip()
            q_start: int | None = part_data.get("question_number_start")
            q_end:   int | None = part_data.get("question_number_end")

            # Infer question range from the questions list if not provided
            questions_raw: list[dict] = part_data.get("questions", [])
            if questions_raw:
                q_numbers = [int(q["question_number"]) for q in questions_raw]
                q_start = q_start or min(q_numbers)
                q_end   = q_end   or max(q_numbers)

            reading_part = ReadingPart.objects.create(
                test=test,
                part_number=part_number,
                instruction=instruction,
                question_number_start=q_start,
                question_number_end=q_end,
            )
            parts_created += 1
            logger.debug("Created ReadingPart %d for test '%s'", part_number, test.name)

            # ---------------------------------------------------------------- #
            # 3a. Passage (optional — null for short-text matching parts)
            # ---------------------------------------------------------------- #
            passage_data = part_data.get("passage")
            if passage_data:
                content = passage_data.get("content", "").strip()
                if content:  # only create a Passage if there is actual text
                    ReadingPassage.objects.create(
                        part=reading_part,
                        title=passage_data.get("title", "").strip(),
                        content=content,
                    )
                    passages_created += 1

            # ---------------------------------------------------------------- #
            # 3b. Questions — validate type before bulk-creating
            # ---------------------------------------------------------------- #
            valid_types = {t[0] for t in ReadingQuestion.QUESTION_TYPES}

            for q_data in questions_raw:
                q_number  = int(q_data["question_number"])
                q_type    = q_data.get("question_type", "").strip()
                q_text    = q_data.get("question_text", "").strip()
                options   = q_data.get("options", [])
                answer    = str(q_data.get("correct_answer", "")).strip()
                explanation = q_data.get("explanation", "").strip()

                # ── Validation ──────────────────────────────────────────── #
                if q_type not in valid_types:
                    raise ValueError(
                        f"Part {part_number}, Q{q_number}: unknown question_type "
                        f"'{q_type}'. Valid types: {sorted(valid_types)}"
                    )
                if not answer:
                    raise ValueError(
                        f"Part {part_number}, Q{q_number}: correct_answer is empty. "
                        "Check the answer key alignment in the LLM output."
                    )
                if not isinstance(options, list):
                    raise ValueError(
                        f"Part {part_number}, Q{q_number}: 'options' must be a list, "
                        f"got {type(options).__name__}."
                    )

                ReadingQuestion.objects.create(
                    part=reading_part,
                    question_number=q_number,
                    question_type=q_type,
                    question_text=q_text,
                    options=options,
                    correct_answer=answer,
                    explanation=explanation,
                )
                questions_created += 1

    summary = {
        "parts_created": parts_created,
        "questions_created": questions_created,
        "passages_created": passages_created,
    }
    logger.info(
        "Ingestion complete for '%s': %s",
        test.name,
        summary,
    )
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_parsed_json(data: dict) -> None:
    """
    Lightweight schema validation before attempting DB writes.
    Raises ValueError with a descriptive message on the first problem found.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object at the top level, got {type(data).__name__}.")

    if "parts" not in data:
        raise ValueError("JSON is missing required key 'parts'.")

    if not isinstance(data["parts"], list) or not data["parts"]:
        raise ValueError("'parts' must be a non-empty list.")

    for i, part in enumerate(data["parts"]):
        if "part_number" not in part:
            raise ValueError(f"parts[{i}] is missing 'part_number'.")
        if "questions" not in part:
            raise ValueError(f"parts[{i}] (part {part.get('part_number')}) is missing 'questions'.")
        if not isinstance(part["questions"], list):
            raise ValueError(
                f"parts[{i}] (part {part.get('part_number')}): 'questions' must be a list."
            )
        for j, q in enumerate(part["questions"]):
            for required_key in ("question_number", "question_type", "correct_answer"):
                if required_key not in q:
                    raise ValueError(
                        f"parts[{i}].questions[{j}] is missing required key '{required_key}'."
                    )


# ---------------------------------------------------------------------------
# Convenience wrapper: build parts_data from the "reading test materials" folder
# ---------------------------------------------------------------------------

def build_parts_data_from_folder(folder_path: str | Path) -> list[dict]:
    """
    Helper to assemble ``parts_data`` from the standard folder layout used
    in this project::

        <folder>/
            Part 1/
                Answers          ← plain-text answer key
                photo_*.jpg      ← one or more page images
            Part 2/
                Answers
                photo_*.jpg
                photo_*.jpg
            ...

    Parameters
    ----------
    folder_path : str | Path
        Absolute path to the root folder (e.g. "reading test materials/").

    Returns
    -------
    list[dict]  suitable as the ``parts_data`` argument to ``parse_reading_materials()``.
    """
    root = Path(folder_path)
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    part_dirs = sorted(
        [d for d in root.iterdir() if d.is_dir() and d.name.lower().startswith("part")],
        key=lambda d: int(d.name.split()[-1]),  # "Part 3" → 3
    )

    parts_data: list[dict] = []
    for part_dir in part_dirs:
        part_number = int(part_dir.name.split()[-1])

        # Collect page images in sorted order (chronological / alphabetical)
        image_paths = sorted(
            p for p in part_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        )

        # Read answer key (file named "Answers" or "answers.txt")
        answer_key = ""
        for candidate in ("Answers", "answers.txt", "answers"):
            ans_file = part_dir / candidate
            if ans_file.exists():
                answer_key = ans_file.read_text(encoding="utf-8").strip()
                break

        parts_data.append(
            {
                "part_number": part_number,
                "image_paths": [str(p) for p in image_paths],
                "answer_key": answer_key,
            }
        )

    if not parts_data:
        raise ValueError(f"No 'Part N' subdirectories found in {root}")

    return parts_data


# ---------------------------------------------------------------------------
# Post-ingestion: generate explanations for ReadingQuestions
# ---------------------------------------------------------------------------

def generate_reading_explanations(test: "Test", stdout=None, force: bool = False) -> int:
    """
    Generate AI explanations for all ReadingQuestions in *test* that currently
    have an empty ``explanation`` field.

    Set *force=True* to regenerate explanations even for questions that already
    have one (useful after updating the explanation prompt).

    Processes one part at a time: sends the passage text + question list to
    Gemini and saves the returned explanations back to the DB.

    Returns
    -------
    int  Number of questions updated.
    """
    import sys
    from google.genai import types as genai_types
    from .models import ReadingPart, ReadingQuestion

    out = stdout or sys.stdout

    client = _build_gemini_client()

    updated = 0
    parts = test.reading_parts.prefetch_related("questions").order_by("part_number")

    for part in parts:
        questions = list(
            part.questions.all().order_by("question_number")
            if force
            else part.questions.filter(explanation__in=["", None]).order_by("question_number")
        )
        if not questions:
            out.write(f"  Part {part.part_number}: all explanations already present, skipping.\n")
            continue

        out.write(f"  Part {part.part_number}: generating explanations for {len(questions)} questions...\n")

        # Build passage context (may be empty for matching-short-texts parts)
        try:
            passage_text = part.passage.content
            passage_title = part.passage.title or ""
        except Exception:
            passage_text = ""
            passage_title = ""

        q_lines = []
        for q in questions:
            line = f"Q{q.question_number} ({q.question_type})"
            if q.question_text:
                line += f": {q.question_text}"
            line += f" → Correct answer: {q.correct_answer}"
            if q.options:
                line += f"  Options: {q.options}"
            q_lines.append(line)

        prompt = (
            "You are an expert CEFR exam tutor. Below is a reading passage"
            + (f" titled '{passage_title}'" if passage_title else "")
            + " followed by a list of exam questions and their correct answers.\n\n"
            + ("PASSAGE:\n" + passage_text + "\n\n" if passage_text else "")
            + "QUESTIONS AND CORRECT ANSWERS:\n"
            + "\n".join(q_lines)
            + "\n\n"
            "For EACH question number, write a detailed tutor-style explanation (4-6 sentences) of "
            "WHY the given correct answer is right. Structure each explanation as follows:\n"
            "1. State what the correct answer is and directly explain why it is correct.\n"
            "2. Quote or paraphrase the specific part of the passage or question that proves it.\n"
            "3. Explain why the other options or alternatives are NOT correct.\n"
            "4. Highlight any key vocabulary, grammar, or reasoning clue that helps identify the answer.\n\n"
            "Return ONLY valid JSON in this exact shape (no markdown fences):\n"
            '{"explanations": {"<question_number>": "<detailed explanation text>", ...}}\n'
            "Use the exact question number integers as keys (e.g. 1, 7, 30)."
        )

        try:
            config = genai_types.GenerateContentConfig(
                response_mime_type="application/json",
            )
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
            raw = (response.text or "").strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.rstrip("`").strip()

            data = json.loads(raw)
            explanations: dict = data.get("explanations", {})

            out.write(f"    Gemini returned {len(explanations)} explanation(s). Keys: {list(explanations.keys())[:5]}\n")

            for q in questions:
                expl = explanations.get(str(q.question_number)) or explanations.get(q.question_number)
                if expl:
                    q.explanation = str(expl).strip()
                    q.save(update_fields=["explanation"])
                    updated += 1

            logger.info(
                "Part %d: generated %d explanations",
                part.part_number, len(explanations),
            )
        except Exception as exc:
            out.write(f"    ERROR: {exc}\n")
            logger.warning(
                "Part %d: explanation generation failed: %s",
                part.part_number, exc,
            )

    return updated

