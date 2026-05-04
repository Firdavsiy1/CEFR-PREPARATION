"""
Management command: generate_reading_explanations

Generates AI explanations for ReadingQuestion objects that currently have an
empty ``explanation`` field.

Usage:
    python manage.py generate_reading_explanations
    python manage.py generate_reading_explanations --test "reading test"
    python manage.py generate_reading_explanations --force          # regenerate ALL explanations
    python manage.py generate_reading_explanations --test "T1" --force
"""

from django.core.management.base import BaseCommand, CommandError

from exams.models import Test
from exams.reading_services import generate_reading_explanations


class Command(BaseCommand):
    help = 'Generate AI explanations for ReadingQuestions with empty explanation field.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test',
            type=str,
            default=None,
            help='Name of a specific reading test to process (default: all reading tests).',
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

        qs = Test.objects.filter(test_type='reading')
        if test_name:
            qs = qs.filter(name=test_name)
            if not qs.exists():
                raise CommandError(f'No reading test found with name: "{test_name}"')

        if force:
            self.stdout.write(self.style.WARNING('--force: regenerating ALL explanations (including existing ones).'))

        for test in qs:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\nProcessing: {test.name}'))
            try:
                n = generate_reading_explanations(test, stdout=self.stdout, force=force)
                self.stdout.write(self.style.SUCCESS(f'  ✓  {n} explanations generated/updated.'))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  ✗  Failed: {e}'))
