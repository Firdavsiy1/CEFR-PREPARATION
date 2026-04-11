"""
Management command to ingest CEFR exam materials from the filesystem.

Scans the ``materials/`` directory for Test folders, processes each Part's
audio, images, and answer keys, runs OCR on question screenshots, and
populates the database.

Usage::

    python manage.py ingest_materials
    python manage.py ingest_materials --test "Test 1"
    python manage.py ingest_materials --dry-run
    python manage.py ingest_materials --skip-ocr

Requirements:
    pip install Pillow pytesseract   (or tesserocr)
    Tesseract traineddata in ``<project>/tessdata/eng.traineddata``
"""

import os
import re
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from PIL import Image, ExifTags

from exams.models import Test, Part, Question, Choice

# ---------------------------------------------------------------------------
# Per-part configuration for the CEFR Listening exam
# ---------------------------------------------------------------------------

PART_QUESTION_TYPES = {
    1: 'multiple_choice',
    2: 'fill_blank',
    3: 'multiple_choice',   # matching speakers → reasons (A-F)
    4: 'map_label',
    5: 'multiple_choice',
    6: 'fill_blank',
}

# Rotation to apply BEFORE OCR (degrees, PIL counter-clockwise)
# Determined empirically from the scanned question images.
PART_ROTATIONS = {
    2: 270,
    3: 270,
    4: 270,
    5: 180,
}

# Tessdata path relative to BASE_DIR
TESSDATA_DIR = 'tessdata'


# ---------------------------------------------------------------------------
# OCR backend abstraction
# ---------------------------------------------------------------------------

def _get_ocr_func(tessdata_path: str):
    """
    Return an OCR callable ``(pil_image) -> str`` using whichever
    backend is available: tesserocr (bundled engine) or pytesseract
    (requires system tesseract binary).
    """
    # Prefer tesserocr (no system binary needed)
    try:
        import tesserocr
        def _ocr(img):
            return tesserocr.image_to_text(img, path=tessdata_path, lang='eng')
        # Quick smoke test
        _ocr(Image.new('RGB', (10, 10)))
        return _ocr
    except Exception:
        pass

    # Fallback to pytesseract
    try:
        import pytesseract
        def _ocr(img):
            return pytesseract.image_to_string(img, lang='eng')
        _ocr(Image.new('RGB', (10, 10)))
        return _ocr
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = 'Ingest CEFR exam materials from the materials/ directory.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test', type=str, default=None,
            help='Ingest only a specific test (e.g. "Test 1").',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Preview what would be ingested without writing to the DB.',
        )
        parser.add_argument(
            '--skip-ocr', action='store_true',
            help='Skip OCR — create records from answers.txt only.',
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        self.skip_ocr = options['skip_ocr']
        self.ocr_func = None

        if not self.skip_ocr:
            tessdata_path = str(settings.BASE_DIR / TESSDATA_DIR)
            self.ocr_func = _get_ocr_func(tessdata_path)
            if self.ocr_func is None:
                raise CommandError(
                    'No OCR backend available.\n'
                    '  Install:  pip install tesserocr   (recommended, no system dep)\n'
                    '            pip install pytesseract + apt install tesseract-ocr\n'
                    '  And place eng.traineddata in:  tessdata/\n'
                    '  Or run with:  --skip-ocr'
                )

        materials_dir = settings.BASE_DIR / 'materials'
        if not materials_dir.exists():
            raise CommandError(f'Materials directory not found: {materials_dir}')

        # Discover test directories
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
    # Test-level processing
    # ------------------------------------------------------------------

    def _ingest_test(self, test_dir: Path, *, dry_run: bool):
        test_name = test_dir.name
        listening_dir = test_dir / 'Listening'

        if not listening_dir.exists():
            self.stderr.write(self.style.WARNING(
                f'  ⚠  No Listening/ folder in {test_name}, skipping.',
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

        # --- Dry run ---
        if dry_run:
            self._dry_run_preview(part_dirs)
            return

        # --- Real ingestion inside an atomic transaction ---
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

            global_offset = 0
            total_questions = 0

            for part_dir in part_dirs:
                part_num = int(part_dir.name.split()[-1])
                n = self._ingest_part(test_obj, part_dir, part_num, global_offset)
                global_offset += n
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
            answers = self._parse_answers(pd / 'answers.txt')
            q_type = PART_QUESTION_TYPES.get(part_num, '?')
            files = [f.name for f in pd.iterdir() if f.is_file()]
            self.stdout.write(
                f'    Part {part_num}: {len(answers)} Qs ({q_type})  '
                f'files={files}'
            )

    # ------------------------------------------------------------------
    # Part-level processing
    # ------------------------------------------------------------------

    def _ingest_part(self, test_obj, part_dir: Path, part_num: int,
                     global_offset: int) -> int:
        """Ingest a single Part directory. Returns question count."""
        self.stdout.write(f'\n  📁  Part {part_num}')

        # 1. Parse answers.txt — this is the source of truth
        answers = self._parse_answers(part_dir / 'answers.txt')
        if not answers:
            self.stderr.write(self.style.WARNING('    ⚠  No answers found, skipping.'))
            return 0

        q_type = PART_QUESTION_TYPES.get(part_num, 'multiple_choice')
        self.stdout.write(f'    📝  {len(answers)} answers loaded  (type={q_type})')

        # 2. OCR processing
        instructions = ''
        parsed_questions = {}
        processed_img_path = None

        questions_img = part_dir / 'questions.jpg'
        if questions_img.exists() and not self.skip_ocr and self.ocr_func:
            instructions, parsed_questions, processed_img_path = (
                self._ocr_and_parse(
                    questions_img, part_num, q_type, answers, global_offset,
                )
            )

        # 3. Create Part record
        slug = test_obj.name.lower().replace(' ', '_')
        part_obj = Part(
            test=test_obj,
            part_number=part_num,
            instructions=instructions,
        )

        # Attach audio
        audio = part_dir / 'audio.mp3'
        if audio.exists():
            with open(audio, 'rb') as f:
                part_obj.audio_file.save(
                    f'{slug}_part{part_num}.mp3', File(f), save=False,
                )
            self.stdout.write('    🔊  Audio attached')

        # Attach question image (rotated version if applicable)
        img_to_save = (
            Path(processed_img_path) if processed_img_path
            else questions_img
        )
        if img_to_save.exists():
            with open(img_to_save, 'rb') as f:
                part_obj.question_image.save(
                    f'{slug}_part{part_num}_questions.jpg', File(f), save=False,
                )
            self.stdout.write('    🖼️   Question image attached')

        # Attach map image (e.g. Part 4)
        map_img = part_dir / 'map.jpg'
        if map_img.exists():
            with open(map_img, 'rb') as f:
                part_obj.map_image.save(
                    f'{slug}_part{part_num}_map.jpg', File(f), save=False,
                )
            self.stdout.write('    🗺️   Map image attached')

        part_obj.save()  # triggers auto-weight in Part.save()
        self.stdout.write(f'    ⚖️   Weight: {part_obj.points_per_question} pts/question')

        # 4. Create Question (+ Choice) records
        for local_num in sorted(answers.keys()):
            global_num = global_offset + local_num
            correct = answers[local_num]

            q_data = parsed_questions.get(local_num, {})
            q_text = q_data.get('text', '')
            choices = q_data.get('choices', [])

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

        # Clean up temp rotated image
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
        """Parse ``answers.txt`` → ``{local_number: correct_answer}``."""
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
    # OCR pipeline
    # ------------------------------------------------------------------

    def _ocr_and_parse(self, image_path: Path, part_num: int,
                       q_type: str, answers: dict,
                       global_offset: int):
        """
        OCR a question image and parse structured question data.

        Returns: (instructions, parsed_questions, processed_image_path)
        """
        instructions = ''
        parsed = {}
        processed_path = str(image_path)

        try:
            img = Image.open(image_path)
            img = self._auto_orient(img)

            # Apply known rotation for this part
            rotation = PART_ROTATIONS.get(part_num, 0)
            if rotation:
                img = img.rotate(rotation, expand=True)
                # Save rotated version for the media copy
                tmp = image_path.parent / 'questions_rotated.jpg'
                img.save(str(tmp), 'JPEG', quality=95)
                processed_path = str(tmp)
                self.stdout.write(f'    🔄  Rotated image {rotation}°')

            # Run OCR
            self.stdout.write('    🔍  Running OCR…')
            raw = self.ocr_func(img)

            if not raw or not raw.strip():
                self.stderr.write(self.style.WARNING('    ⚠  OCR returned empty text'))
                return instructions, parsed, processed_path

            self.stdout.write(f'    📄  OCR extracted {len(raw)} characters')
            instructions = raw.strip()

            # Parse per question type
            if q_type == 'multiple_choice':
                if part_num == 3:
                    parsed = self._parse_matching(raw, answers)
                else:
                    parsed = self._parse_mc(raw, answers, global_offset)
            elif q_type == 'map_label':
                parsed = self._parse_map_label(raw, answers, global_offset)
            elif q_type == 'fill_blank':
                parsed = self._parse_fill_blank(raw, answers, global_offset)

        except Exception as e:
            self.stderr.write(self.style.WARNING(f'    ⚠  OCR error: {e}'))

        return instructions, parsed, processed_path

    @staticmethod
    def _auto_orient(img: Image.Image) -> Image.Image:
        """Apply EXIF orientation tag if present."""
        try:
            exif = img._getexif()
            if exif:
                orient_key = next(
                    (k for k, v in ExifTags.TAGS.items() if v == 'Orientation'),
                    None,
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
    # Question parsers  (best-effort — admin is the safety net)
    # ------------------------------------------------------------------

    def _parse_mc(self, raw: str, answers: dict, global_offset: int) -> dict:
        """
        Parse standard multiple-choice questions (Parts 1 & 5).

        Looks for patterns like::

            24  What is the man's …?
            A  He thinks …
            B  He believes …
            C  He doesn't …
        """
        parsed = {}

        for local_num in answers:
            global_num = global_offset + local_num
            block = self._extract_question_block(raw, global_num)
            q_text = ''
            choices = []

            if block:
                # Extract A/B/C choices — deduplicate by label
                choice_matches = re.findall(
                    r'^[ \t]*([A-C])\)?\s+(.+)$', block, re.MULTILINE,
                )
                seen_labels = set()
                for label, text in choice_matches:
                    if label not in seen_labels:
                        choices.append((label, text.strip()))
                        seen_labels.add(label)

                # Question text = first line(s) before the first choice
                first_choice = re.search(r'^[ \t]*[A-C]\)?[ \t]+', block, re.MULTILINE)
                if first_choice:
                    q_text = block[:first_choice.start()].strip()
                    # Remove leading question number
                    q_text = re.sub(r'^\d+\.?\s*', '', q_text).strip()

            parsed[local_num] = {'text': q_text, 'choices': choices}

        return parsed

    def _parse_matching(self, raw: str, answers: dict) -> dict:
        """
        Parse matching questions (Part 3 — speakers to reasons A–F).

        The same set of choices (A–F) applies to all questions.
        Question text is "Speaker N".
        """
        parsed = {}

        # Extract options A through F
        option_matches = re.findall(
            r'^[ \t]*([A-F])\s+(.+?)$', raw, re.MULTILINE,
        )
        # Deduplicate by label (OCR may produce duplicates)
        seen = set()
        all_choices = []
        for label, text in option_matches:
            if label not in seen:
                all_choices.append((label, text.strip()))
                seen.add(label)

        for local_num in answers:
            parsed[local_num] = {
                'text': f'Speaker {local_num}',
                'choices': list(all_choices),  # same choices for all
            }

        return parsed

    def _parse_map_label(self, raw: str, answers: dict,
                         global_offset: int) -> dict:
        """
        Parse map-labeling questions (Part 4).

        Extracts the location name for each question number.
        No text choices — the map image provides the visual options.
        """
        parsed = {}

        for local_num in answers:
            global_num = global_offset + local_num
            # Pattern: "19  Box Office ..."
            match = re.search(
                rf'(?:^|\n)\s*{global_num}\s+(.+?)(?:\s*[.…]+\s*$|\s*$)',
                raw, re.MULTILINE,
            )
            label = match.group(1).strip() if match else f'Location {local_num}'
            # Clean OCR artifacts from the label
            label = re.sub(r'[.…]+$', '', label).strip()

            parsed[local_num] = {'text': label, 'choices': []}

        return parsed

    def _parse_fill_blank(self, raw: str, answers: dict,
                          global_offset: int) -> dict:
        """
        Parse fill-in-the-blank questions (Parts 2 & 6).

        Extracts the sentence context around each numbered blank.
        """
        parsed = {}

        for local_num in answers:
            global_num = global_offset + local_num
            context = self._extract_blank_context(raw, global_num)
            parsed[local_num] = {'text': context, 'choices': []}

        return parsed

    # ------------------------------------------------------------------
    # Text extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_question_block(raw: str, q_num: int) -> str | None:
        """Extract the text block for question ``q_num`` from raw OCR."""
        # Find start: "24 " or "24."
        pattern = rf'(?:^|\n)\s*{q_num}\.?\s+'
        match = re.search(pattern, raw)
        if not match:
            return None

        start = match.start()

        # Find start of *next* question
        next_pattern = rf'(?:^|\n)\s*{q_num + 1}\.?\s+'
        next_match = re.search(next_pattern, raw[match.end():])

        if next_match:
            end = match.end() + next_match.start()
        else:
            end = len(raw)

        return raw[start:end].strip()

    @staticmethod
    def _extract_blank_context(raw: str, q_num: int) -> str:
        """Extract sentence context around a numbered blank."""
        # "… and 30 ............. are the conventional …"
        pattern = rf'(.{{0,100}})\b{q_num}\b\s*[.…]+\s*(.{{0,100}})'
        match = re.search(pattern, raw, re.DOTALL)
        if match:
            before = match.group(1).strip()
            after = match.group(2).strip()
            # Clean up OCR noise
            before = re.sub(r'\n+', ' ', before)
            after = re.sub(r'\n+', ' ', after)
            return f'{before} _____ {after}'.strip()

        # Fallback: find the line mentioning this number
        for line in raw.split('\n'):
            if re.search(rf'\b{q_num}\b', line):
                return line.strip()

        return ''
