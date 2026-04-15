# requirements:
#   pip install google-genai>=1.51.0 Pillow

import os
import re
import json
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from PIL import Image, ExifTags

from exams.models import Test, Part, Question, Choice

PART_QUESTION_TYPES = {
    1: 'multiple_choice',
    2: 'fill_blank',
    3: 'multiple_choice',
    4: 'map_label',
    5: 'multiple_choice',
    6: 'fill_blank',
}

PART_ROTATIONS = {
    2: 270,
    3: 270,
    4: 270,
    5: 180,
}

# Gemini 3 Flash доступен ТОЛЬКО через глобальный endpoint
GEMINI_MODEL    = "gemini-3-flash-preview"
GEMINI_LOCATION = "global"


class Command(BaseCommand):
    help = 'Ingest CEFR exam materials from the materials/ directory.'

    def add_arguments(self, parser):
        parser.add_argument('--test',     type=str, default=None)
        parser.add_argument('--dry-run',  action='store_true')
        parser.add_argument('--skip-ocr', action='store_true')
        parser.add_argument(
            '--thinking-level',
            choices=['minimal', 'low', 'medium', 'high'],
            default='minimal',
            help=(
                'Gemini 3 thinking level. '
                '"minimal" = fastest/cheapest (default for OCR); '
                '"high" = deepest reasoning.'
            ),
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        self.skip_ocr      = options['skip_ocr']
        self.thinking_level = options['thinking_level'].upper()   # SDK хочет UPPER
        self.genai_client  = None

        if not self.skip_ocr:
            try:
                from google import genai                          # google-genai >= 1.51
                from google.genai import types as genai_types

                self.genai_types = genai_types

                project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
                if not project_id:
                    adc_path = os.path.expanduser(
                        "~/.config/gcloud/application_default_credentials.json"
                    )
                    if os.path.exists(adc_path):
                        with open(adc_path) as f:
                            project_id = json.load(f).get("quota_project_id")

                self.stdout.write('    ⏳  Initialising Gen AI client (Gemini 3 Flash)…')

                # Gemini 3 требует location="global"
                self.genai_client = genai.Client(
                    vertexai=True,
                    project=project_id,
                    location=GEMINI_LOCATION,
                )

                self.stdout.write(self.style.SUCCESS(
                    f'    ✓  Gen AI client ready  '
                    f'(project={project_id or "ADC default"}, '
                    f'model={GEMINI_MODEL}, '
                    f'thinking={self.thinking_level})'
                ))

            except ImportError:
                raise CommandError(
                    'google-genai is not installed.\n'
                    '  Install:  pip install "google-genai>=1.51.0"\n'
                    '  Or run with:  --skip-ocr'
                )
            except Exception as e:
                raise CommandError(f'Failed to initialise Gen AI client: {e}')

        materials_dir = settings.BASE_DIR / 'materials'
        if not materials_dir.exists():
            raise CommandError(f'Materials directory not found: {materials_dir}')

        test_dirs = sorted([
            d for d in materials_dir.iterdir()
            if d.is_dir() and d.name.startswith('Test')
        ])
        if options['test']:
            test_dirs = [d for d in test_dirs if d.name == options['test']]
        if not test_dirs:
            raise CommandError('No matching test directories found.')

        for test_dir in test_dirs:
            self._ingest_test(test_dir, dry_run=options['dry_run'])

    # ------------------------------------------------------------------
    # Test-level processing  (без изменений кроме сообщений)
    # ------------------------------------------------------------------

    def _ingest_test(self, test_dir: Path, *, dry_run: bool):
        test_name     = test_dir.name
        listening_dir = test_dir / 'Listening'

        if not listening_dir.exists():
            self.stderr.write(self.style.WARNING(
                f'  ⚠  No Listening/ folder in {test_name}, skipping.'
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f'\n{"=" * 60}'))
        self.stdout.write(self.style.MIGRATE_HEADING(f'  Ingesting: {test_name}'))
        self.stdout.write(self.style.MIGRATE_HEADING(f'{"=" * 60}'))

        part_dirs = sorted(
            [d for d in listening_dir.iterdir()
             if d.is_dir() and d.name.startswith('Part')],
            key=lambda d: int(d.name.split()[-1]),
        )
        if not part_dirs:
            self.stderr.write(self.style.WARNING('  ⚠  No Part folders found.'))
            return

        if dry_run:
            self._dry_run_preview(part_dirs)
            return

        with transaction.atomic():
            test_obj, created = Test.objects.update_or_create(
                name=test_name,
                defaults={'test_type': 'listening', 'is_active': True},
            )
            if not created:
                self.stdout.write(f'  ↻  Re-ingesting "{test_name}" (clearing old data)…')
                test_obj.parts.all().delete()
            else:
                self.stdout.write(self.style.SUCCESS(f'  ✓  Created Test: {test_name}'))

            global_offset   = 0
            total_questions = 0
            for part_dir in part_dirs:
                part_num = int(part_dir.name.split()[-1])
                n = self._ingest_part(test_obj, part_dir, part_num, global_offset)
                global_offset   += n
                total_questions += n

        self.stdout.write(self.style.SUCCESS(
            f'\n  ✅  Successfully ingested {test_name}: '
            f'{total_questions} questions across {len(part_dirs)} parts.\n'
        ))

    # ------------------------------------------------------------------
    # Dry-run preview
    # ------------------------------------------------------------------

    def _dry_run_preview(self, part_dirs):
        self.stdout.write(self.style.WARNING('  [DRY RUN] No database changes.\n'))
        for pd in part_dirs:
            part_num = int(pd.name.split()[-1])
            answers  = self._parse_answers(pd / 'answers.txt')
            q_type   = PART_QUESTION_TYPES.get(part_num, '?')
            files    = [f.name for f in pd.iterdir() if f.is_file()]
            self.stdout.write(
                f'    Part {part_num}: {len(answers)} Qs ({q_type})  files={files}'
            )

    # ------------------------------------------------------------------
    # Part-level processing  (без изменений)
    # ------------------------------------------------------------------

    def _ingest_part(self, test_obj, part_dir: Path, part_num: int,
                     global_offset: int) -> int:
        self.stdout.write(f'\n  📁  Part {part_num}')

        answers = self._parse_answers(part_dir / 'answers.txt')
        if not answers:
            self.stderr.write(self.style.WARNING('    ⚠  No answers found, skipping.'))
            return 0

        q_type = PART_QUESTION_TYPES.get(part_num, 'multiple_choice')
        self.stdout.write(f'    📝  {len(answers)} answers loaded  (type={q_type})')

        # Default empty extraction result
        extraction = {
            'instructions': '',
            'passage_title': '',
            'passage_text': '',
            'shared_choices': [],
            'questions': {},
        }
        processed_img_path = None

        questions_img = part_dir / 'questions.jpg'
        if questions_img.exists() and not self.skip_ocr and self.genai_client:
            extraction, processed_img_path = self._ocr_and_parse(
                questions_img, part_num, q_type, answers, global_offset,
            )

        slug     = test_obj.name.lower().replace(' ', '_')
        part_obj = Part(
            test=test_obj,
            part_number=part_num,
            instructions=extraction['instructions'],
            passage_title=extraction['passage_title'],
            passage_text=extraction['passage_text'],
            shared_choices_json=extraction['shared_choices'],
        )

        audio = part_dir / 'audio.mp3'
        if audio.exists():
            with open(audio, 'rb') as f:
                part_obj.audio_file.save(
                    f'{slug}_part{part_num}.mp3', File(f), save=False,
                )
            self.stdout.write('    🔊  Audio attached')

        img_to_save = Path(processed_img_path) if processed_img_path else questions_img
        if img_to_save.exists():
            with open(img_to_save, 'rb') as f:
                part_obj.question_image.save(
                    f'{slug}_part{part_num}_questions.jpg', File(f), save=False,
                )
            self.stdout.write('    🖼️   Question image attached')

        map_img = part_dir / 'map.jpg'
        if map_img.exists():
            with open(map_img, 'rb') as f:
                part_obj.map_image.save(
                    f'{slug}_part{part_num}_map.jpg', File(f), save=False,
                )
            self.stdout.write('    🗺️   Map image attached')

        part_obj.save()
        self.stdout.write(f'    ⚖️   Weight: {part_obj.points_per_question} pts/question')

        if extraction['passage_title']:
            self.stdout.write(f'    📖  Passage: "{extraction["passage_title"]}"')
        if extraction['shared_choices']:
            self.stdout.write(f'    🔗  Shared choices: {len(extraction["shared_choices"])} options')

        parsed_questions = extraction['questions']

        for local_num in sorted(answers.keys()):
            global_num = global_offset + local_num
            correct    = answers[local_num]

            q_data      = parsed_questions.get(local_num, {})
            q_text      = q_data.get('text') or ''
            choices     = q_data.get('choices') or []
            group_label = q_data.get('group_label') or ''

            q_obj = Question.objects.create(
                part=part_obj,
                question_number=local_num,
                global_question_number=global_num,
                question_text=q_text,
                question_type=q_type,
                correct_answer=correct,
                group_label=group_label,
            )

            # Only create per-question Choice rows where applicable
            # (skip for Part 3 matching — those go in shared_choices_json)
            seen_labels = set()
            for label, text in choices:
                if label not in seen_labels:
                    Choice.objects.get_or_create(
                        question=q_obj, label=label,
                        defaults={'text': text},
                    )
                    seen_labels.add(label)

            extra = ''
            if choices:
                extra = f'  ({len(choices)} choices)'
            if group_label:
                extra += f'  [{group_label}]'
            self.stdout.write(
                f'      Q{global_num:>2}  →  {correct:<15}  {q_type}{extra}'
            )

        if processed_img_path and processed_img_path != str(questions_img):
            try:
                os.remove(processed_img_path)
            except OSError:
                pass

        # Perform Audio Analysis if requested and possible
        if audio.exists() and not self.skip_ocr and self.genai_client:
            self.stdout.write('    🧠  Analyzing audio for transcript and explanations...')
            self._analyze_audio_and_explain(audio, part_obj, answers, parsed_questions)

        return len(answers)

    # ------------------------------------------------------------------
    # answers.txt parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_answers(filepath: Path) -> dict:
        if not filepath.exists():
            return {}
        answers = {}
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'^(\d+)\s+(.+)$', line)
                if m:
                    answers[int(m.group(1))] = m.group(2).strip()
        return answers

    # ------------------------------------------------------------------
    # OCR pipeline  —  переписан под Gemini 3 Flash (google-genai SDK)
    # ------------------------------------------------------------------

    def _ocr_and_parse(self, image_path: Path, part_num: int,
                       q_type: str, answers: dict, global_offset: int):
        """
        Extract exam questions via Gemini 3 Flash on Vertex AI (global endpoint).
        Returns: (extraction_dict, processed_image_path)

        extraction_dict keys:
            instructions, passage_title, passage_text,
            shared_choices, questions
        """
        extraction = {
            'instructions': '',
            'passage_title': '',
            'passage_text': '',
            'shared_choices': [],
            'questions': {},
        }
        processed_path = str(image_path)

        try:
            from google.genai import types as T

            # --- 1. Pre-process image ---
            img = Image.open(image_path)
            img = self._auto_orient(img)

            rotation = PART_ROTATIONS.get(part_num, 0)
            if rotation:
                img = img.rotate(rotation, expand=True)
                tmp = image_path.parent / 'questions_rotated.jpg'
                img.save(str(tmp), 'JPEG', quality=95)
                processed_path = str(tmp)
                self.stdout.write(f'    🔄  Rotated image {rotation}°')

            with open(processed_path, 'rb') as fh:
                image_bytes = fh.read()

            # --- 2. Build content parts (новый SDK) ---
            image_part = T.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")

            # --- 3. Per-part-type prompt ---
            prompt = self._build_prompt(part_num, q_type)

            # --- 4. Thinking config (Gemini 3) ---
            thinking_cfg = T.ThinkingConfig(thinking_level=self.thinking_level)

            generate_cfg = T.GenerateContentConfig(
                thinking_config=thinking_cfg,
                response_mime_type="application/json",
            )

            # --- 5. Call API ---
            self.stdout.write(
                f'    🔍  Calling {GEMINI_MODEL} '
                f'(thinking={self.thinking_level})…'
            )

            response = self.genai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[image_part, prompt],
                config=generate_cfg,
            )

            raw_text = response.text.strip()

            # Защитная зачистка на случай непрошенных markdown-блоков
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.startswith("```"):
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()

            # --- 6. Parse JSON ---
            data = json.loads(raw_text)
            extraction['instructions'] = data.get('instructions', '').strip()

            # Part-type-specific fields
            extraction['passage_title'] = data.get('passage_title', '').strip()
            extraction['passage_text'] = data.get('passage_text', '').strip()
            extraction['shared_choices'] = data.get('shared_choices', [])

            extracted_qs = data.get('questions', [])
            self.stdout.write(
                f'    📄  Gemini 3 Flash extracted {len(extracted_qs)} questions.'
            )

            # Re-index from exam-page numbers to local 1-based indices
            valid_qs = []
            for q in extracted_qs:
                raw_num = q.get('number')
                if raw_num is None:
                    continue
                try:
                    raw_num = int(raw_num)
                except (ValueError, TypeError):
                    continue
                valid_qs.append((raw_num, q))

            valid_qs.sort(key=lambda t: t[0])

            for local_idx, (_, q) in enumerate(valid_qs, start=1):
                choices = q.get('choices') or []
                extraction['questions'][local_idx] = {
                    'text':        q.get('text') or '',
                    'choices':     [(c.get('label', ''), c.get('text', ''))
                                    for c in choices],
                    'group_label': q.get('group_label') or '',
                }

        except Exception as e:
            self.stderr.write(self.style.WARNING(f'    ⚠  Gemini 3 Flash error: {e}'))

        return extraction, processed_path

    # ------------------------------------------------------------------
    # Per-part-type prompt builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(part_num: int, q_type: str) -> str:
        """Build a part-type-specific prompt for Gemini."""

        base_rules = (
            'Act as an expert English CEFR exam parser.\n'
            f'Analyze this exam page for Part {part_num} of a listening test.\n'
            'Ignore ALL page numbers and extraneous headers '
            f'(like "TEST 11", "PART {part_num}", or "Questions X-Y").\n'
        )

        # ---- Part 1: Standard multiple choice (A/B/C per question) ----
        if part_num == 1:
            return base_rules + """
Extract the instructions block and all 8 questions with their A/B/C choices.

Return STRICTLY a JSON object (no markdown fences):
{
    "instructions": "Instructions text",
    "questions": [
        {
            "number": 1,
            "text": "",
            "choices": [
                {"label": "A", "text": "OK, so down the stairs..."},
                {"label": "B", "text": "That sounds lovely."},
                {"label": "C", "text": "We really must go."}
            ]
        }
    ]
}
Note: Part 1 questions are audio-based with no written question text, so "text" should be empty string."""

        # ---- Part 2: Fill-in-blank with passage ----
        if part_num == 2:
            return base_rules + """
This is a NOTE COMPLETION / fill-in-the-blank part with a titled passage.
The page contains:
1. Instructions (e.g. "You will hear someone giving a talk...")
2. A PASSAGE TITLE (e.g. "Working in a Forest in New Zealand")
3. A continuous passage with numbered blanks (9, 10, 11, ...)

Extract the COMPLETE passage text preserving its natural flow.
Mark each blank as {N} where N is the question number printed on the page.

Return STRICTLY a JSON object (no markdown fences):
{
    "instructions": "You will hear someone giving a talk...",
    "passage_title": "Working in a Forest in New Zealand",
    "passage_text": "Steve had accommodation in a shared {9}. Steve says it was important to have good {10} at the end of each day. ...",
    "questions": [
        {"number": 9,  "text": "Steve had accommodation in a shared _____", "choices": []},
        {"number": 10, "text": "Steve says it was important to have good _____ at the end of each day.", "choices": []}
    ]
}
IMPORTANT: passage_text must contain the FULL passage with ALL sentences, not just the blank lines."""

        # ---- Part 3: Matching speakers to shared choices A-F ----
        if part_num == 3:
            return base_rules + """
This is a MATCHING part. There are 4 speakers and a SHARED list of reasons/options A-F.
The student matches each speaker to one option. Two options are extras (not used).

The choices A-F belong to the PART, not to individual questions.

Return STRICTLY a JSON object (no markdown fences):
{
    "instructions": "You will hear people apologising about something...",
    "shared_choices": [
        {"label": "A", "text": "disturbing someone"},
        {"label": "B", "text": "cancelling a theatre booking"},
        {"label": "C", "text": "leaving something behind"},
        {"label": "D", "text": "forgetting to write something down"},
        {"label": "E", "text": "arriving very late"},
        {"label": "F", "text": "dropping something"}
    ],
    "questions": [
        {"number": 15, "text": "Speaker 1", "choices": [], "group_label": "Speaker 1"},
        {"number": 16, "text": "Speaker 2", "choices": [], "group_label": "Speaker 2"},
        {"number": 17, "text": "Speaker 3", "choices": [], "group_label": "Speaker 3"},
        {"number": 18, "text": "Speaker 4", "choices": [], "group_label": "Speaker 4"}
    ]
}
IMPORTANT: Do NOT put choices inside individual questions. They go in "shared_choices"."""

        # ---- Part 4: Map labeling ----
        if part_num == 4:
            return base_rules + """
This is a MAP LABELING part. The student matches location names to letters on a map.
Extract each question number and the location name.

Return STRICTLY a JSON object (no markdown fences):
{
    "instructions": "",
    "questions": [
        {"number": 19, "text": "Box Office", "choices": []},
        {"number": 20, "text": "Children's Room", "choices": []}
    ]
}"""

        # ---- Part 5: Multiple choice with Extract grouping ----
        if part_num == 5:
            return base_rules + """
This is a MULTIPLE CHOICE part with THREE separate "Extracts".
Each Extract has 2 questions with A/B/C choices.

Return STRICTLY a JSON object (no markdown fences):
{
    "instructions": "You will hear three extracts...",
    "questions": [
        {
            "number": 24,
            "text": "What is the man's reaction to the majority of visiting birdwatchers?",
            "group_label": "Extract One",
            "choices": [
                {"label": "A", "text": "He thinks they drive too fast."},
                {"label": "B", "text": "He believes they are ignorant..."},
                {"label": "C", "text": "He doesn't understand why..."}
            ]
        }
    ]
}
IMPORTANT: Include "group_label" for EVERY question (Extract One, Extract Two, or Extract Three)."""

        # ---- Part 6: Fill-in-blank with passage (like Part 2) ----
        if part_num == 6:
            return base_rules + """
This is a LECTURE SUMMARY / fill-in-the-blank part with a titled passage.
The page contains:
1. Instructions (e.g. "You will hear a part of a lecture...")
2. A PASSAGE TITLE (e.g. "BRITISH MARINE LIFE IN CRISIS")
3. A continuous passage with numbered blanks (30, 31, 32, ...)

Extract the COMPLETE passage text preserving its natural flow.
Mark each blank as {N} where N is the question number.

Return STRICTLY a JSON object (no markdown fences):
{
    "instructions": "You will hear a part of a lecture...",
    "passage_title": "British Marine Life in Crisis",
    "passage_text": "Pollution, coastal developments and {30} are the conventional threats to marine life. Pink coral is most in danger along with turtles, sharks and salmon. {31} passed by the UK and EU protects some areas of UK waters. ...",
    "questions": [
        {"number": 30, "text": "Pollution, coastal developments and _____ are the conventional threats to marine life.", "choices": []},
        {"number": 31, "text": "_____ passed by the UK and EU protects some areas of UK waters.", "choices": []}
    ]
}
IMPORTANT: passage_text must contain the FULL passage with ALL sentences, not just the blank lines."""

        # ---- Fallback for unknown parts ----
        return base_rules + f"""
Extract all questions. Question type is '{q_type}'.
Return STRICTLY a JSON object (no markdown fences):
{{
    "instructions": "...",
    "questions": [
        {{"number": 1, "text": "...", "choices": [{{"label": "A", "text": "..."}}]}}
    ]
}}
If a question has no choices, use an empty list for "choices"."""

    # ------------------------------------------------------------------

    @staticmethod
    def _auto_orient(img: Image.Image) -> Image.Image:
        try:
            exif = img._getexif()
            if exif:
                orient_key = next(
                    (k for k, v in ExifTags.TAGS.items() if v == 'Orientation'), None
                )
                if orient_key and orient_key in exif:
                    rotations = {3: 180, 6: 270, 8: 90}
                    angle = rotations.get(exif[orient_key])
                    if angle:
                        img = img.rotate(angle, expand=True)
        except Exception:
            pass
        return img

    # ------------------------------------------------------------------
    # Audio Analysis Pipeline
    # ------------------------------------------------------------------

    def _analyze_audio_and_explain(self, audio_path: Path, part_obj: Part, answers: dict, parsed_questions: dict):
        """
        Extract exam transcript and explanations via Gemini 3 Flash on Vertex AI (global endpoint)
        using inline audio bytes.
        """
        try:
            from google.genai import types as T

            with open(audio_path, 'rb') as f:
                audio_bytes = f.read()

            audio_part = T.Part.from_bytes(data=audio_bytes, mime_type="audio/mp3")

            q_list_str = []
            for local_num, correct in answers.items():
                q_data = parsed_questions.get(local_num, {})
                q_text = q_data.get('text', '')
                choices = q_data.get('choices', [])
                group_label = q_data.get('group_label', '')
                
                # Retrieve the actual question object to get its global_question_number
                q_obj = part_obj.questions.get(question_number=local_num)
                global_num = q_obj.global_question_number

                details = f"Question {global_num}: "
                if group_label:
                    details += f"[{group_label}] "
                if q_text:
                    details += f"{q_text} "
                if choices:
                    details += f"(Choices: {choices}) "
                details += f"-> Correct Answer: {correct}"
                q_list_str.append(details)

            prompt = (
                "Listen to the attached audio track for this exam part.\n"
                "Provide the full transcript of the audio.\n"
                "For each question provided, write a short explanation of why the given correct answer is right. "
                "Quote the specific part of the transcript that proves it.\n\n"
                "Questions and correct answers:\n"
                + "\n".join(q_list_str) + "\n\n"
                "Request the output STRICTLY as JSON:\n"
                "{\n"
                '    "transcript": "Full text of the audio...",\n'
                '    "explanations": {\n'
                '        "1": "The answer is A because...",\n'
                '        "2": "The answer is C because..."\n'
                "    }\n"
                "}"
            )

            thinking_cfg = T.ThinkingConfig(thinking_level=self.thinking_level)
            generate_cfg = T.GenerateContentConfig(
                thinking_config=thinking_cfg,
                response_mime_type="application/json",
            )

            self.stdout.write(
                f'    🔍  Calling {GEMINI_MODEL} for Audio '
                f'(thinking={self.thinking_level})…'
            )

            response = self.genai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[audio_part, prompt],
                config=generate_cfg,
            )

            raw_text = response.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.startswith("```"):
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()

            data = json.loads(raw_text)

            # Save the transcript
            part_obj.transcript = data.get('transcript', '')
            part_obj.save()

            # Save explanations to questions
            explanations = data.get('explanations', {})
            for q_obj in part_obj.questions.all():
                q_num_str = str(q_obj.global_question_number)
                expl = explanations.get(q_num_str)
                if expl:
                    q_obj.explanation = expl
                    q_obj.save()

            self.stdout.write(self.style.SUCCESS(f'    ✅  Audio analysis & explanations saved.'))

        except Exception as e:
            self.stderr.write(self.style.WARNING(f'    ⚠  Audio analysis failed: {e}'))