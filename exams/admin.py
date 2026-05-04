"""
Django Admin configuration for the CEFR Exams application.

Layout:
  ── Listening:  Test → Part → Question → Choice
  ── Reading:    ReadingTest → ReadingPart → ReadingPassage / ReadingQuestion
  ── Writing:    WritingTask (read-only submissions inline)
  ── Speaking:   SpeakingPart → SpeakingQuestion (managed via builder; admin is read-only)
  ── Attempts:   UserAttempt → UserAnswer / ReadingUserAnswer / TabBlurEvent
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Test, Part, Question, Choice, UserAttempt, UserAnswer,
    AutoSaveDraft, TabBlurEvent,
    ReadingTest, ReadingPart, ReadingPassage, ReadingQuestion,
    ReadingUserAnswer,
    WritingTask, WritingSubmission,
    SpeakingPart, SpeakingQuestion, SpeakingSubmission,
    VideoLesson, QuizQuestion,
    WordCache, DictionaryEntry,
)

# ---------------------------------------------------------------------------
# Site branding
# ---------------------------------------------------------------------------

admin.site.site_header = 'CEFR Preparer — Admin'
admin.site.site_title = 'CEFR Preparer'
admin.site.index_title = 'Панель управления'


# ---------------------------------------------------------------------------
# Inline admin classes
# ---------------------------------------------------------------------------

class PartInline(admin.TabularInline):
    model = Part
    extra = 0
    fields = (
        'part_number', 'points_per_question', 'audio_file',
        'question_image', 'map_image',
    )
    readonly_fields = ('points_per_question',)
    show_change_link = True
    ordering = ('part_number',)


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 0
    fields = (
        'question_number', 'global_question_number', 'question_type',
        'group_label', 'correct_answer', 'question_text',
    )
    show_change_link = True


class ChoiceInline(admin.TabularInline):
    model = Choice
    extra = 0
    fields = ('label', 'text')


class UserAnswerInline(admin.TabularInline):
    model = UserAnswer
    extra = 0
    readonly_fields = ('question', 'given_answer', 'is_correct')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ReadingUserAnswerInline(admin.TabularInline):
    model = ReadingUserAnswer
    extra = 0
    readonly_fields = ('question', 'given_answer', 'is_correct')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class TabBlurEventInline(admin.TabularInline):
    model = TabBlurEvent
    extra = 0
    readonly_fields = ('timestamp', 'duration_seconds')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# Test admin
# ---------------------------------------------------------------------------

@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = ('name', 'test_type', 'author_name', 'is_active', 'part_count',
                    'question_count', 'created_at')
    list_filter = ('test_type', 'is_active')
    search_fields = ('name', 'author__username')
    list_editable = ('is_active',)
    ordering = ('-created_at',)
    inlines = [PartInline]

    @admin.display(description='Автор')
    def author_name(self, obj):
        return obj.author.username if obj.author else '—'

    @admin.display(description='Parts')
    def part_count(self, obj):
        return obj.parts.count()

    @admin.display(description='Questions')
    def question_count(self, obj):
        return obj.total_questions


# ---------------------------------------------------------------------------
# Part admin
# ---------------------------------------------------------------------------

@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = (
        'test', 'part_number', 'points_per_question',
        'question_count', 'has_audio', 'has_image', 'has_map',
    )
    list_filter = (
        ('test', admin.RelatedOnlyFieldListFilter),
        'part_number',
    )
    search_fields = ('test__name',)
    ordering = ('test', 'part_number')
    readonly_fields = (
        'points_per_question', 'question_image_preview', 'map_image_preview',
    )
    inlines = [QuestionInline]

    fieldsets = (
        (None, {
            'fields': ('test', 'part_number', 'points_per_question'),
        }),
        ('Media Files', {
            'fields': ('audio_file', 'question_image', 'question_image_preview',
                       'map_image', 'map_image_preview'),
        }),
        ('Instructions (OCR)', {
            'fields': ('instructions',),
            'classes': ('collapse',),
        }),
        ('Passage Content (Parts 2 & 6)', {
            'description': (
                'For fill-in-the-blank parts: the passage title and full text '
                'with numbered blank markers like {30}, {31}.'
            ),
            'fields': ('passage_title', 'passage_text'),
            'classes': ('collapse',),
        }),
        ('Shared Choices (Part 3 — Matching)', {
            'description': (
                'For matching parts: a JSON list of shared choices. '
                'Example: [{"label": "A", "text": "disturbing someone"}, ...]'
            ),
            'fields': ('shared_choices_json',),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Questions')
    def question_count(self, obj):
        return obj.questions.count()

    @admin.display(boolean=True, description='Audio')
    def has_audio(self, obj):
        return bool(obj.audio_file)

    @admin.display(boolean=True, description='Image')
    def has_image(self, obj):
        return bool(obj.question_image)

    @admin.display(boolean=True, description='Map')
    def has_map(self, obj):
        return bool(obj.map_image)

    @admin.display(description='Question Screenshot')
    def question_image_preview(self, obj):
        if obj.question_image:
            return format_html(
                '<img src="{}" style="max-width:800px; max-height:600px; '
                'border:1px solid #ccc; border-radius:4px;" />',
                obj.question_image.url,
            )
        from django.utils.safestring import mark_safe
        return mark_safe(
            '<span style="color:#999;">No image uploaded</span>'
        )

    @admin.display(description='Map Preview')
    def map_image_preview(self, obj):
        if obj.map_image:
            return format_html(
                '<img src="{}" style="max-width:600px; max-height:400px; '
                'border:1px solid #ccc; border-radius:4px;" />',
                obj.map_image.url,
            )
        from django.utils.safestring import mark_safe
        return mark_safe(
            '<span style="color:#999;">No map image</span>'
        )


# ---------------------------------------------------------------------------
# Question admin  — primary review interface for OCR corrections
# ---------------------------------------------------------------------------

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = (
        'global_question_number', 'part', 'question_number',
        'question_type', 'correct_answer', 'short_text', 'has_choices',
    )
    list_filter = (
        'question_type',
        ('part__test', admin.RelatedOnlyFieldListFilter),
        'part__part_number',
    )
    search_fields = ('question_text', 'correct_answer')
    list_per_page = 35  # Show a full test on one page

    readonly_fields = ('question_image_preview',)
    inlines = [ChoiceInline]

    fieldsets = (
        ('Question Details', {
            'fields': (
                'part', 'question_number', 'global_question_number',
                'question_type', 'group_label', 'correct_answer',
            ),
        }),
        ('Original Screenshot (source of truth)', {
            'description': (
                'Compare the original screenshot below with the OCR-extracted '
                'text and correct any errors in the text field.'
            ),
            'fields': ('question_image_preview',),
        }),
        ('OCR-Extracted Text (editable)', {
            'fields': ('question_text',),
        }),
    )

    @admin.display(description='Text (preview)')
    def short_text(self, obj):
        if obj.question_text:
            text = obj.question_text[:60]
            return f'{text}…' if len(obj.question_text) > 60 else text
        return '—'

    @admin.display(boolean=True, description='Choices')
    def has_choices(self, obj):
        return obj.choices.exists()

    @admin.display(description='Original Question Screenshot')
    def question_image_preview(self, obj):
        """
        Render the parent Part's question_image inline so the admin can
        compare it against the OCR-extracted text and fix any errors.
        """
        if obj.pk and obj.part and obj.part.question_image:
            return format_html(
                '<div style="background:#f8f8f8; padding:12px; '
                'border-radius:8px; border:1px solid #ddd;">'
                '<img src="{}" style="max-width:100%; max-height:700px; '
                'display:block;" />'
                '</div>',
                obj.part.question_image.url,
            )
        from django.utils.safestring import mark_safe
        return mark_safe(
            '<span style="color:#999; font-style:italic;">'
            'No question image available for this Part.</span>'
        )


# ---------------------------------------------------------------------------
# Choice admin
# ---------------------------------------------------------------------------

@admin.register(Choice)
class ChoiceAdmin(admin.ModelAdmin):
    list_display = ('question', 'label', 'text')
    list_filter = (
        ('question__part__test', admin.RelatedOnlyFieldListFilter),
        'question__part__part_number',
    )
    search_fields = ('text',)
    ordering = ('question__part__test', 'question__question_number', 'label')


# ---------------------------------------------------------------------------
# User attempt & answer admins (read-only)
# ---------------------------------------------------------------------------

@admin.register(UserAttempt)
class UserAttemptAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'test', 'started_at', 'total_correct',
        'total_questions', 'total_score', 'max_possible_score',
        'display_score_pct',
    )
    list_filter = (
        ('test', admin.RelatedOnlyFieldListFilter),
        ('user', admin.RelatedOnlyFieldListFilter),
    )
    search_fields = ('user__username', 'test__name')
    date_hierarchy = 'started_at'
    ordering = ('-started_at',)
    show_full_result_count = False
    readonly_fields = (
        'user', 'test', 'started_at', 'completed_at',
        'total_correct', 'total_questions', 'total_score',
        'max_possible_score',
    )
    inlines = [UserAnswerInline, ReadingUserAnswerInline, TabBlurEventInline]

    def has_add_permission(self, request):
        return False

    @admin.display(description='Score %')
    def display_score_pct(self, obj):
        pct = obj.score_percentage
        if pct >= 70:
            color = '#28a745'
        elif pct >= 50:
            color = '#ffc107'
        else:
            color = '#dc3545'
        return format_html(
            '<span style="font-weight:bold; color:{};">{:.1f}%</span>',
            color, pct,
        )


@admin.register(UserAnswer)
class UserAnswerAdmin(admin.ModelAdmin):
    list_display = ('attempt', 'question', 'given_answer', 'is_correct')
    list_filter = (
        'is_correct',
        ('attempt__test', admin.RelatedOnlyFieldListFilter),
    )
    search_fields = ('attempt__user__username', 'question__correct_answer')
    show_full_result_count = False
    readonly_fields = ('attempt', 'question', 'given_answer', 'is_correct')

    def has_add_permission(self, request):
        return False


# ---------------------------------------------------------------------------
# Auto-save & Anti-cheat admins
# ---------------------------------------------------------------------------

@admin.register(AutoSaveDraft)
class AutoSaveDraftAdmin(admin.ModelAdmin):
    list_display = ('attempt', 'updated_at')
    readonly_fields = ('attempt', 'data', 'updated_at')

    def has_add_permission(self, request):
        return False


@admin.register(TabBlurEvent)
class TabBlurEventAdmin(admin.ModelAdmin):
    list_display = ('attempt', 'timestamp', 'duration_seconds')
    list_filter = ('attempt__test',)
    readonly_fields = ('attempt', 'timestamp', 'duration_seconds')

    def has_add_permission(self, request):
        return False


# ---------------------------------------------------------------------------
# Reading Module Admin
# ---------------------------------------------------------------------------

class ReadingQuestionInline(admin.TabularInline):
    model = ReadingQuestion
    extra = 0
    fields = (
        'question_number', 'question_type', 'question_text',
        'correct_answer', 'explanation',
    )
    show_change_link = True


class ReadingPassageInline(admin.StackedInline):
    model = ReadingPassage
    extra = 0
    fields = ('title', 'content')
    show_change_link = True


class ReadingPartInline(admin.TabularInline):
    model = ReadingPart
    extra = 0
    fields = (
        'part_number', 'instruction',
        'question_number_start', 'question_number_end',
    )
    show_change_link = True


@admin.register(ReadingTest)
class ReadingTestAdmin(admin.ModelAdmin):
    """
    Entry point for reading test metadata — links to the base Test.
    Reading parts are managed via ReadingPart (FK to Test, not ReadingTest).
    """
    list_display = ('test', 'total_parts', 'reading_parts_count', 'reading_questions_count')
    raw_id_fields = ('test',)

    @admin.display(description='Parts')
    def reading_parts_count(self, obj):
        return obj.test.reading_parts.count()

    @admin.display(description='Questions')
    def reading_questions_count(self, obj):
        return ReadingQuestion.objects.filter(part__test=obj.test).count()


@admin.register(ReadingPart)
class ReadingPartAdmin(admin.ModelAdmin):
    list_display = (
        'test', 'part_number',
        'question_number_start', 'question_number_end',
        'question_count', 'has_passage',
    )
    list_filter = (
        ('test', admin.RelatedOnlyFieldListFilter),
        'part_number',
    )
    search_fields = ('test__name', 'instruction')
    ordering = ('test', 'part_number')
    inlines = [ReadingPassageInline, ReadingQuestionInline]

    fieldsets = (
        (None, {
            'fields': ('test', 'part_number', 'question_number_start', 'question_number_end'),
        }),
        ('Instructions', {
            'fields': ('instruction',),
        }),
    )

    @admin.display(description='Questions')
    def question_count(self, obj):
        return obj.questions.count()

    @admin.display(boolean=True, description='Passage')
    def has_passage(self, obj):
        return hasattr(obj, 'passage')


@admin.register(ReadingPassage)
class ReadingPassageAdmin(admin.ModelAdmin):
    list_display = ('part', 'title', 'content_preview')
    search_fields = ('title', 'content', 'part__test__name')
    readonly_fields = ('part',)

    @admin.display(description='Content (preview)')
    def content_preview(self, obj):
        text = obj.content[:120]
        return f"{text}…" if len(obj.content) > 120 else text


@admin.register(ReadingQuestion)
class ReadingQuestionAdmin(admin.ModelAdmin):
    list_display = (
        'question_number', 'part', 'question_type',
        'short_text', 'correct_answer',
    )
    list_filter = (
        'question_type',
        ('part__test', admin.RelatedOnlyFieldListFilter),
        'part__part_number',
    )
    search_fields = ('question_text', 'correct_answer', 'part__test__name')
    list_per_page = 40

    fieldsets = (
        ('Question', {
            'fields': (
                'part', 'question_number', 'question_type',
                'question_text', 'options',
            ),
        }),
        ('Answer', {
            'fields': ('correct_answer', 'explanation'),
        }),
    )

    @admin.display(description='Text (preview)')
    def short_text(self, obj):
        if obj.question_text:
            text = obj.question_text[:70]
            return f"{text}…" if len(obj.question_text) > 70 else text
        return "—"


@admin.register(ReadingUserAnswer)
class ReadingUserAnswerAdmin(admin.ModelAdmin):
    list_display = ('attempt', 'question', 'given_answer', 'is_correct')
    list_filter = (
        'is_correct',
        ('attempt__test', admin.RelatedOnlyFieldListFilter),
    )
    show_full_result_count = False
    readonly_fields = ('attempt', 'question', 'given_answer', 'is_correct')

    def has_add_permission(self, request):
        return False


# ---------------------------------------------------------------------------
# Writing Module Admin (read-only monitoring — managed via mentor builder)
# ---------------------------------------------------------------------------

class WritingSubmissionInline(admin.TabularInline):
    model = WritingSubmission
    extra = 0
    fields = ('attempt', 'word_count', 'is_graded', 'estimated_level')
    readonly_fields = ('attempt', 'word_count', 'is_graded', 'estimated_level')
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WritingTask)
class WritingTaskAdmin(admin.ModelAdmin):
    list_display = ('test', 'task_type', 'order', 'word_range', 'has_prompt', 'has_input_text')
    list_filter = (
        ('test', admin.RelatedOnlyFieldListFilter),
        'task_type',
    )
    search_fields = ('test__name', 'prompt')
    ordering = ('test', 'order')
    inlines = [WritingSubmissionInline]

    fieldsets = (
        (None, {
            'fields': ('test', 'task_type', 'order'),
        }),
        ('Задание', {
            'fields': ('prompt', 'input_text'),
        }),
        ('Лимит слов', {
            'fields': ('min_words', 'max_words'),
        }),
    )

    @admin.display(description='Лимит слов')
    def word_range(self, obj):
        if obj.min_words or obj.max_words:
            return f'{obj.min_words}–{obj.max_words}'
        return '—'

    @admin.display(boolean=True, description='Prompt')
    def has_prompt(self, obj):
        return bool((obj.prompt or '').strip())

    @admin.display(boolean=True, description='Input text')
    def has_input_text(self, obj):
        return bool((obj.input_text or '').strip())


@admin.register(WritingSubmission)
class WritingSubmissionAdmin(admin.ModelAdmin):
    list_display = ('attempt', 'task', 'word_count', 'is_graded', 'estimated_level')
    list_filter = (
        'is_graded',
        ('task__test', admin.RelatedOnlyFieldListFilter),
        'estimated_level',
    )
    search_fields = ('attempt__user__username', 'task__test__name')
    ordering = ('-attempt__started_at',)
    show_full_result_count = False
    readonly_fields = (
        'attempt', 'task', 'word_count', 'submitted_text',
        'is_graded', 'estimated_level', 'feedback_text',
        'corrections_json', 'feedback_json',
    )

    def has_add_permission(self, request):
        return False


# ---------------------------------------------------------------------------
# Speaking Module Admin (read-only monitoring — managed via Speaking Builder)
# ---------------------------------------------------------------------------

class SpeakingQuestionInline(admin.TabularInline):
    model = SpeakingQuestion
    extra = 0
    fields = ('question_number', 'question_text', 'has_audio')
    readonly_fields = ('question_number', 'question_text', 'has_audio')
    can_delete = False
    show_change_link = True
    ordering = ('question_number',)

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(boolean=True, description='TTS Audio')
    def has_audio(self, instance):
        return bool(instance.audio_file)


@admin.register(SpeakingPart)
class SpeakingPartAdmin(admin.ModelAdmin):
    """
    Read-only view of Speaking parts.  Editing is done through the
    Speaking Builder — this admin is for monitoring and data inspection only.
    """
    list_display = (
        'test', 'part_number', 'is_validated', 'has_image',
        'question_count', 'points_per_question',
    )
    list_filter = (
        ('test', admin.RelatedOnlyFieldListFilter),
        'is_validated',
        'part_number',
    )
    search_fields = ('test__name', 'instructions')
    ordering = ('test', 'part_number')
    readonly_fields = (
        'test', 'part_number', 'instructions', 'points_per_question',
        'is_validated', 'alt_text', 'debate_data', 'validation_data',
        'original_image_preview', 'cropped_image_preview',
    )
    inlines = [SpeakingQuestionInline]

    fieldsets = (
        (None, {
            'fields': ('test', 'part_number', 'is_validated', 'points_per_question'),
        }),
        ('Инструкции', {
            'fields': ('instructions',),
        }),
        ('Медиа', {
            'fields': ('original_image_preview', 'cropped_image_preview', 'alt_text'),
        }),
        ('Данные для дебатов (Part 3)', {
            'fields': ('debate_data',),
            'classes': ('collapse',),
        }),
        ('OCR / Validation data', {
            'fields': ('validation_data',),
            'classes': ('collapse',),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(boolean=True, description='Изображение')
    def has_image(self, obj):
        return bool(obj.cropped_image or obj.original_image)

    @admin.display(description='Вопросов')
    def question_count(self, obj):
        return obj.questions.count()

    @admin.display(description='Оригинал')
    def original_image_preview(self, obj):
        if obj.original_image:
            return format_html(
                '<img src="{}" style="max-width:600px; max-height:450px; border-radius:6px;" />',
                obj.original_image.url,
            )
        from django.utils.safestring import mark_safe
        return mark_safe('<span style="color:#999;">Нет изображения</span>')

    @admin.display(description='Обрезанное')
    def cropped_image_preview(self, obj):
        if obj.cropped_image:
            return format_html(
                '<img src="{}" style="max-width:600px; max-height:450px; border-radius:6px;" />',
                obj.cropped_image.url,
            )
        from django.utils.safestring import mark_safe
        return mark_safe('<span style="color:#999;">Нет обрезанного изображения</span>')


@admin.register(SpeakingQuestion)
class SpeakingQuestionAdmin(admin.ModelAdmin):
    list_display = ('part', 'question_number', 'short_text', 'has_audio')
    list_filter = (
        ('part__test', admin.RelatedOnlyFieldListFilter),
        'part__part_number',
    )
    search_fields = ('question_text', 'part__test__name')
    ordering = ('part__test', 'part__part_number', 'question_number')
    readonly_fields = ('part', 'question_number', 'question_text', 'audio_file')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Текст (preview)')
    def short_text(self, obj):
        text = (obj.question_text or '')[:80]
        return f'{text}…' if len(obj.question_text or '') > 80 else text

    @admin.display(boolean=True, description='TTS Audio')
    def has_audio(self, obj):
        return bool(obj.audio_file)


# ---------------------------------------------------------------------------
# YouTube Video Lessons
# ---------------------------------------------------------------------------

class QuizQuestionInline(admin.TabularInline):
    model = QuizQuestion
    extra = 0
    fields = ('trigger_time_seconds', 'question_text', 'correct_option_index', 'options')
    readonly_fields = ('options',)
    ordering = ('trigger_time_seconds',)


@admin.register(VideoLesson)
class VideoLessonAdmin(admin.ModelAdmin):
    list_display = ('title', 'youtube_id', 'cefr_level', 'is_public', 'created_by', 'quiz_count', 'created_at')
    list_filter = ('cefr_level', 'is_public')
    search_fields = ('title', 'youtube_id')
    readonly_fields = ('youtube_id', 'transcript_json', 'thumbnail_url', 'created_at', 'quiz_count_display')
    list_editable = ('is_public',)
    ordering = ('-created_at',)
    inlines = [QuizQuestionInline]

    @admin.display(description='Quiz questions')
    def quiz_count_display(self, obj):
        return obj.quiz_questions.count()

    @admin.display(description='Quizzes')
    def quiz_count(self, obj):
        return obj.quiz_questions.count()


@admin.register(QuizQuestion)
class QuizQuestionAdmin(admin.ModelAdmin):
    list_display = ('video', 'trigger_time_seconds', 'short_question', 'correct_option_index')
    list_filter = (('video', admin.RelatedOnlyFieldListFilter),)
    search_fields = ('question_text', 'video__title')
    ordering = ('video', 'trigger_time_seconds')

    @admin.display(description='Question (preview)')
    def short_question(self, obj):
        return obj.question_text[:80] + ('…' if len(obj.question_text) > 80 else '')



# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------

@admin.register(WordCache)
class WordCacheAdmin(admin.ModelAdmin):
    list_display = ('word', 'part_of_speech', 'register', 'transcription', 'created_at')
    list_filter = ('register', 'part_of_speech')
    search_fields = ('word',)
    readonly_fields = ('created_at',)
    ordering = ('word',)


@admin.register(DictionaryEntry)
class DictionaryEntryAdmin(admin.ModelAdmin):
    list_display = ('cached_word', 'user', 'source', 'created_at')
    list_filter = ('source',)
    search_fields = ('cached_word__word', 'user__username', 'user__email')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)
