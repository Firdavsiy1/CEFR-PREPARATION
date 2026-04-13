"""
Django Admin configuration for the CEFR Exams application.

Key features:
  - Inline editing for Parts within Tests, Questions within Parts
  - Side-by-side view of OCR text and original question screenshot
  - Image previews (question screenshots and map images)
  - Read-only attempt/answer records (no accidental data mutation)
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import Test, Part, Question, Choice, UserAttempt, UserAnswer


# ---------------------------------------------------------------------------
# Inline admin classes
# ---------------------------------------------------------------------------

class PartInline(admin.TabularInline):
    model = Part
    extra = 0
    fields = (
        'part_number', 'points_per_question', 'audio_file',
        'question_image', 'map_image', 'instructions',
    )
    readonly_fields = ('points_per_question',)
    show_change_link = True


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 0
    fields = (
        'question_number', 'global_question_number', 'question_type',
        'correct_answer', 'question_text',
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


# ---------------------------------------------------------------------------
# Test admin
# ---------------------------------------------------------------------------

@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = ('name', 'test_type', 'is_active', 'part_count',
                    'question_count', 'created_at')
    list_filter = ('test_type', 'is_active')
    search_fields = ('name',)
    list_editable = ('is_active',)
    inlines = [PartInline]

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
    list_filter = ('test', 'part_number')
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
                'question_type', 'correct_answer',
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
    list_filter = ('question__part__test', 'question__part__part_number')
    search_fields = ('text',)


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
    list_filter = ('test', 'user')
    date_hierarchy = 'started_at'
    readonly_fields = (
        'user', 'test', 'started_at', 'completed_at',
        'total_correct', 'total_questions', 'total_score',
        'max_possible_score',
    )
    inlines = [UserAnswerInline]

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
    list_filter = ('is_correct', 'attempt__test')
    readonly_fields = ('attempt', 'question', 'given_answer', 'is_correct')

    def has_add_permission(self, request):
        return False
