# Generated manually — adds is_public flag to VideoLesson.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('exams', '0020_videolesson_quizquestion'),
    ]

    operations = [
        migrations.AddField(
            model_name='videolesson',
            name='is_public',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'If True, the lesson is visible to all users (mentor-uploaded). '
                    'If False, it is private and visible only to the creator (student-uploaded).'
                ),
            ),
        ),
    ]
