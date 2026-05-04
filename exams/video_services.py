"""
exams/video_services.py — YouTube Video Learning Service Layer.

Public API
----------
extract_youtube_id(url)          -> str
fetch_transcript(youtube_id)     -> list[dict]
generate_quiz_questions(...)     -> list[dict]
create_video_lesson(...)         -> VideoLesson

Orchestrates fetching YouTube transcripts via youtube-transcript-api,
yt-dlp, and Gemini Flash, generating quiz questions via Gemini Flash, 
and persisting everything to the database.
"""

from __future__ import annotations

import json
import logging
import re
import os
import tempfile
from typing import Any

from django.db import transaction

from .models import VideoLesson, QuizQuestion

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEMINI_MODEL = "gemini-3-flash-preview"

# Minimum seconds to wait after the last tested segment ends before showing the question.
MIN_TRIGGER_BUFFER_SECONDS = 10

# Minimum video duration accepted for lesson creation (10 minutes).
MIN_VIDEO_DURATION_SECONDS = 600

# Maximum transcript segments sent to Gemini; longer transcripts are sampled.
MAX_TRANSCRIPT_SEGMENTS = 250

QUIZ_GENERATION_PROMPT = """
You are an expert English language teacher specializing in CEFR exam preparation.

Below is a transcript of a YouTube video. Each line is formatted as:
  [START - END] spoken text
where START and END are timestamps in MM:SS (minutes:seconds) showing when
that segment BEGINS and ENDS in the video.

Your task is three-fold:
1. Determine the appropriate CEFR level for this video (from A1 to C1). Do not assign C2; cap the maximum difficulty at C1.
2. Identify 2–5 topic categories that best describe the video content. Choose from this list (pick only what applies):
   Science, Technology, Business, Education, Health, Nature, Environment,
   History, Culture, Arts, Sports, Travel, Food, Psychology, Politics,
   Society, Entertainment, Language, Philosophy, Economics
3. Generate Listening Comprehension quiz questions tailored to that specific CEFR level.

RULES FOR QUIZZES:
1. Generate between 4 and 8 questions, evenly distributed across the video timeline.
2. CRITICAL TIMING RULE — trigger_time_seconds is when the video will pause to show
   the question. It MUST be set like this:
     a. Find the END time (in seconds) of the LAST transcript segment that contains
        the information being tested.
     b. Add at least 10 seconds to that END time.
     c. Use that result as trigger_time_seconds.
   Example: if the answer is spoken in a segment that ends at 01:45 (105 seconds),
   set trigger_time_seconds to at least 115.
   NEVER set trigger_time_seconds earlier than the END time of any segment
   containing the tested content.
3. Each question must have exactly 4 answer options.
4. Questions should test comprehension, not just vocabulary:
   - "What did the speaker say about...?"
   - "According to the speaker, why...?"
   - "What example did the speaker give for...?"
5. The correct_option_index is 0-based (0, 1, 2, or 3). YOU MUST RANDOMIZE the position of the correct answer! Do NOT always make the correct answer option 0. Randomly distribute correct_option_index across 0, 1, 2, and 3.
6. Write a brief explanation (2-3 sentences) for each correct answer.
7. Make distractors plausible but clearly wrong if the student listened carefully.
8. Questions should progress chronologically through the video.

TRANSCRIPT:
{transcript_text}

Return ONLY valid JSON matching this schema (no markdown fences, no extra text):
{{
  "cefr_level": "<A1|A2|B1|B2|C1>",
  "topic_tags": ["<tag1>", "<tag2>"],
  "questions": [
    {{
      "trigger_time_seconds": <int>,
      "question_text": "<string>",
      "options": ["<option_0>", "<option_1>", "<option_2>", "<option_3>"],
      "correct_option_index": <0|1|2|3>,
      "explanation": "<string>"
    }}
  ]
}}
""".strip()


# ---------------------------------------------------------------------------
# YouTube ID Extraction
# ---------------------------------------------------------------------------

def extract_youtube_id(url: str) -> str:
    url = url.strip()

    if re.match(r'^[A-Za-z0-9_-]{11}$', url):
        return url

    match = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url)
    if match: return match.group(1)

    match = re.search(r'youtu\.be/([A-Za-z0-9_-]{11})', url)
    if match: return match.group(1)

    match = re.search(r'(?:/embed/|/v/)([A-Za-z0-9_-]{11})', url)
    if match: return match.group(1)

    raise ValueError(f"Could not extract a valid YouTube video ID from: {url!r}")

# ---------------------------------------------------------------------------
# YouTube Metadata
# ---------------------------------------------------------------------------

def fetch_youtube_title(youtube_id: str) -> str:
    import requests as _requests

    api_key = os.getenv("YOUTUBE_API_KEY")
    if api_key:
        api_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet&id={youtube_id}&key={api_key}"
        try:
            resp = _requests.get(api_url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("items"):
                title = data["items"][0]["snippet"]["title"].strip()
                if title: return title
        except Exception as exc:
            logger.warning("YouTube Data API failed for %s: %s", youtube_id, exc)

    oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={youtube_id}&format=json"
    try:
        resp = _requests.get(oembed_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        title = data.get('title', '').strip()
        if title: return title
    except Exception as exc:
        logger.warning("Could not fetch YouTube title via oEmbed: %s", exc)

    return f"YouTube Video — {youtube_id}"

# ---------------------------------------------------------------------------
# Transcript Fetching Pipeline
# ---------------------------------------------------------------------------

def fetch_transcript(youtube_id: str) -> list[dict]:
    """
    3-Tier Strategy to bypass YouTube blocks:
      1. youtube-transcript-api (fastest, but IP-sensitive)
      2. yt-dlp (very robust against anti-bot)
      3. Gemini Flash Audio Transcription (if no subs exist)
    """
    last_error = None

    # === Strategy 1: youtube-transcript-api ===
    logger.info("Strategy 1: Attempting youtube-transcript-api for %s", youtube_id)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt_api = YouTubeTranscriptApi() # Simplified for brevity, you can keep your proxy logic
        
        try:
            transcript = ytt_api.fetch(youtube_id, languages=['en'])
            result = transcript.to_raw_data()
            logger.info("Success! Fetched transcript via API: %d segments", len(result))
            return result
        except Exception:
            # Try auto-generated
            t_list = ytt_api.list(youtube_id)
            try:
                transcript = t_list.find_generated_transcript(['en'])
                result = transcript.fetch().to_raw_data()
                logger.info("Success! Fetched auto-generated via API: %d segments", len(result))
                return result
            except Exception as e:
                raise e

    except Exception as err:
        logger.warning("Strategy 1 failed for %s: %s", youtube_id, err)
        last_error = err

    # === Strategy 2: yt-dlp (Bulletproof scraping) ===
    logger.info("Strategy 2: Attempting yt-dlp extraction for %s", youtube_id)
    try:
        result = _fetch_transcript_via_ytdlp(youtube_id)
        if result:
            logger.info("Success! Fetched transcript via yt-dlp: %d segments", len(result))
            return result
    except Exception as err:
        logger.warning("Strategy 2 failed for %s: %s", youtube_id, err)
        last_error = err

    # === Strategy 3: Gemini Fallback ===
    logger.info("Strategy 3: Attempting Gemini Native extraction for %s", youtube_id)
    try:
        result = _fetch_transcript_via_gemini(youtube_id)
        if result:
            logger.info("Success! Fetched transcript via Gemini: %d segments", len(result))
            return result
    except Exception as err:
        logger.error("Strategy 3 failed for %s: %s", youtube_id, err)
        last_error = err

    raise RuntimeError(
        f"Could not fetch English transcript for video {youtube_id}. "
        f"All 3 strategies failed. Last error: {last_error}"
    )


def _fetch_transcript_via_ytdlp(youtube_id: str) -> list[dict]:
    """Uses yt-dlp to download subtitles and webvtt-py to parse them."""
    import yt_dlp
    import webvtt
    
    url = f"https://www.youtube.com/watch?v={youtube_id}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['en'],
            'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            
        # Find the downloaded .vtt file
        vtt_file = None
        for file in os.listdir(tmpdir):
            if file.endswith('.vtt'):
                vtt_file = os.path.join(tmpdir, file)
                break
                
        if not vtt_file:
            raise RuntimeError("yt-dlp could not find or download English subtitles.")
            
        # Parse VTT
        segments = []
        for caption in webvtt.read(vtt_file):
            # Convert HH:MM:SS.mmm to seconds
            start_parts = caption.start.split(':')
            start_sec = float(start_parts[0])*3600 + float(start_parts[1])*60 + float(start_parts[2])
            
            end_parts = caption.end.split(':')
            end_sec = float(end_parts[0])*3600 + float(end_parts[1])*60 + float(end_parts[2])
            
            # Clean up text (remove tags like <c>)
            text = re.sub(r'<[^>]+>', '', caption.text).strip()
            
            # Ignore empty segments
            if not text: continue
                
            segments.append({
                'text': text,
                'start': start_sec,
                'duration': end_sec - start_sec
            })
            
        return segments


def _fetch_transcript_via_gemini(youtube_id: str) -> list[dict]:
    """
    Updated prompt: If previous steps failed, the video might NOT have captions.
    We now allow Gemini to attempt to transcribe the video by listening to the audio.
    """
    import time
    from google.genai import types as genai_types
    from .reading_services import _build_gemini_client

    client = _build_gemini_client()
    video_url = f"https://www.youtube.com/watch?v={youtube_id}"
    
    video_part = genai_types.Part.from_uri(file_uri=video_url, mime_type='video/youtube')

    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.1,
    )

    MAX_PAGES = 5 
    current_offset = 0
    all_segments = []

    for page in range(MAX_PAGES):
        # Allow Gemini to actually transcribe if subs are missing
        prompt = f"""This is a YouTube video. 
Please provide the English transcription of the spoken audio starting from EXACTLY {current_offset} seconds.
Listen to the audio and transcribe it.

Each element should contain the text and its timestamp:
[
  {{"text": "transcribed text here", "start": {float(current_offset)}, "duration": 5.0}},
  ...
]

Return ONLY the JSON array.
"""

        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[video_part, prompt],
                config=config,
            )
            raw_text = response.text
        except Exception as e:
            if page == 0 and ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)):
                raise RuntimeError("Google AI API quota exceeded (429). The system is overloaded. Please wait a minute and try again.") from e
            break

        if not raw_text: break

        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```", 2)[1]
            if raw_text.startswith("json"): raw_text = raw_text[4:]
            raw_text = raw_text.rstrip("`").strip()

        import json as _json
        try:
            parsed = _json.loads(raw_text)
        except _json.JSONDecodeError:
            break

        if not isinstance(parsed, list) or not parsed: break

        page_segments = []
        for item in parsed:
            page_segments.append({
                'text': str(item.get('text', '')),
                'start': float(item.get('start', 0)),
                'duration': float(item.get('duration', 3.0)),
            })

        if not page_segments: break

        last_added_start = all_segments[-1]['start'] if all_segments else -1
        new_segments = [s for s in page_segments if s['start'] > last_added_start]
        
        if not new_segments: break
        all_segments.extend(new_segments)

        last_start = new_segments[-1]['start']
        if last_start - current_offset < 120: break
        current_offset = int(last_start)
        time.sleep(5)

    if not all_segments:
        raise RuntimeError("Gemini returned empty or invalid response for transcription")

    return all_segments


# ---------------------------------------------------------------------------
# Transcript utilities & Quiz Generation
# ---------------------------------------------------------------------------

def _get_transcript_duration(transcript: list[dict]) -> float:
    if not transcript: return 0.0
    last = max(transcript, key=lambda s: float(s.get('start', 0)))
    return float(last.get('start', 0)) + float(last.get('duration', 0))


def _sample_transcript(transcript: list[dict], max_segments: int = MAX_TRANSCRIPT_SEGMENTS) -> list[dict]:
    n = len(transcript)
    if n <= max_segments: return transcript
    indices = {0, n - 1}
    step = (n - 2) / (max_segments - 2)
    for i in range(1, max_segments - 1):
        indices.add(min(int(round(i * step)), n - 1))
    return [transcript[i] for i in sorted(indices)]


def _build_transcript_text(transcript: list[dict]) -> str:
    lines = []
    for segment in transcript:
        start = segment['start']
        end = start + segment.get('duration', 0)
        s_min, s_sec = int(start // 60), int(start % 60)
        e_min, e_sec = int(end // 60), int(end % 60)
        timestamp = f"[{s_min:02d}:{s_sec:02d} - {e_min:02d}:{e_sec:02d}]"
        lines.append(f"{timestamp} {segment['text']}")
    return "\n".join(lines)


def generate_quiz_questions(transcript: list[dict], task_id: str = None) -> tuple[str, list[dict]]:
    from google.genai import types as genai_types
    from .reading_services import _build_gemini_client
    from django.core.cache import cache

    client = _build_gemini_client()

    if task_id:
        cache.set(f"video_task_{task_id}", {"pct": 40, "label": "AI analysing transcript..."}, timeout=3600)

    sampled = _sample_transcript(transcript)
    transcript_text = _build_transcript_text(sampled)
    prompt = QUIZ_GENERATION_PROMPT.format(transcript_text=transcript_text)

    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.7,
    )

    MAX_RETRIES = 3
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[prompt],
                config=config,
            )

            raw_text = response.text
            if not raw_text: continue

            raw_text = raw_text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```", 2)[1]
                if raw_text.startswith("json"): raw_text = raw_text[4:]
                raw_text = raw_text.rstrip("`").strip()

            parsed = json.loads(raw_text)
            cefr_level = parsed.get("cefr_level", "B2")
            topic_tags = parsed.get("topic_tags", [])
            questions = parsed.get("questions", [])

            if not questions: continue

            # Validate basic structure to prevent KeyErrors
            if not all(all(k in q for k in ['trigger_time_seconds', 'question_text', 'options', 'correct_option_index']) for q in questions):
                continue

            for q in questions:
                t = q['trigger_time_seconds']
                for seg in transcript:
                    seg_end = seg['start'] + seg.get('duration', 0)
                    if seg['start'] <= t < seg_end:
                        q['trigger_time_seconds'] = int(seg_end) + MIN_TRIGGER_BUFFER_SECONDS
                        break

            return cefr_level, topic_tags, questions

        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"Quiz generation failed after {MAX_RETRIES} attempts. Last error: {last_error}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def create_video_lesson(youtube_url: str, user, title: str = "", is_public: bool = False, task_id: str = None) -> VideoLesson:
    from django.core.cache import cache
    
    def set_progress(pct, label):
        if task_id: cache.set(f"video_task_{task_id}", {"pct": pct, "label": label}, timeout=3600)

    set_progress(10, "Extracting video ID...")
    youtube_id = extract_youtube_id(youtube_url)

    existing = VideoLesson.objects.filter(youtube_id=youtube_id).first()
    if existing: return existing

    set_progress(20, "Fetching subtitles...")
    transcript = fetch_transcript(youtube_id)

    duration = _get_transcript_duration(transcript)
    if duration < MIN_VIDEO_DURATION_SECONDS:
        raise ValueError("Video is too short or subtitles could not be extracted fully.")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_quiz = pool.submit(generate_quiz_questions, transcript, task_id)
        future_title = pool.submit(fetch_youtube_title, youtube_id) if not title else None
        cefr_level, topic_tags, quiz_data = future_quiz.result() 
        if future_title is not None:
            title = future_title.result()

    thumbnail_url = f"https://img.youtube.com/vi/{youtube_id}/hqdefault.jpg"
    set_progress(85, "Saving lesson...")

    with transaction.atomic():
        lesson = VideoLesson.objects.create(
            youtube_id=youtube_id,
            title=title,
            cefr_level=cefr_level,
            topic_tags=topic_tags,
            thumbnail_url=thumbnail_url,
            transcript_json=transcript,
            is_public=is_public,
            created_by=user,
        )

        quiz_objects = [
            QuizQuestion(
                video=lesson,
                trigger_time_seconds=q['trigger_time_seconds'],
                question_text=q['question_text'],
                options=q['options'],
                correct_option_index=q['correct_option_index'],
                explanation=q.get('explanation', ''),
            )
            for q in quiz_data
        ]
        QuizQuestion.objects.bulk_create(quiz_objects)

    return lesson