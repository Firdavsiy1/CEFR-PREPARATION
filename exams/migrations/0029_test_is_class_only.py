from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('exams', '0028_classroom_video_lessons'),
    ]

    operations = [
        migrations.AddField(
            model_name='test',
            name='is_class_only',
            field=models.BooleanField(default=False, help_text='Class-exclusive tests: hidden from public dashboard and mentor panel; accessible only through classroom.'),
        ),
    ]
