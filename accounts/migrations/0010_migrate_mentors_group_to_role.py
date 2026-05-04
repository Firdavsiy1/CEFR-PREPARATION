"""
Data migration: Set role='mentor' for users in the 'Mentors' group.
All other users default to 'student' via the model default.
"""
from django.db import migrations


def migrate_mentors_to_role(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    Group = apps.get_model('auth', 'Group')
    UserProfile = apps.get_model('accounts', 'UserProfile')

    try:
        mentors_group = Group.objects.get(name='Mentors')
    except Group.DoesNotExist:
        return  # No mentors group — nothing to do

    mentor_user_ids = mentors_group.user_set.values_list('id', flat=True)
    updated = UserProfile.objects.filter(user_id__in=mentor_user_ids).update(role='mentor')
    print(f"  → Migrated {updated} users to role='mentor'")


def reverse_migration(apps, schema_editor):
    # Reverse is a no-op; the Mentors group membership still exists
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_add_role_to_userprofile'),
    ]

    operations = [
        migrations.RunPython(migrate_mentors_to_role, reverse_migration),
    ]
