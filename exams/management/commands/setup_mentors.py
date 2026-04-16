"""
Management command to set up the Mentors group and optionally
add users to it.

Usage:
    python manage.py setup_mentors                # Just create the group
    python manage.py setup_mentors --add admin     # Add user 'admin' to Mentors
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = 'Create the Mentors group and optionally add users to it.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--add', type=str, nargs='*', default=[],
            help='Usernames to add to the Mentors group.',
        )

    def handle(self, *args, **options):
        group, created = Group.objects.get_or_create(name='Mentors')
        if created:
            self.stdout.write(self.style.SUCCESS('✓ Created "Mentors" group.'))
        else:
            self.stdout.write('  "Mentors" group already exists.')

        for username in options['add']:
            try:
                user = User.objects.get(username=username)
                user.groups.add(group)
                self.stdout.write(self.style.SUCCESS(
                    f'✓ Added "{username}" to Mentors group.'
                ))
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(
                    f'✗ User "{username}" not found.'
                ))
