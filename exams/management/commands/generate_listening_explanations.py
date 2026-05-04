"""
Management command: generate_listening_explanations

Generates (or regenerates) AI explanations for Listening Question objects
using the stored Part transcript + question data. Does NOT require the
original audio file.

Usage:
    python manage.py generate_listening_explanations
    python manage.py generate_listening_explanations --test "Listening test #1"
    python manage.py generate_listening_explanations --force          # regenerate ALL
    python manage.py generate_listening_explanations --test "T1" --force
"""

import json
import logging
import os

from django.core.management.base import BaseCommand, CommandError

from exams.models import Part, Test
from exams.reading_services import _build_gemini_client, GEMINI_MODEL

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Generate detailed AI explanations for Listening Questions using stored transcripts.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test',
            type=str,
            default=None,
            help='Name of a specific listening test to process (default: all listening tests).',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Regenerate explanations even for questions that already have one.',
        )

    def handle(self, *args, **options):
        test_name = options.get('test')
        force = options.get('force', False)

        qs = Test.objects.filter(test_type='listening')
        if test_name:
            qs = qs.filter(name__icontains=test_name)
            if not qs.exists():
                raise CommandError(f'No listening test found matching: "{test_name}"')

        if force:
            self.stdout.write(self.style.WARNING(
                '--force: regenerating ALL explanations (including existing ones).'
            ))

        client = _build_gemini_client()
        total_updated = 0

        for test in qs:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\nProcessing: {test.name}'))
            parts = test.parts.prefetch_related('questions').order_by('part_number')

            for part in parts:
                questions = list(
                    part.questions.all().order_by('question_number')
                    if force
                    else part.questions.filter(explanation__in=['', None]).order_by('question_number')
                )
                if not questions:
                    self.stdout.write(
                        f'  Part {part.part_number}: all explanations already present, skipping.'
                    )
                    continue

                self.stdout.write(
                    f'  Part {part.part_number}: generating explanations for {len(questions)} question(s)...'
                )

                transcript = part.transcript.strip() if part.transcript else ''
                if not transcript:
                    self.stdout.write(
                        self.style.WARNING(
                            f'    ⚠  Part {part.part_number}: no transcript stored — skipping.'
                            ' (Re-ingest the test to generate the transcript.)'
                        )
                    )
                    continue

                q_lines = []
                for q in questions:
                    global_num = q.global_question_number or q.question_number
                    line = f'Q{global_num} ({q.question_type})'
                    if q.question_text:
                        line += f': {q.question_text}'
                    line += f' → Correct answer: {q.correct_answer}'
                    if hasattr(q, 'choices') and q.choices.exists():
                        choices_str = ', '.join(
                            f'{c.label}) {c.text}' for c in q.choices.all()
                        )
                        line += f'  Choices: [{choices_str}]'
                    q_lines.append(line)

                prompt = (
                    f'You are an expert CEFR Listening exam tutor.\n'
                    f'Below is the full transcript of a listening audio (Part {part.part_number}) '
                    f'followed by a list of exam questions and their correct answers.\n\n'
                    f'TRANSCRIPT:\n{transcript}\n\n'
                    f'QUESTIONS AND CORRECT ANSWERS:\n'
                    + '\n'.join(q_lines)
                    + '\n\n'
                    'For EACH question number, write a detailed tutor-style explanation (4-6 sentences). '
                    'Structure each explanation as follows:\n'
                    '1. State what the correct answer is and directly explain why it is correct.\n'
                    '2. Quote the specific part of the transcript that proves it.\n'
                    '3. Explain why the other options are NOT correct (if applicable).\n'
                    '4. Mention any key vocabulary or audio cue (tone, stress, speaker context) '
                    'that helps a student identify the answer.\n\n'
                    'Return ONLY valid JSON in this exact shape (no markdown fences):\n'
                    '{"explanations": {"<question_number>": "<detailed explanation text>", ...}}\n'
                    'Use the exact global question number integers as keys.'
                )

                try:
                    from google.genai import types as genai_types

                    config = genai_types.GenerateContentConfig(
                        response_mime_type='application/json',
                    )
                    response = client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=prompt,
                        config=config,
                    )
                    raw = (response.text or '').strip()
                    if raw.startswith('```'):
                        raw = raw.split('```', 2)[1]
                        if raw.startswith('json'):
                            raw = raw[4:]
                        raw = raw.rstrip('`').strip()

                    data = json.loads(raw)
                    explanations: dict = data.get('explanations', {})

                    self.stdout.write(
                        f'    Gemini returned {len(explanations)} explanation(s).'
                    )

                    updated_in_part = 0
                    for q in questions:
                        global_num = q.global_question_number or q.question_number
                        expl = (
                            explanations.get(str(global_num))
                            or explanations.get(global_num)
                        )
                        if expl:
                            q.explanation = str(expl).strip()
                            q.save(update_fields=['explanation'])
                            updated_in_part += 1

                    total_updated += updated_in_part
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'    ✓  Part {part.part_number}: {updated_in_part} explanation(s) saved.'
                        )
                    )

                except Exception as exc:
                    self.stderr.write(
                        self.style.ERROR(f'    ✗  Part {part.part_number} failed: {exc}')
                    )
                    logger.exception('Explanation generation failed for Part %d', part.part_number)

        self.stdout.write(self.style.SUCCESS(f'\nDone. Total explanations updated: {total_updated}'))
