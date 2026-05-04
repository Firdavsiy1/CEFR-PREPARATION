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


@register.filter(name='feedback_lang')
def feedback_lang(feedback_json, lang):
    """
    Return the feedback text for the given language from a feedback_json dict.
    Falls back: requested lang → 'en' → 'ru' → first available value → ''.

    Usage:
        {{ sub.feedback_json|feedback_lang:current_language }}
        {{ sub.feedback_json|feedback_lang:current_language|default:sub.feedback_text }}
    """
    if not isinstance(feedback_json, dict) or not feedback_json:
        return ''
    return (
        feedback_json.get(lang)
        or feedback_json.get('en')
        or feedback_json.get('ru')
        or next(iter(feedback_json.values()), '')
    )


@register.filter(name='explanation_lang')
def explanation_lang(correction_dict, lang):
    """
    Return the explanation in the given language from a correction dict.
    Looks in correction_dict['explanation_i18n'][lang], falls back to
    correction_dict['explanation'].

    Usage:
        {% for corr in sub.corrections_json %}
          {{ corr|explanation_lang:current_language }}
        {% endfor %}
    """
    if not isinstance(correction_dict, dict):
        return ''
    i18n = correction_dict.get('explanation_i18n')
    if isinstance(i18n, dict):
        result = (
            i18n.get(lang)
            or i18n.get('en')
            or i18n.get('ru')
            or next(iter(i18n.values()), '')
        )
        if result:
            return result
    return correction_dict.get('explanation', '')



@register.filter(name='get_list_item')
def get_list_item(value, index):
    """Return list item by index (safe for templates)."""
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    if isinstance(value, (list, tuple)) and 0 <= idx < len(value):
        return value[idx]
    return None


@register.filter(name='split_paragraphs')
def split_paragraphs(text):
    """Split text by blank lines into paragraph list."""
    if not text:
        return []
    parts = re.split(r'\n\n+', text.strip())
    return [p.strip() for p in parts if p.strip()]


@register.filter(name='strip_option_prefix')
def strip_option_prefix(text):
    """Remove leading 'A) ' style prefix from option text."""
    if not text:
        return ''
    return re.sub(r'^[A-Z]\)\s*', '', text.strip())


@register.filter(name='strip_leading_number')
def strip_leading_number(text):
    """Remove leading '7. ' style prefix from paragraph text."""
    if not text:
        return ''
    return re.sub(r'^\d+\.\s*', '', text.strip())


@register.filter(name='render_reading_blanks')
def render_reading_blanks(passage_content, questions_qs):
    """
    Replace [GAP] markers in reading passage content with inline <input> fields.

    Each [GAP] is replaced sequentially with an input for the corresponding
    fill_in_the_blank question (ordered by question_number).

    Args:
        passage_content: The passage text containing [GAP] markers.
        questions_qs: QuerySet of ReadingQuestion objects for this part.

    Usage:
        {{ part.passage.content|render_reading_blanks:part.questions.all }}
    """
    if not passage_content:
        return ''

    # Only fill_in_the_blank questions map to gaps
    blank_questions = sorted(
        [q for q in questions_qs if q.question_type == 'fill_in_the_blank'],
        key=lambda q: q.question_number,
    )

    gap_iter = iter(blank_questions)

    def replace_gap(match):
        try:
            q = next(gap_iter)
        except StopIteration:
            return '<span class="text-red-400 font-bold">[GAP]</span>'
        return (
            f'<span class="rg-wrap">'
            f'<span class="rg-num">{q.question_number}.</span>'
            f'<input type="text" name="reading_q_{q.id}" '
            f'id="reading_blank_{q.id}" '
            f'class="rg-input" '
            f'placeholder="..." autocomplete="off">'
            f'</span>'
        )

    # Replace [GAP] markers
    result = re.sub(r'\[GAP\]', replace_gap, passage_content)

    # Preserve paragraph breaks; clean up within each paragraph
    paragraphs = re.split(r'\n\s*\n', result)
    html_parts = []
    for para in paragraphs:
        para = re.sub(r'\s*\n\s*', ' ', para).strip()
        para = re.sub(r'\s{2,}', ' ', para)
        if para:
            html_parts.append(f'<p class="mb-5 leading-relaxed">{para}</p>')
    return mark_safe(''.join(html_parts))


@register.filter(name='render_reading_passage')
def render_reading_passage(passage_content):
    """
    Render reading passage with paragraph breaks preserved.

    For readability, [GAP] markers are shown as a neutral blank line,
    while actual answer inputs are rendered in the question block below.
    """
    if not passage_content:
        return ''

    text = str(passage_content)
    text = text.replace('[GAP]', '_____')

    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    if not paragraphs:
        safe_text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        safe_text = safe_text.replace('\n', '<br>')
        return mark_safe(safe_text)

    html_parts = []
    for para in paragraphs:
        safe_para = para.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        safe_para = safe_para.replace('\n', ' ')
        safe_para = re.sub(r'\s{2,}', ' ', safe_para)
        html_parts.append(f'<p class="mb-5 leading-relaxed">{safe_para}</p>')

    return mark_safe(''.join(html_parts))
