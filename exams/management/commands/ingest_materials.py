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

        instructions     = ''
        parsed_questions = {}
        processed_img_path = None

        questions_img = part_dir / 'questions.jpg'
        if questions_img.exists() and not self.skip_ocr and self.genai_client:
            instructions, parsed_questions, processed_img_path = self._ocr_and_parse(
                questions_img, part_num, q_type, answers, global_offset,
            )

        slug     = test_obj.name.lower().replace(' ', '_')
        part_obj = Part(
            test=test_obj,
            part_number=part_num,
            instructions=instructions,
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

        for local_num in sorted(answers.keys()):
            global_num = global_offset + local_num
            correct    = answers[local_num]

            q_data  = parsed_questions.get(local_num, {})
            q_text  = q_data.get('text') or ''
            choices = q_data.get('choices') or []

            # If there's no question text (e.g. Part 1 audio), display the choices
            # so the admin UI isn't mysteriously blank.
            if not q_text.strip() and choices:
                q_text = "\n".join(f"{label}) {text}" for label, text in choices)

            q_obj = Question.objects.create(
                part=part_obj,
                question_number=local_num,
                global_question_number=global_num,
                question_text=q_text,
                question_type=q_type,
                correct_answer=correct,
            )

            seen_labels = set()
            for label, text in choices:
                if label not in seen_labels:
                    Choice.objects.get_or_create(
                        question=q_obj, label=label,
                        defaults={'text': text},
                    )
                    seen_labels.add(label)

            choice_info = f'  ({len(choices)} choices)' if choices else ''
            self.stdout.write(
                f'      Q{global_num:>2}  →  {correct:<15}  {q_type}{choice_info}'
            )

        if processed_img_path and processed_img_path != str(questions_img):
            try:
                os.remove(processed_img_path)
            except OSError:
                pass

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
        Returns: (instructions, parsed_questions, processed_image_path)
        """
        instructions   = ''
        parsed         = {}
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

            prompt = f"""Act as an expert English CEFR exam parser.
Analyze this exam page for Part {part_num} of a listening test. The question type is '{q_type}'.

Ignore all page numbers and extraneous headers (like "TEST 11" or "PART {part_num}").
Extract the main instructions block.
Extract all questions.
- For multiple_choice: extract the question text and all choices (A, B, C …).
- For fill_blank: extract the sentence context with the blank rendered as '_____'.
- For map_label: extract the location name.

Return STRICTLY a JSON object with this exact structure, nothing else
(no markdown fences, no preamble):
{{
    "instructions": "Instructions text",
    "questions": [
        {{
            "number": 1,
            "text": "Question text or sentence with blank",
            "choices": [
                {{"label": "A", "text": "Choice A text"}},
                {{"label": "B", "text": "Choice B text"}}
            ]
        }}
    ]
}}
If a question has no choices, return an empty list [] for "choices"."""

            # --- 3. Thinking config (новый параметр Gemini 3) ---
            thinking_cfg = T.ThinkingConfig(thinking_level=self.thinking_level)
            # thinking_level: MINIMAL | LOW | MEDIUM | HIGH
            # MINIMAL = почти нулевой бюджет, максимальная скорость — идеально для OCR

            generate_cfg = T.GenerateContentConfig(
                thinking_config=thinking_cfg,
                response_mime_type="application/json",   # просим JSON напрямую
            )

            # --- 4. Call API ---
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

            # --- 5. Parse ---
            data         = json.loads(raw_text)
            instructions = data.get("instructions", "").strip()
            extracted_qs = data.get("questions", [])

            self.stdout.write(
                f'    📄  Gemini 3 Flash extracted {len(extracted_qs)} questions.'
            )

            # Gemini returns exam-page numbers (e.g. 9,10,11 for Part 2).
            # answers.txt always uses local 1-based indices (1,2,3…).
            # Sort by exam number and re-index to 1,2,3… so the lookup matches.
            valid_qs = []
            for q in extracted_qs:
                raw_num = q.get("number")
                if raw_num is None:
                    continue
                try:
                    raw_num = int(raw_num)
                except (ValueError, TypeError):
                    continue
                valid_qs.append((raw_num, q))

            valid_qs.sort(key=lambda t: t[0])   # sort by exam-page number

            for local_idx, (_, q) in enumerate(valid_qs, start=1):
                choices = q.get("choices") or []
                parsed[local_idx] = {
                    'text':    q.get("text") or "",
                    'choices': [(c.get("label") or "", c.get("text") or "")
                                for c in choices],
                }

        except Exception as e:
            self.stderr.write(self.style.WARNING(f'    ⚠  Gemini 3 Flash error: {e}'))

        return instructions, parsed, processed_path

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