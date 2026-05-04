"""
Models for the CEFR Exam Preparation application.

Hierarchy: Test → Part → Question → Choice (for multiple-choice types)
User data: UserAttempt → UserAnswer

Scoring weights per part:
    Part 1: 2.0 pts   Part 2: 2.5 pts   Part 3: 3.0 pts
    Part 4: 3.0 pts   Part 5: 3.0 pts   Part 6: 4.0 pts
"""

import re
import unicodedata

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


# ---------------------------------------------------------------------------
# Grading utilities
# ---------------------------------------------------------------------------

def normalize_answer(text: str) -> str:
    """
    Normalize a user-submitted answer for fair comparison.

    Steps:
      1. Strip leading/trailing whitespace
      2. Collapse multiple internal spaces to a single space
      3. Convert to lowercase
      4. Remove common punctuation (periods, commas, quotes, etc.)
      5. Normalize unicode (é → e, café → cafe)

    This ensures fill-in-the-blank grading isn't affected by minor typos
    in spacing, capitalization, or stray punctuation.
    """
    if not text:
        return ""
    # Strip and collapse spaces
    text = text.strip()
    text = re.sub(r'\s+', ' ', text)
    # Lowercase
    text = text.lower()
    # Remove punctuation (keep alphanumeric + spaces)
    text = re.sub(r'[^\w\s]', '', text, flags=re.UNICODE)
    # Normalize unicode accents (NFD decomposes, then strip combining marks)
    text = unicodedata.normalize('NFD', text)
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')
    # Final collapse in case punctuation removal left extra spaces
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def check_answer(given: str, correct: str, question_type: str) -> bool:
    """
    Compare a given answer to the correct answer.

    For multiple_choice / map_label: exact letter match (case-insensitive).
    For fill_blank: fully normalized comparison.
    """
    if question_type == 'fill_blank':
        return normalize_answer(given) == normalize_answer(correct)
    else:
        # Multiple choice / map label — simple letter comparison
        return given.strip().upper() == correct.strip().upper()


# ---------------------------------------------------------------------------
# Classroom models (Mentor → Students)
# ---------------------------------------------------------------------------

import random
import string as _string
import uuid as _uuid


def _generate_join_code():
    """Generate a unique 6-character alphanumeric join code."""
    return ''.join(random.choices(_string.ascii_uppercase + _string.digits, k=6))


CLASSROOM_COLORS = [
    ('blue', 'Blue'),
    ('green', 'Green'),
    ('purple', 'Purple'),
    ('gold', 'Gold'),
    ('red', 'Red'),
    ('teal', 'Teal'),
]


class Classroom(models.Model):
    """
    A mentor's classroom — a group of students with assigned tests.

    Mentors create classrooms and share the `join_code` with students.
    Students join by entering the code and gain access to all tests
    linked to the classroom.
    """
    name = models.CharField(
        max_length=200,
        help_text="Название класса (например, 'B2 Утренняя группа').",
    )
    mentor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='mentored_classrooms',
    )
    description = models.TextField(
        blank=True,
        help_text="Описание класса.",
    )
    join_code = models.CharField(
        max_length=8,
        unique=True,
        db_index=True,
        default=_generate_join_code,
        help_text="Уникальный код для вступления учеников.",
    )
    tests = models.ManyToManyField(
        'Test',
        blank=True,
        related_name='classrooms',
        help_text="Тесты, назначенные этому классу.",
    )
    video_lessons = models.ManyToManyField(
        'VideoLesson',
        blank=True,
        related_name='classrooms',
        help_text="Видеоуроки, назначенные этому классу.",
    )
    emoji = models.CharField(
        max_length=8,
        blank=True,
        default='🎓',
        help_text="Эмодзи для визуальной идентификации класса.",
    )
    color = models.CharField(
        max_length=20,
        choices=CLASSROOM_COLORS,
        default='blue',
        help_text="Цветовой акцент карточки класса.",
    )
    invite_token = models.UUIDField(
        default=_uuid.uuid4,
        unique=True,
        db_index=True,
        help_text="UUID-токен для пригласительных ссылок.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Неактивные классы скрыты от учеников.",
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.mentor.username})"

    @property
    def student_count(self):
        return self.memberships.count()

    @property
    def accent_classes(self):
        """Return Tailwind CSS classes for this classroom's accent color."""
        mapping = {
            'blue':   ('duo-blue',   'bg-duo-blue/15',   'border-duo-blue/25',   'text-duo-blue'),
            'green':  ('duo-green',  'bg-duo-green/15',  'border-duo-green/25',  'text-duo-green'),
            'purple': ('duo-purple', 'bg-duo-purple/15', 'border-duo-purple/25', 'text-duo-purple'),
            'gold':   ('duo-gold',   'bg-duo-gold/15',   'border-duo-gold/25',   'text-duo-gold'),
            'red':    ('duo-red',    'bg-duo-red/15',    'border-duo-red/25',    'text-duo-red'),
            'teal':   ('duo-teal',   'bg-duo-teal/15',   'border-duo-teal/25',   'text-duo-teal'),
        }
        c = self.color or 'blue'
        t = mapping.get(c, mapping['blue'])
        return {'var': t[0], 'bg': t[1], 'border': t[2], 'text': t[3]}


class ClassroomMembership(models.Model):
    """
    Links a student to a mentor's classroom.

    Students can be in multiple classrooms simultaneously.
    """
    classroom = models.ForeignKey(
        Classroom,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='classroom_memberships',
        help_text="Ученик может состоять в нескольких классах.",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-joined_at']
        unique_together = [('classroom', 'student')]

    def __str__(self):
        return f"{self.student.username} → {self.classroom.name}"


class ClassroomAnnouncement(models.Model):
    """
    A mentor's announcement posted to a classroom.

    Pinned announcements appear first in student and mentor views.
    """
    classroom = models.ForeignKey(
        Classroom,
        on_delete=models.CASCADE,
        related_name='announcements',
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='classroom_announcements',
    )
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    is_pinned = models.BooleanField(
        default=False,
        help_text="Закреплённые объявления отображаются первыми.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_pinned', '-created_at']

    def __str__(self):
        return f"[{self.classroom.name}] {self.title}"


class ClassroomAssignment(models.Model):
    """
    A specific test or video lesson assigned to a classroom with
    optional deadline and custom instructions.

    If `due_date` is NULL → unlimited time (no deadline).
    """
    classroom = models.ForeignKey(
        Classroom,
        on_delete=models.CASCADE,
        related_name='assignments',
    )
    test = models.ForeignKey(
        'Test',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='assignments',
    )
    video_lesson = models.ForeignKey(
        'VideoLesson',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='assignments',
    )
    title = models.CharField(
        max_length=300,
        blank=True,
        help_text="Необязательное название задания (напр. 'Домашка неделя 3').",
    )
    instructions = models.TextField(
        blank=True,
        help_text="Инструкции ментора к заданию.",
    )
    due_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Дедлайн. NULL = без ограничения по времени.",
    )
    is_required = models.BooleanField(
        default=True,
        help_text="Обязательное задание или дополнительное.",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-assigned_at']

    def __str__(self):
        target = self.test or self.video_lesson
        return f"[{self.classroom.name}] {target}"

    @property
    def is_overdue(self):
        from django.utils import timezone
        return self.due_date and timezone.now() > self.due_date

    @property
    def days_remaining(self):
        from django.utils import timezone
        if not self.due_date:
            return None
        delta = self.due_date - timezone.now()
        return max(delta.days, 0)


class Notification(models.Model):
    """
    In-app notification for a user.

    Created automatically when mentors post announcements, assign tests,
    or grade submissions. Displayed in the navbar bell dropdown.
    """
    TYPES = [
        ('assignment', 'Новое задание'),
        ('announcement', 'Объявление'),
        ('grade', 'Оценка выставлена'),
        ('deadline', 'Напоминание о дедлайне'),
    ]
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    notification_type = models.CharField(
        max_length=20,
        choices=TYPES,
    )
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True)
    url = models.CharField(
        max_length=500,
        blank=True,
        help_text="URL для перехода при клике.",
    )
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.notification_type}] {self.title} → {self.user.username}"


# ---------------------------------------------------------------------------
# Core exam models
# ---------------------------------------------------------------------------

class Test(models.Model):
    """
    A complete CEFR test (e.g. 'Test 1').
    Each test contains multiple Parts (typically 6 for Listening).
    """
    name = models.CharField(max_length=100, db_index=True)
    test_type = models.CharField(
        max_length=20,
        choices=[
            ('listening', 'Listening'),
            ('reading', 'Reading'),
            ('writing', 'Writing'),
            ('speaking', 'Speaking'),
        ],
        default='listening',
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='authored_tests',
        help_text="The mentor who created/uploaded this test.",
    )
    clone_of = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cloned_tests',
        help_text="The original test this was cloned from (set by split_parts).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Only active tests are shown to users.",
    )
    is_deleted = models.BooleanField(
        default=False,
        help_text="Soft-deleted tests are hidden everywhere but preserve attempt history.",
    )
    is_class_only = models.BooleanField(
        default=False,
        help_text="Class-exclusive tests: hidden from public dashboard and mentor panel; accessible only through classroom.",
    )
    deleted_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp when the test was soft-deleted.",
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def total_questions(self):
        if self.test_type == 'reading':
            return ReadingQuestion.objects.filter(part__test=self).count()
        if self.test_type == 'speaking':
            return SpeakingQuestion.objects.filter(part__test=self).count()
        return Question.objects.filter(part__test=self).count()

    @property
    def max_possible_score(self):
        if self.test_type == 'reading':
            return float(ReadingQuestion.objects.filter(part__test=self).count())
        if self.test_type == 'speaking':
            return float(SpeakingQuestion.objects.filter(part__test=self).count() * 10)
        total = 0.0
        for part in self.parts.all():
            total += part.questions.count() * part.points_per_question
        return total

    @property
    def num_parts(self):
        if self.test_type == 'reading':
            return self.reading_parts.count()
        if self.test_type == 'speaking':
            return self.speaking_parts.count()
        return self.parts.count()


class Part(models.Model):
    """
    A numbered part within a test (Part 1–6).
    Each part has its own audio, question image, scoring weight,
    and optionally a supplementary image (e.g. map for Part 4).
    """
    PART_WEIGHTS = {
        1: 2.0,
        2: 2.5,
        3: 3.0,
        4: 3.0,
        5: 3.0,
        6: 4.0,
    }

    test = models.ForeignKey(
        Test,
        on_delete=models.CASCADE,
        related_name='parts',
    )
    part_number = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(6)],
    )
    instructions = models.TextField(
        blank=True,
        help_text="Part instructions extracted via OCR or entered manually.",
    )
    audio_file = models.FileField(
        upload_to='exams/audio/',
        help_text="MP3 audio for this part.",
    )
    question_image = models.ImageField(
        upload_to='exams/questions/',
        blank=True,
        help_text="Original screenshot of the questions (always shown to users).",
    )
    map_image = models.ImageField(
        upload_to='exams/maps/',
        blank=True,
        null=True,
        help_text="Supplementary image (e.g. map for Part 4).",
    )
    passage_title = models.CharField(
        max_length=300,
        blank=True,
        help_text=(
            "Title of the passage (Parts 2 & 6), "
            "e.g. 'British Marine Life in Crisis'."
        ),
    )
    passage_text = models.TextField(
        blank=True,
        help_text=(
            "Full passage text with numbered blanks (Parts 2 & 6). "
            "Blanks are marked as {30}, {31}, etc."
        ),
    )
    shared_choices_json = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Shared choices for matching parts (e.g. Part 3). '
            'Format: [{"label": "A", "text": "disturbing someone"}, ...]'
        ),
    )
    transcript = models.TextField(
        blank=True,
        help_text="AI-generated audio transcript.",
    )
    points_per_question = models.FloatField(
        default=2.0,
        help_text="Weighted points awarded per correct answer in this part.",
    )

    class Meta:
        unique_together = ['test', 'part_number']
        ordering = ['part_number']

    def __str__(self):
        return f"{self.test.name} — Part {self.part_number}"

    def save(self, *args, **kwargs):
        """Auto-set the scoring weight based on part number."""
        if self.part_number in self.PART_WEIGHTS:
            self.points_per_question = self.PART_WEIGHTS[self.part_number]
        super().save(*args, **kwargs)

    @property
    def max_score(self):
        return self.questions.count() * self.points_per_question


class Question(models.Model):
    """
    A single question within a Part.

    Question types:
      - multiple_choice: User picks from lettered options (Parts 1, 3, 5)
      - fill_blank:      User types a word/phrase (Parts 2, 6)
      - map_label:       User picks a letter from a map (Part 4)
    """
    QUESTION_TYPES = [
        ('multiple_choice', 'Multiple Choice'),
        ('fill_blank', 'Fill in the Blank'),
        ('map_label', 'Map Labeling'),
    ]

    part = models.ForeignKey(
        Part,
        on_delete=models.CASCADE,
        related_name='questions',
    )
    question_number = models.PositiveIntegerField(
        help_text="Sequential number within this part (1, 2, 3…).",
    )
    global_question_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Question number as printed on the exam paper (1–35).",
    )
    group_label = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Sub-group label, e.g. 'Extract One' (Part 5) "
            "or 'Speaker 1' (Part 3)."
        ),
    )
    question_text = models.TextField(
        blank=True,
        help_text="OCR-extracted question text. The original image is the primary display.",
    )
    question_type = models.CharField(
        max_length=20,
        choices=QUESTION_TYPES,
    )
    correct_answer = models.CharField(
        max_length=200,
        help_text="The correct answer: a letter (A/B/C…) or a word/phrase.",
    )
    explanation = models.TextField(
        blank=True,
        help_text="AI-generated explanation for the correct answer.",
    )

    class Meta:
        unique_together = ['part', 'question_number']
        ordering = ['question_number']

    def __str__(self):
        return f"Q{self.question_number} ({self.part})"

    def check_answer(self, given_answer: str) -> bool:
        """Check whether the given answer is correct."""
        return check_answer(given_answer, self.correct_answer, self.question_type)


class Choice(models.Model):
    """
    An answer option for multiple-choice or map-label questions.
    Not used for fill_blank questions.
    """
    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name='choices',
    )
    label = models.CharField(
        max_length=5,
        help_text="Option letter: A, B, C, etc.",
    )
    text = models.CharField(
        max_length=500,
        help_text="The text of this answer option.",
    )

    class Meta:
        unique_together = ['question', 'label']
        ordering = ['label']

    def __str__(self):
        return f"{self.label}) {self.text}"


# ---------------------------------------------------------------------------
# User attempt tracking
# ---------------------------------------------------------------------------

class UserAttempt(models.Model):
    """
    A user's attempt at a complete Test.

    Created atomically (via transaction.atomic) together with all
    UserAnswer records when the user submits their test.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='attempts',
    )
    test = models.ForeignKey(
        Test,
        on_delete=models.CASCADE,
        related_name='attempts',
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    total_correct = models.PositiveIntegerField(default=0)
    total_questions = models.PositiveIntegerField(default=0)
    total_score = models.FloatField(
        default=0.0,
        help_text="Weighted total score across all parts.",
    )
    max_possible_score = models.FloatField(
        default=0.0,
        help_text="Maximum achievable weighted score.",
    )

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.user.username} — {self.test.name} ({self.started_at:%Y-%m-%d %H:%M})"

    @property
    def score_percentage(self):
        """Return the score as a percentage (0–100)."""
        if self.max_possible_score == 0:
            return 0.0
        return round((self.total_score / self.max_possible_score) * 100, 1)

    def get_part_results(self):
        """
        Return a per-part breakdown of this attempt's results.

        Returns a list of dicts:
          [{'part_number': 1, 'correct': 6, 'total': 8,
            'score': 12.0, 'max_score': 16.0, 'weight': 2.0}, ...]

        Uses Python-level filtering so Django's prefetch_related cache is
        respected: when the caller has prefetched 'test__parts__questions'
        and 'answers__question__part', this method issues zero extra queries.
        Falls back to DB queries gracefully when called without prefetching
        (e.g. the test-result view fetches a single attempt).
        """
        if self.test.test_type == 'reading':
            return self._get_reading_part_results()

        # Build a per-part answer map from the prefetch cache (or a DB query
        # when no prefetch was set up — both paths use .all()).
        part_answers_map: dict[int, list] = {}
        for ans in self.answers.all():
            part_num = ans.question.part.part_number
            part_answers_map.setdefault(part_num, []).append(ans)

        results = []
        for part in self.test.parts.all():
            ans_list = part_answers_map.get(part.part_number, [])
            correct_count = sum(1 for a in ans_list if a.is_correct)
            # len() on a prefetched reverse relation hits the cache, not the DB.
            total_in_part = len(part.questions.all())
            part_score = correct_count * part.points_per_question
            part_max = total_in_part * part.points_per_question

            results.append({
                'part_number': part.part_number,
                'correct': correct_count,
                'total': total_in_part,
                'score': part_score,
                'max_score': part_max,
                'weight': part.points_per_question,
            })
        return results

    def _get_reading_part_results(self):
        part_answers_map: dict[int, list] = {}
        for ans in self.reading_answers.all():
            part_num = ans.question.part.part_number
            part_answers_map.setdefault(part_num, []).append(ans)

        results = []
        for part in self.test.reading_parts.all():
            ans_list = part_answers_map.get(part.part_number, [])
            correct_count = sum(1 for a in ans_list if a.is_correct)
            total_in_part = len(part.questions.all())
            results.append({
                'part_number': part.part_number,
                'correct': correct_count,
                'total': total_in_part,
                'score': float(correct_count),
                'max_score': float(total_in_part),
                'weight': 1.0,
            })
        return results


class UserAnswer(models.Model):
    """
    A single answer submitted by the user for one question within an attempt.
    The is_correct flag is set at submission time based on check_answer().
    """
    attempt = models.ForeignKey(
        UserAttempt,
        on_delete=models.CASCADE,
        related_name='answers',
    )
    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name='user_answers',
    )
    given_answer = models.CharField(max_length=200)
    is_correct = models.BooleanField(default=False)

    class Meta:
        unique_together = ['attempt', 'question']

    def __str__(self):
        mark = "✓" if self.is_correct else "✗"
        return f"{mark} Q{self.question.question_number}: {self.given_answer}"

    def save(self, *args, **kwargs):
        """Automatically grade the answer on save."""
        self.is_correct = self.question.check_answer(self.given_answer)
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Background ingestion task tracking
# ---------------------------------------------------------------------------

class IngestionTask(models.Model):
    """
    Tracks the progress of a background test ingestion job.

    Created when a mentor uploads a ZIP. The background thread updates
    status, progress, and stage fields as it works. The frontend polls
    the API endpoint to display real-time progress.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ingestion_tasks',
    )
    test_name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
    )
    progress = models.PositiveIntegerField(
        default=0,
        help_text="Progress percentage (0-100).",
    )
    stage = models.CharField(
        max_length=300,
        default='В очереди...',
        help_text="Human-readable description of the current processing stage.",
    )
    split_parts = models.BooleanField(default=False)
    result_test_id = models.IntegerField(
        null=True, blank=True,
        help_text="ID of the created Test object on success.",
    )
    parts_count = models.PositiveIntegerField(default=0)
    questions_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Ingestion: {self.test_name} ({self.get_status_display()})"

    def update_progress(self, progress, stage):
        """Thread-safe progress update."""
        self.progress = min(progress, 100)
        self.stage = stage
        self.save(update_fields=['progress', 'stage'])


# ---------------------------------------------------------------------------
# Writing Module Models
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Auto-save & Anti-cheat
# ---------------------------------------------------------------------------

class AutoSaveDraft(models.Model):
    """
    Stores partial answers during an active test attempt.
    One row per attempt — the JSON blob holds all in-progress answers.
    Cleared once the test is finalized.
    """
    attempt = models.OneToOneField(
        'UserAttempt',
        on_delete=models.CASCADE,
        related_name='draft',
    )
    data = models.JSONField(
        default=dict,
        help_text='Draft answers: {"question_<id>": "A", "task_<id>": "text", ...}',
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Draft for {self.attempt}"


class TabBlurEvent(models.Model):
    """
    Logs each time a student leaves the test tab (window blur).
    Mentors can review these to detect potential cheating.
    """
    attempt = models.ForeignKey(
        'UserAttempt',
        on_delete=models.CASCADE,
        related_name='tab_blur_events',
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    duration_seconds = models.FloatField(
        null=True,
        blank=True,
        help_text="How long (seconds) the student was away from the tab.",
    )

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"Blur at {self.timestamp:%H:%M:%S} ({self.attempt})"


class WritingTask(models.Model):
    """
    A writing task (1.1, 1.2, or 2) within a writing test.
    """
    TASK_TYPES = [
        ('informal', 'Informal Message (Task 1.1)'),
        ('formal', 'Formal Letter (Task 1.2)'),
        ('essay', 'Academic Essay (Task 2)'),
    ]

    test = models.ForeignKey(
        Test,
        on_delete=models.CASCADE,
        related_name='writing_tasks'
    )
    task_type = models.CharField(max_length=20, choices=TASK_TYPES)
    input_text = models.TextField(
        blank=True,
        help_text="The incoming letter, advert, or text the student reads."
    )
    prompt = models.TextField(
        help_text="The instructions/prompt for writing."
    )
    min_words = models.PositiveIntegerField(default=0)
    max_words = models.PositiveIntegerField(default=0)
    order = models.PositiveIntegerField(
        default=1,
        help_text="Order in the test (1, 2, or 3)"
    )

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"{self.test.name} - {self.get_task_type_display()}"


class WritingSubmission(models.Model):
    """
    A student's submission for a single writing task.
    """
    attempt = models.ForeignKey(
        UserAttempt,
        on_delete=models.CASCADE,
        related_name='writing_submissions'
    )
    task = models.ForeignKey(
        WritingTask,
        on_delete=models.CASCADE,
        related_name='submissions'
    )
    submitted_text = models.TextField()
    word_count = models.PositiveIntegerField(default=0)

    # Automated AI Grading Fields
    is_graded = models.BooleanField(default=False)
    estimated_level = models.CharField(
        max_length=10,
        blank=True,
        help_text="CEFR level estimate (A1-C1)"
    )
    feedback_text = models.TextField(
        blank=True,
        help_text="General feedback on structure and grammar"
    )
    corrections_json = models.JSONField(
        default=list,
        blank=True,
        help_text="List of correction dicts: {original, correction, explanation, explanation_i18n}"
    )
    feedback_json = models.JSONField(
        default=dict,
        blank=True,
        help_text='Multilingual feedback: {"en": "...", "ru": "...", "uz": "..."}'
    )

    # Mentor Manual Grading Fields
    mentor_score = models.FloatField(
        null=True, blank=True,
        help_text="Manual score set by a mentor (0–10 for speaking, 0–100 for writing)."
    )
    mentor_feedback = models.TextField(
        blank=True,
        help_text="Written feedback from the mentor."
    )

    class Meta:
        unique_together = ['attempt', 'task']

    def __str__(self):
        return f"Submission: {self.attempt.user.username} for {self.task}"


# ---------------------------------------------------------------------------
# Reading Module Models
# ---------------------------------------------------------------------------

class ReadingTest(models.Model):
    """
    Thin extension of Test for reading-specific metadata.
    Linked OneToOne so the existing Test infrastructure (author, is_active,
    attempts, dashboard) is automatically reused.

    Usage:
        test = Test.objects.create(name="Reading Test 1", test_type="reading", ...)
        reading_meta = ReadingTest.objects.create(test=test)
    """
    test = models.OneToOneField(
        Test,
        on_delete=models.CASCADE,
        related_name='reading_meta',
        limit_choices_to={'test_type': 'reading'},
    )
    total_parts = models.PositiveIntegerField(
        default=5,
        help_text="Number of parts in this reading test (typically 5).",
    )

    class Meta:
        verbose_name = 'Reading Test Metadata'
        verbose_name_plural = 'Reading Tests Metadata'

    def __str__(self):
        return f"Reading Meta → {self.test.name}"


class ReadingPart(models.Model):
    """
    One numbered part within a reading test.

    Each part has its own instruction block and question range.
    The actual passage text lives in the related ReadingPassage (if any).
    Parts like 'matching short texts' have no single passage (passage=None).
    """
    test = models.ForeignKey(
        Test,
        on_delete=models.CASCADE,
        related_name='reading_parts',
        limit_choices_to={'test_type': 'reading'},
    )
    part_number = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Part number as printed on the exam (1–5 typically).",
    )
    instruction = models.TextField(
        blank=True,
        help_text="Full instruction text for this part (e.g. 'Fill in each gap with ONE word...').",
    )
    # Global question numbers that belong to this part (for display purposes).
    question_number_start = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="First question number in this part (e.g. 1 for Q1-6).",
    )
    question_number_end = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Last question number in this part (e.g. 6 for Q1-6).",
    )

    class Meta:
        unique_together = ['test', 'part_number']
        ordering = ['part_number']

    def __str__(self):
        return f"{self.test.name} — Reading Part {self.part_number}"

    # ------------------------------------------------------------------
    # Compatibility properties used by the shared test_result.html template
    # ------------------------------------------------------------------

    @property
    def points_per_question(self):
        """Each reading question is worth 1 point."""
        return 1.0

    @property
    def transcript(self):
        return ""

    @property
    def audio_file(self):
        return None


class ReadingPassage(models.Model):
    """
    The reading text associated with a ReadingPart.

    Stored separately so the LLM can concatenate multi-page passages
    without polluting the part record with large text blobs.

    Not all parts have a passage:
      - Fill-in-the-blank, heading-match, and MCQ parts → OneToOne passage.
      - Matching-short-texts parts (e.g. Part 2 with ads/notices) → no passage
        (passage is None on the part; each question holds its own mini-text).
    """
    part = models.OneToOneField(
        ReadingPart,
        on_delete=models.CASCADE,
        related_name='passage',
    )
    title = models.CharField(
        max_length=300,
        blank=True,
        help_text="Title of the passage (e.g. 'Sepsis', 'Radio Automation').",
    )
    content = models.TextField(
        help_text=(
            "Full concatenated passage text. Page numbers and footers removed. "
            "Paragraphs separated by '\\n\\n'. "
            "Fill-in-the-blank gaps are marked as [GAP]."
        ),
    )

    def __str__(self):
        return f"Passage: {self.title or '(untitled)'} ({self.part})"


class ReadingQuestion(models.Model):
    """
    A single question within a ReadingPart.

    Supports all reading question formats found in CEFR exams:

    ┌─────────────────────┬────────────────────────────────────────────────────┐
    │ question_type       │ Example                                            │
    ├─────────────────────┼────────────────────────────────────────────────────┤
    │ multiple_choice     │ Choose A/B/C/D (Parts 4, 5)                        │
    │ fill_in_the_blank   │ One word from the text / no options (Parts 1, 5)   │
    │ matching            │ Match text 7-14 to statements A-J (Part 2, 3)      │
    │ true_false_ni       │ True / False / No Information (Part 4)             │
    │ insert_sentence     │ Insert missing sentence into paragraph (rare)      │
    └─────────────────────┴────────────────────────────────────────────────────┘
    """
    QUESTION_TYPES = [
        ('multiple_choice',  'Multiple Choice'),
        ('fill_in_the_blank', 'Fill in the Blank'),
        ('matching',          'Matching'),
        ('true_false_ni',     'True / False / No Information'),
        ('insert_sentence',   'Insert Sentence'),
    ]

    part = models.ForeignKey(
        ReadingPart,
        on_delete=models.CASCADE,
        related_name='questions',
    )
    question_number = models.PositiveIntegerField(
        help_text="Global question number as printed on the exam paper (e.g. 1, 7, 21).",
    )
    question_type = models.CharField(
        max_length=25,
        choices=QUESTION_TYPES,
    )
    question_text = models.TextField(
        blank=True,
        help_text=(
            "The question stem or statement. "
            "For fill_in_the_blank, the sentence with [GAP] as the placeholder. "
            "For matching, e.g. 'Text 7' or the full statement to match."
        ),
    )
    options = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Answer options as a list of strings. "
            "For multiple_choice: ['A) text...', 'B) text...', 'C) text...', 'D) text...']. "
            "For matching: the shared list of statements (A-J). "
            "Empty [] for fill_in_the_blank with no provided choices."
        ),
    )
    correct_answer = models.CharField(
        max_length=500,
        help_text=(
            "The correct answer. "
            "A letter for MCQ/matching ('A', 'C', 'H'). "
            "A word or phrase for fill_in_the_blank ('killers'). "
            "'True', 'False', or 'No Information' for true_false_ni."
        ),
    )
    explanation = models.TextField(
        blank=True,
        help_text="Optional AI-generated explanation for why this answer is correct.",
    )

    class Meta:
        unique_together = ['part', 'question_number']
        ordering = ['question_number']

    def __str__(self):
        return f"Reading Q{self.question_number} ({self.part})"

    def check_answer(self, given: str) -> bool:
        """
        Normalized answer comparison.
        Letter-based types (MCQ, matching, true_false_ni) use case-insensitive
        exact match. Fill-in-the-blank uses the full normalize_answer pipeline.
        """
        if self.question_type == 'fill_in_the_blank':
            return normalize_answer(given) == normalize_answer(self.correct_answer)
        return given.strip().upper() == self.correct_answer.strip().upper()


class ReadingUserAnswer(models.Model):
    """
    A single answer submitted by the user for a reading question.
    """
    attempt = models.ForeignKey(
        UserAttempt,
        on_delete=models.CASCADE,
        related_name='reading_answers',
    )
    question = models.ForeignKey(
        ReadingQuestion,
        on_delete=models.CASCADE,
        related_name='user_answers',
    )
    given_answer = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)

    class Meta:
        unique_together = ['attempt', 'question']

    def __str__(self):
        mark = "✓" if self.is_correct else "✗"
        return f"{mark} Reading Q{self.question.question_number}: {self.given_answer}"

    def save(self, *args, **kwargs):
        self.is_correct = self.question.check_answer(self.given_answer)
        super().save(*args, **kwargs)

# ---------------------------------------------------------------------------
# Speaking test models
# ---------------------------------------------------------------------------

class SpeakingPart(models.Model):
    """
    A section of a Speaking test (e.g. Part 1, Part 2, Part 3).
    Usually created from a mentor-uploaded photo of a textbook page.

    Scoring weights (points_per_question = max points per question in this part):
        Part 1: 10 pts  — basic personal questions (warm-up)
        Part 2: 15 pts  — structured monologue / cue card (range & fluency)
        Part 3: 20 pts  — abstract two-way discussion (sophisticated vocab & grammar)
        Part 4: 25 pts  — complex debate / extended discussion (highest demand)
    Gemini rates each answer 0–10; the weighted score = raw_score × points_per_question / 10.
    """

    PART_WEIGHTS = {1: 10.0, 2: 15.0, 3: 20.0, 4: 25.0}

    test = models.ForeignKey(
        Test,
        on_delete=models.CASCADE,
        related_name='speaking_parts',
        limit_choices_to={'test_type': 'speaking'},
    )
    part_number = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(4)],
    )
    instructions = models.TextField(blank=True)
    points_per_question = models.FloatField(
        default=10.0,
        help_text="Max weighted points per question in this part (auto-set by part number).",
    )
    
    # Original image uploaded by mentor
    original_image = models.ImageField(
        upload_to='exams/speaking/original/',
        blank=True, null=True,
    )
    
    # Auto-cropped image for student UI
    cropped_image = models.ImageField(
        upload_to='exams/speaking/cropped/',
        blank=True, null=True,
        help_text="Isolated image for this part (e.g. Part 2 cue card or Part 3 charts).",
    )
    
    # AI-generated description of the cropped image
    alt_text = models.TextField(
        blank=True,
        help_text="AI-generated description of the image for accessibility and low-bandwidth fallback.",
    )

    # Debate table data for Part 3 (FOR/AGAINST structure)
    debate_data = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Stores Part 3 debate table: "
            "{\"topic\": \"...\", \"for_points\": [...], \"against_points\": [...]}"
        ),
    )
    
    # Coordinates of the extracted components to show in validation UI
    validation_data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Stores bounding boxes and raw extraction data from Document AI / Gemini.",
    )
    
    is_validated = models.BooleanField(
        default=False,
        help_text="Has the mentor validated the auto-crop and extraction?",
    )

    class Meta:
        unique_together = ['test', 'part_number']
        ordering = ['part_number']

    def __str__(self):
        return f"{self.test.name} — Speaking Part {self.part_number}"

    def save(self, *args, **kwargs):
        """Auto-set the scoring weight based on part number."""
        if self.part_number in self.PART_WEIGHTS:
            self.points_per_question = self.PART_WEIGHTS[self.part_number]
        super().save(*args, **kwargs)

    @property
    def max_score(self):
        return self.questions.count() * self.points_per_question


class SpeakingQuestion(models.Model):
    """
    Individual questions asked by the examiner (TTS) within a Speaking part.
    """
    part = models.ForeignKey(
        SpeakingPart,
        on_delete=models.CASCADE,
        related_name='questions',
    )
    question_number = models.PositiveIntegerField()
    question_text = models.TextField(
        help_text="Text of the question, string for TTS generation.",
    )
    audio_file = models.FileField(
        upload_to='exams/speaking/audio/',
        blank=True, null=True,
        help_text="Pre-generated TTS audio for this question.",
    )

    class Meta:
        unique_together = ['part', 'question_number']
        ordering = ['question_number']

    def __str__(self):
        return f"Q{self.question_number} ({self.part})"


class SpeakingSubmission(models.Model):
    """Stores a student's recorded audio answer for a single speaking question."""
    attempt = models.ForeignKey(
        'UserAttempt',
        on_delete=models.CASCADE,
        related_name='speaking_submissions',
    )
    question = models.ForeignKey(
        SpeakingQuestion,
        on_delete=models.CASCADE,
        related_name='submissions',
    )
    audio_file = models.FileField(
        upload_to='uploads/speaking/',
        blank=True,
        null=True,
    )
    duration_seconds = models.FloatField(default=0)
    transcript = models.TextField(blank=True)
    is_evaluated = models.BooleanField(default=False)
    estimated_level = models.CharField(max_length=5, blank=True)
    feedback_text = models.TextField(blank=True)
    feedback_json = models.JSONField(
        default=dict,
        blank=True,
        help_text='Multilingual feedback: {"en": "...", "ru": "...", "uz": "..."}'
    )
    score = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    # Mentor Manual Grading Fields
    mentor_score = models.FloatField(
        null=True, blank=True,
        help_text="Manual score set by a mentor (0–10)."
    )
    mentor_feedback = models.TextField(
        blank=True,
        help_text="Written feedback from the mentor."
    )

    class Meta:
        unique_together = ['attempt', 'question']
        ordering = ['question__part__part_number', 'question__question_number']

    def __str__(self):
        return f"Speaking sub: {self.attempt.user} / Q{self.question.question_number}"


# ---------------------------------------------------------------------------
# AI Chat Models
# ---------------------------------------------------------------------------

class ChatSession(models.Model):
    """
    A conversation session between a user and the AI assistant.
    Groups multiple ChatMessage records under a single titled thread.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='chat_sessions',
    )
    title = models.CharField(
        max_length=200,
        default='New Chat',
        help_text="Auto-generated from the first user message.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"Chat: {self.title} ({self.user.username})"


class ChatMessage(models.Model):
    """
    A single message within an AI chat session.
    """
    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
    ]

    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    module_cards = models.JSONField(
        default=list,
        blank=True,
        help_text='List of module IDs to display as cards, e.g. ["listening", "writing"]',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.role}: {self.content[:50]}..."


# ---------------------------------------------------------------------------
# Dictionary Models
# ---------------------------------------------------------------------------

class WordCache(models.Model):
    """
    Global cache for word enrichment data from Gemini.

    When any student looks up a word, the AI-generated data (transcription,
    translations, definition, example) is stored here.  Subsequent students
    who add the same word get the cached data instantly — no extra API call.
    """
    word = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Normalised lowercase English word or short phrase.",
    )
    transcription = models.CharField(
        max_length=200,
        blank=True,
        help_text="IPA phonetic transcription, e.g. /juːˈbɪkwɪtəs/.",
    )
    translations = models.JSONField(
        default=dict,
        blank=True,
        help_text='Translations keyed by language code: {"ru": "...", "uz": "...", "en": "..."}.',
    )
    definitions = models.JSONField(
        default=dict,
        blank=True,
        help_text='Definitions keyed by language code: {"ru": "...", "uz": "...", "en": "..."}.',
    )
    example = models.TextField(
        blank=True,
        help_text="Example sentence using the word in natural context.",
    )
    part_of_speech = models.CharField(
        max_length=50,
        blank=True,
        help_text="Part of speech: noun, verb, adjective, etc.",
    )
    register = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text="Register: formal, informal, or neutral.",
    )
    alternative_form = models.TextField(
        blank=True,
        default='',
        help_text="Opposite-register equivalent: informal→formal or formal→informal.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['word']
        verbose_name = 'Word Cache'
        verbose_name_plural = 'Word Cache'

    def __str__(self):
        return self.word


class DictionaryEntry(models.Model):
    """
    A word saved by a student to their personal dictionary.

    Points to a shared WordCache record so Gemini is called at most once
    per unique word across the entire platform.
    """
    SOURCE_CHOICES = [
        ('manual', 'Manual input'),
        ('dblclick', 'Double-click in UI'),
        ('test', 'From a test'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='dictionary_entries',
    )
    cached_word = models.ForeignKey(
        WordCache,
        on_delete=models.CASCADE,
        related_name='user_entries',
    )
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default='manual',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'cached_word']
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} → {self.cached_word.word}"


# ---------------------------------------------------------------------------
# YouTube Video Learning Models
# ---------------------------------------------------------------------------

class VideoLesson(models.Model):
    """
    A YouTube video used as an English learning lesson.

    The transcript is fetched via youtube-transcript-api and stored as JSON.
    AI-generated quiz questions are linked via the QuizQuestion model.
    """
    CEFR_LEVELS = [
        ('A1', 'A1 — Beginner'),
        ('A2', 'A2 — Elementary'),
        ('B1', 'B1 — Intermediate'),
        ('B2', 'B2 — Upper-Intermediate'),
        ('C1', 'C1 — Advanced'),
    ]

    youtube_id = models.CharField(
        max_length=20,
        unique=True,
        db_index=True,
        help_text="YouTube video ID (e.g. 'dQw4w9WgXcQ').",
    )
    title = models.CharField(
        max_length=300,
        help_text="Video title (fetched from YouTube or entered manually).",
    )
    cefr_level = models.CharField(
        max_length=2,
        choices=CEFR_LEVELS,
        default='B2',
        help_text="Target CEFR level for quiz question generation.",
    )
    thumbnail_url = models.URLField(
        blank=True,
        help_text="YouTube video thumbnail URL.",
    )
    transcript_json = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Raw transcript from youtube-transcript-api. "
            "Format: [{\"text\": \"...\", \"start\": 0.0, \"duration\": 3.5}, ...]"
        ),
    )
    topic_tags = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "AI-detected topic categories for this video, e.g. "
            '["Science", "Technology", "Education"].'
        ),
    )
    is_public = models.BooleanField(
        default=False,
        help_text=(
            "If True, the lesson is visible to all users (mentor-uploaded). "
            "If False, it is private and visible only to the creator (student-uploaded)."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='video_lessons',
        help_text="The user who added this video lesson.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.youtube_id})"

    @property
    def youtube_url(self):
        return f"https://www.youtube.com/watch?v={self.youtube_id}"

    @property
    def quiz_count(self):
        return self.quiz_questions.count()


class QuizQuestion(models.Model):
    """
    An AI-generated quiz question tied to a specific timecode in a video.

    When the video reaches `trigger_time_seconds`, playback is paused and
    the question is displayed as a modal overlay.
    """
    video = models.ForeignKey(
        VideoLesson,
        on_delete=models.CASCADE,
        related_name='quiz_questions',
    )
    trigger_time_seconds = models.PositiveIntegerField(
        help_text="Time in seconds when the video should pause for this question.",
    )
    question_text = models.TextField(
        help_text="The listening comprehension question.",
    )
    options = models.JSONField(
        default=list,
        help_text='Answer options as a list of strings: ["opt A", "opt B", "opt C", "opt D"].',
    )
    correct_option_index = models.PositiveSmallIntegerField(
        help_text="0-based index of the correct answer in the options list.",
    )
    explanation = models.TextField(
        blank=True,
        help_text="AI-generated explanation of why the correct answer is right.",
    )

    class Meta:
        ordering = ['trigger_time_seconds']

    def __str__(self):
        return f"Q@{self.trigger_time_seconds}s — {self.video.title[:40]}"


# ---------------------------------------------------------------------------
# Multiplayer Video Room Models (Kahoot-style)
# ---------------------------------------------------------------------------

class VideoRoom(models.Model):
    """
    A live session for collective video viewing.
    """
    STATUS_CHOICES = [
        ('waiting', 'Waiting in Lobby'),
        ('playing', 'Video Playing'),
        ('question', 'Question Active'),
        ('results', 'Showing Results'),
        ('finished', 'Finished'),
    ]

    room_code = models.CharField(max_length=10, unique=True, db_index=True)
    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hosted_rooms')
    lesson = models.ForeignKey(VideoLesson, on_delete=models.CASCADE, related_name='rooms')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    current_question = models.ForeignKey(QuizQuestion, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Room {self.room_code} - {self.lesson.title}"


class VideoRoomParticipant(models.Model):
    """
    A participant in a live VideoRoom.
    """
    room = models.ForeignKey(VideoRoom, on_delete=models.CASCADE, related_name='participants')
    nickname = models.CharField(max_length=50)
    score = models.IntegerField(default=0)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_active = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['room', 'nickname']

    def __str__(self):
        return f"{self.nickname} in {self.room.room_code}"


class VideoRoomAnswer(models.Model):
    """
    An answer submitted by a participant in a live VideoRoom.
    """
    participant = models.ForeignKey(VideoRoomParticipant, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(QuizQuestion, on_delete=models.CASCADE)
    selected_index = models.IntegerField()
    is_correct = models.BooleanField(default=False)
    points_awarded = models.IntegerField(default=0)
    answered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['participant', 'question']

    def __str__(self):
        return f"{self.participant.nickname} -> Q:{self.question.id}"