"""
Custom template tags and filters for the CEFR Exam test-taking interface.

Provides:
  - render_blanks: Replaces {N} markers in passage text with <input> fields,
    mapping the printed question number to the actual database Question.id.
"""

import re

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter(name='render_blanks')
def render_blanks(passage_text, questions_qs):
    """
    Replace {N} placeholders in passage_text with HTML text inputs.

    Args:
        passage_text: The Part's passage_text containing markers like {30}, {31}.
        questions_qs: A QuerySet (or list) of Question objects for this Part.
                      Each question's `global_question_number` corresponds to
                      the printed number N in {N}.

    Returns:
        Safe HTML string with <input> elements replacing each {N} placeholder.

    Usage in template:
        {{ part.passage_text|render_blanks:part.questions.all }}
    """
    if not passage_text:
        return ''

    # Build a mapping: global_question_number -> question.id
    number_to_id = {}
    for q in questions_qs:
        if q.global_question_number is not None:
            number_to_id[q.global_question_number] = q.id
        # Fallback: also try using question_number
        number_to_id[q.question_number] = q.id

    def replace_marker(match):
        """Replace a single {N} marker with an <input> field."""
        number = int(match.group(1))
        question_id = number_to_id.get(number)
        if question_id is None:
            # If we can't map the number, leave it as visible text
            return f'<span class="text-red-400 font-bold">({number})</span>'
        return (
            f'<span class="inline-flex items-center mx-1">'
            f'<span class="text-xs font-bold text-indigo-400 mr-1">{number}.</span>'
            f'<input type="text" name="question_{question_id}" '
            f'id="blank_{question_id}" '
            f'class="inline-block w-32 sm:w-40 px-2 py-1 text-sm '
            f'bg-slate-700/50 border border-slate-500/50 rounded-lg '
            f'text-white placeholder-slate-400 '
            f'focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent '
            f'transition-all duration-200" '
            f'placeholder="..." autocomplete="off">'
            f'</span>'
        )

    # Replace all {N} markers
    result = re.sub(r'\{(\d+)\}', replace_marker, passage_text)

    # Convert newlines to <br> for proper display
    result = result.replace('\n', '<br>')

    return mark_safe(result)


@register.filter(name='get_item')
def get_item(dictionary, key):
    """
    Access a dictionary value by key in a template.
    Usage: {{ my_dict|get_item:key }}
    """
    if isinstance(dictionary, dict):
        return dictionary.get(key)
    return None
