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
# Core exam models
# ---------------------------------------------------------------------------

class Test(models.Model):
    """
    A complete CEFR test (e.g. 'Test 1').
    Each test contains multiple Parts (typically 6 for Listening).
    """
    name = models.CharField(max_length=100, unique=True)
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
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Only active tests are shown to users.",
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def total_questions(self):
        return Question.objects.filter(part__test=self).count()

    @property
    def max_possible_score(self):
        total = 0.0
        for part in self.parts.all():
            total += part.questions.count() * part.points_per_question
        return total


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
        """
        results = []
        for part in self.test.parts.all():
            part_answers = self.answers.filter(question__part=part)
            correct_count = part_answers.filter(is_correct=True).count()
            total_in_part = part.questions.count()
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
        help_text="List of correction dicts: {original, correction, explanation}"
    )

    class Meta:
        unique_together = ['attempt', 'task']

    def __str__(self):
        return f"Submission: {self.attempt.user.username} for {self.task}"
