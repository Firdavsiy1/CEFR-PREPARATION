"""
Views for the CEFR Exam test-taking interface.

Views:
  - dashboard_view: Lists all active tests for the student to choose from.
  - take_test_view: GET renders the full test form; POST grades and records the attempt.
"""

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
import json
from django.db import connection

from .models import (
    Test, Question, UserAttempt, UserAnswer,
    WritingTask, WritingSubmission, AutoSaveDraft, TabBlurEvent,
    ReadingTest, ReadingPart, ReadingPassage, ReadingQuestion, ReadingUserAnswer,
    SpeakingPart, SpeakingQuestion, SpeakingSubmission,
    ChatSession, ChatMessage,
    WordCache, DictionaryEntry,
)
from .services import get_skill_radar_data, get_recommendations


# ---------------------------------------------------------------------------
# Landing Page
# ---------------------------------------------------------------------------

def landing_page_view(request):
    """
    Render the landing page for unauthenticated users.
    Authenticated users are redirected to the dashboard.
    """
    if request.user.is_authenticated:
        return redirect('exams:dashboard')
    
    context = {
        'hide_navbar': False,
        'hide_footer': False,
    }
    return render(request, 'exams/landing.html', context)


# ---------------------------------------------------------------------------
# Dashboard — list of available tests
# ---------------------------------------------------------------------------

@login_required
def dashboard_view(request, category=None):
    """Display categories or tests within a category."""
    
    # Auto-cleanup abandoned tests for this user (older than 2 hours)
    cutoff = timezone.now() - timezone.timedelta(hours=2)
    UserAttempt.objects.filter(
        user=request.user,
        completed_at__isnull=True,
        started_at__lt=cutoff
    ).delete()

    categories = [
        {'id': 'listening', 'name': 'Listening', 'active': True, 'image': 'images/listening_card.png'},
        {'id': 'reading', 'name': 'Reading', 'active': True, 'image': 'images/reading_card.png'},
        {'id': 'writing', 'name': 'Writing', 'active': True, 'image': 'images/writing_card.png'},
        {'id': 'speaking', 'name': 'Speaking', 'active': True, 'image': 'images/speaking_card.png'},
    ]

    context = {
        'show_categories': category is None,
        'categories': categories,
    }

    if category:
        search_q = request.GET.get('q', '').strip()
        tests = (
            Test.objects
            .filter(is_active=True, is_deleted=False, is_class_only=False, test_type=category)
            .prefetch_related(
                'parts__questions', 'writing_tasks',
                'reading_parts__questions',
                'speaking_parts__questions',
            )
            .order_by('-pk')
        )
        if search_q:
            tests = tests.filter(name__icontains=search_q)
        
        # Writing tests don't have 'parts' relationship, but when split they contain ' - Part ' 
        # just like other test types split parts.
        full_tests = [t for t in tests if ' - Part ' not in t.name]
        micro_tests = [t for t in tests if ' - Part ' in t.name]
        
        category_name = next((c['name'] for c in categories if c['id'] == category), category.capitalize())
        
        context.update({
            'full_tests': full_tests,
            'micro_tests': micro_tests,
            'category_id': category,
            'category_name': category_name,
            'search_q': search_q,
        })

    return render(request, 'exams/dashboard.html', context)


# ---------------------------------------------------------------------------
# Multi-Page Test Flow
# ---------------------------------------------------------------------------

@login_required
def test_tutorial_view(request, test_id):
    """
    Displays a tutorial and sound check page before starting the actual test.
    Writing tests get a writing-specific tutorial (no sound check needed).
    """
    test = get_object_or_404(Test, pk=test_id, is_deleted=False)
    
    if not test.is_active or test.is_class_only:
        from django.db import models
        has_classroom_access = test.classrooms.filter(
            models.Q(memberships__student=request.user) | models.Q(mentor=request.user)
        ).exists()
        if not has_classroom_access:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
    if test.test_type == 'writing':
        template = 'exams/tutorial_writing.html'
    elif test.test_type == 'reading':
        template = 'exams/tutorial_reading.html'
    elif test.test_type == 'speaking':
        template = 'exams/tutorial_speaking.html'
    else:
        template = 'exams/tutorial.html'
    return render(request, template, {'test': test})

@login_required
def start_test_view(request, test_id):
    """
    Creates a new UserAttempt and redirects to the first available part.
    """
    test = get_object_or_404(Test, pk=test_id, is_deleted=False)
    
    if not test.is_active or test.is_class_only:
        from django.db import models
        has_classroom_access = test.classrooms.filter(
            models.Q(memberships__student=request.user) | models.Q(mentor=request.user)
        ).exists()
        if not has_classroom_access:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied

    # Create the attempt record (timer starts now via started_at auto_now_add)
    attempt = UserAttempt.objects.create(
        user=request.user,
        test=test
    )
    
    if test.test_type == 'writing':
        return redirect('exams:take_writing_test', attempt_id=attempt.id)
    
    if test.test_type == 'reading':
        first_reading_part = test.reading_parts.order_by('part_number').first()
        if first_reading_part:
            return redirect('exams:take_reading_part', attempt_id=attempt.id, part_number=first_reading_part.part_number)
        return redirect('exams:take_reading_test', attempt_id=attempt.id)
    
    if test.test_type == 'speaking':
        return redirect('exams:take_speaking_test', attempt_id=attempt.id)
    
    first_part = test.parts.order_by('part_number').first()
    if first_part:
        return redirect('exams:take_test_part', attempt_id=attempt.id, part_number=first_part.part_number)
    
    # Edge case: test has zero parts
    return _finalize_test(request, attempt)


@login_required
def take_test_part_view(request, attempt_id, part_number):
    """
    GET:  Render a specific part of the test with a 1-hour timer.
    POST: Save current part answers, then move to next part or finalize.
    """
    attempt = get_object_or_404(
        UserAttempt.objects.select_related('test'),
        pk=attempt_id,
        user=request.user,
        completed_at__isnull=True  # Ensure test isn't already finished
    )
    
    # Calculate time remaining (1 hour = 3600s)
    elapsed = (timezone.now() - attempt.started_at).total_seconds()
    time_remaining_seconds = max(0, 3600 - int(elapsed))
    
    if time_remaining_seconds <= 0:
        # Time is up! Finalize what we have
        return _finalize_test(request, attempt)

    # Fetch the specific part
    test = attempt.test
    try:
        part = test.parts.get(part_number=part_number)
    except test.parts.model.DoesNotExist:
        # If part doesn't exist, just finalize (or redirect to dashboard)
        return _finalize_test(request, attempt)

    # Determine all parts to support dynamic lengths
    all_parts = list(test.parts.all().order_by('part_number'))
    part_numbers = [p.part_number for p in all_parts]
    try:
        current_idx = part_numbers.index(part_number)
    except ValueError:
        return _finalize_test(request, attempt)
        
    next_part_number = part_numbers[current_idx + 1] if current_idx + 1 < len(part_numbers) else None

    if request.method == 'POST':
        # Save answers for CURRENT part questions only
        questions = part.questions.all()
        for question in questions:
            key = f'question_{question.id}'
            given_answer = request.POST.get(key, '').strip()

            # Use update_or_create to allow resuming/editing before moving next
            UserAnswer.objects.update_or_create(
                attempt=attempt,
                question=question,
                defaults={'given_answer': given_answer}
            )

        # Clean up draft data for submitted questions
        draft = AutoSaveDraft.objects.filter(attempt=attempt).first()
        if draft and draft.data:
            submitted_keys = {f'question_{q.id}' for q in questions}
            draft.data = {k: v for k, v in draft.data.items() if k not in submitted_keys}
            draft.save(update_fields=['data'])

        # Determine next step
        if next_part_number:
            return redirect('exams:take_test_part', attempt_id=attempt.id, part_number=next_part_number)
        else:
            return _finalize_test(request, attempt)

    # GET — render the single part form
    # Prefetch questions and choices
    part_with_data = test.parts.filter(part_number=part_number).prefetch_related('questions__choices').first()

    # Load draft answers for pre-populating form fields
    draft_answers = {}
    draft = AutoSaveDraft.objects.filter(attempt=attempt).first()
    if draft and draft.data:
        draft_answers = draft.data

    context = {
        'attempt': attempt,
        'test': test,
        'part': part_with_data,
        'part_number': part_number,
        'all_parts': all_parts,
        'next_part_number': next_part_number,
        'total_parts': len(all_parts),
        'current_part_num': current_idx + 1,
        'time_remaining': time_remaining_seconds,
        'endtime_iso': (attempt.started_at + timezone.timedelta(hours=1)).isoformat(),
        'draft_answers': json.dumps(draft_answers),
        'hide_navbar': True,
    }
    return render(request, 'exams/take_test.html', context)


@login_required
def take_writing_test_view(request, attempt_id):
    """
    Render all 3 writing tasks on a single page, allow submission.
    """
    attempt = get_object_or_404(
        UserAttempt.objects.select_related('test'),
        pk=attempt_id,
        user=request.user,
        completed_at__isnull=True
    )
    
    # Calculate time remaining (1 hour = 3600s)
    elapsed = (timezone.now() - attempt.started_at).total_seconds()
    time_remaining_seconds = max(0, 3600 - int(elapsed))
    
    if time_remaining_seconds <= 0 and request.method != 'POST':
        return _finalize_test(request, attempt)
    
    test = attempt.test
    tasks = list(test.writing_tasks.all().order_by('order'))
    
    if request.method == 'POST':
        with transaction.atomic():
            for task in tasks:
                field_name = f'task_{task.id}'
                given_text = request.POST.get(field_name, '').strip()
                words = len(given_text.split()) if given_text else 0

                WritingSubmission.objects.update_or_create(
                    attempt=attempt,
                    task=task,
                    defaults={'submitted_text': given_text, 'word_count': words}
                )
            attempt.completed_at = timezone.now()
            attempt.save()
            
            if hasattr(attempt.user, 'profile'):
                activity_type = 'mini_exam' if ' - Part ' in attempt.test.name else 'exam'
                attempt.user.profile.record_activity(activity_type)

        # Clean up draft
        AutoSaveDraft.objects.filter(attempt=attempt).delete()

        # Kick off background grading via Celery
        from exams.tasks import grade_writing_submission
        grade_writing_submission.delay(attempt.id)

        messages.success(request, "Writing test submitted! AI evaluation is in progress.")
        return redirect('exams:test_result', attempt_id=attempt.id)

    # Load draft answers for pre-populating textareas
    draft_answers = {}
    draft = AutoSaveDraft.objects.filter(attempt=attempt).first()
    if draft and draft.data:
        draft_answers = draft.data

    context = {
        'attempt': attempt,
        'test': test,
        'tasks': tasks,
        'time_remaining': time_remaining_seconds,
        'endtime_iso': (attempt.started_at + timezone.timedelta(hours=1)).isoformat(),
        'draft_answers': json.dumps(draft_answers),
        'hide_navbar': True,
    }
    return render(request, 'exams/take_writing_test.html', context)


# ---------------------------------------------------------------------------
# Reading Test Flow
# ---------------------------------------------------------------------------

@login_required
def take_reading_test_view(request, attempt_id):
    """
    Redirect to the first reading part for backwards compatibility.
    """
    attempt = get_object_or_404(
        UserAttempt.objects.select_related('test'),
        pk=attempt_id,
        user=request.user,
        completed_at__isnull=True
    )
    first_part = attempt.test.reading_parts.order_by('part_number').first()
    if first_part:
        return redirect('exams:take_reading_part', attempt_id=attempt.id, part_number=first_part.part_number)
    return _finalize_reading_test(request, attempt)


@login_required
def take_reading_part_view(request, attempt_id, part_number):
    """
    Render a single reading part with progress bar and part-by-part navigation.
    POST: Save current part answers, then move to next part or finalize.
    """
    attempt = get_object_or_404(
        UserAttempt.objects.select_related('test'),
        pk=attempt_id,
        user=request.user,
        completed_at__isnull=True
    )

    elapsed = (timezone.now() - attempt.started_at).total_seconds()
    time_remaining_seconds = max(0, 3600 - int(elapsed))

    if time_remaining_seconds <= 0 and request.method != 'POST':
        return _finalize_reading_test(request, attempt)

    test = attempt.test
    all_parts = list(
        test.reading_parts
        .select_related('passage')
        .prefetch_related('questions')
        .order_by('part_number')
    )

    part_numbers = [p.part_number for p in all_parts]
    if part_number not in part_numbers:
        return _finalize_reading_test(request, attempt)

    current_idx = part_numbers.index(part_number)
    part = all_parts[current_idx]
    next_part_number = part_numbers[current_idx + 1] if current_idx + 1 < len(part_numbers) else None
    prev_part_number = part_numbers[current_idx - 1] if current_idx > 0 else None

    if request.method == 'POST':
        questions = part.questions.all()
        for question in questions:
            key = f'reading_q_{question.id}'
            given_answer = request.POST.get(key, '').strip()
            ReadingUserAnswer.objects.update_or_create(
                attempt=attempt,
                question=question,
                defaults={'given_answer': given_answer},
            )

        # Clean up draft data for submitted questions
        draft = AutoSaveDraft.objects.filter(attempt=attempt).first()
        if draft and draft.data:
            submitted_keys = {f'reading_q_{q.id}' for q in questions}
            draft.data = {k: v for k, v in draft.data.items() if k not in submitted_keys}
            draft.save(update_fields=['data'])

        if request.POST.get('_go_back') and prev_part_number:
            return redirect('exams:take_reading_part', attempt_id=attempt.id, part_number=prev_part_number)
        elif next_part_number:
            return redirect('exams:take_reading_part', attempt_id=attempt.id, part_number=next_part_number)
        else:
            return _finalize_reading_test(request, attempt)

    draft_answers = {}
    draft = AutoSaveDraft.objects.filter(attempt=attempt).first()
    if draft and draft.data:
        draft_answers = draft.data

    context = {
        'attempt': attempt,
        'test': test,
        'part': part,
        'part_number': part_number,
        'all_parts': all_parts,
        'next_part_number': next_part_number,
        'prev_part_number': prev_part_number,
        'total_parts': len(all_parts),
        'current_part_num': current_idx + 1,
        'time_remaining': time_remaining_seconds,
        'endtime_iso': (attempt.started_at + timezone.timedelta(hours=1)).isoformat(),
        'draft_answers': json.dumps(draft_answers),
        'hide_navbar': True,
    }
    return render(request, 'exams/take_reading_test.html', context)


def _finalize_reading_test(request, attempt):
    """Calculate final scores for a reading test and mark as completed."""
    all_questions = ReadingQuestion.objects.filter(
        part__test=attempt.test
    ).select_related('part')

    total_correct = 0
    total_questions = 0

    with transaction.atomic():
        for question in all_questions:
            total_questions += 1

            # Check if answer was already saved part-by-part
            existing = ReadingUserAnswer.objects.filter(
                attempt=attempt, question=question
            ).first()

            if existing:
                if existing.is_correct:
                    total_correct += 1
            else:
                # Fallback: try POST data (for time-expired auto-submit)
                key = f'reading_q_{question.id}'
                given_answer = request.POST.get(key, '').strip() if request.method == 'POST' else ''
                obj, _ = ReadingUserAnswer.objects.update_or_create(
                    attempt=attempt,
                    question=question,
                    defaults={'given_answer': given_answer},
                )
                if obj.is_correct:
                    total_correct += 1

        attempt.total_correct = total_correct
        attempt.total_questions = total_questions
        attempt.total_score = float(total_correct)
        attempt.max_possible_score = float(total_questions)
        attempt.completed_at = timezone.now()
        attempt.save()

        if hasattr(attempt.user, 'profile'):
            activity_type = 'mini_exam' if ' - Part ' in attempt.test.name else 'exam'
            attempt.user.profile.record_activity(activity_type)

    AutoSaveDraft.objects.filter(attempt=attempt).delete()
    messages.success(request, f"Test completed! Score: {total_correct}/{total_questions}")
    return redirect('exams:test_result', attempt_id=attempt.id)


def _grade_writing_submission_background(attempt_id):
    """Background thread to grade Writing submissions via Gemini."""
    connection.close()  # Force new connection in thread
    import os
    try:
        from google import genai
        from google.genai import types as T
        
        attempt = UserAttempt.objects.get(pk=attempt_id)
        submissions = attempt.writing_submissions.select_related('task').all()
        
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project_id:
            adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
            if os.path.exists(adc_path):
                import json
                with open(adc_path) as f:
                    project_id = json.load(f).get("quota_project_id")
                    
        client = genai.Client(vertexai=True, project=project_id, location="global")
        
        for sub in submissions:
            task = sub.task
            
            prompt = f"""Act as an expert CEFR Examiner for B2 Level. 
Evaluate the following student submission for a writing task.
Task Type: {task.get_task_type_display()}
Target Length: {task.min_words} to {task.max_words} words.
Task Instruction: {task.prompt}
Incoming text to reply to (if any): {task.input_text}

Student Submission ({sub.word_count} words):
{sub.submitted_text}

Provide detailed feedback with grammatical, lexical, and structural analysis.
Assign an estimated CEFR Level (A1, A2, B1, B2, C1).

Provide feedback in three languages. KEEP English linguistic/CEFR terms in all languages
(e.g. "B2", "cohesion", "lexical range", "discourse markers" — do NOT translate these).

Return strictly in this JSON format:
{{
  "estimated_level": "B2",
  "feedback_i18n": {{
    "en": "Detailed English feedback with English terminology.",
    "ru": "Подробный отзыв на русском с сохранением английских терминов (B2, cohesion, lexical range и т.д.).",
    "uz": "O'zbek tilida batafsil fikr-mulohaza, ingliz atamalari saqlanadi (B2, cohesion, lexical range va b.)."
  }},
  "corrections": [
    {{
      "original": "incorrect phrase",
      "correction": "correct phrase",
      "explanation_i18n": {{
        "en": "Why this is incorrect in English.",
        "ru": "Объяснение на русском языке.",
        "uz": "O'zbek tilidagi tushuntirish."
      }}
    }}
  ]
}}"""
            
            generate_cfg = T.GenerateContentConfig(response_mime_type="application/json")
            resp = client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=[prompt],
                config=generate_cfg,
            )
            
            raw_text = resp.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.startswith("```"):
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
                
            result = json.loads(raw_text.strip())
            
            sub.estimated_level = result.get('estimated_level', 'N/A')
            # Multilingual feedback: prefer feedback_i18n, fall back to legacy 'feedback'
            feedback_i18n = result.get('feedback_i18n')
            if isinstance(feedback_i18n, dict) and feedback_i18n:
                sub.feedback_json = feedback_i18n
                sub.feedback_text = feedback_i18n.get('en') or next(iter(feedback_i18n.values()), '')
            else:
                raw_feedback = result.get('feedback', '')
                sub.feedback_text = raw_feedback
                sub.feedback_json = {'en': raw_feedback, 'ru': raw_feedback, 'uz': raw_feedback}
            sub.corrections_json = result.get('corrections', [])
            sub.is_graded = True
            sub.save()
            
    except Exception as e:
        print(f"Error in background grading: {e}")
    finally:
        connection.close()


def _finalize_test(request, attempt):
    """
    Calculate final scores and mark test as completed.
    """
    total_correct = 0
    total_questions = 0
    total_score = 0.0
    max_possible_score = 0.0

    all_answers = attempt.answers.select_related('question__part')
    
    # We need to ensure we account for ALL questions in the test,
    # even those the user never got to (if time ran out).
    all_questions = Question.objects.filter(part__test=attempt.test).select_related('part')
    
    # Map answers by question_id for easy lookup
    answer_map = {ans.question_id: ans for ans in all_answers}

    with transaction.atomic():
        for question in all_questions:
            total_questions += 1
            max_possible_score += question.part.points_per_question
            
            answer = answer_map.get(question.id)
            if not answer:
                # Create a blank answer if missing (auto-grades to incorrect)
                answer = UserAnswer.objects.create(
                    attempt=attempt,
                    question=question,
                    given_answer=''
                )
            
            if answer.is_correct:
                total_correct += 1
                total_score += question.part.points_per_question

        attempt.total_correct = total_correct
        attempt.total_questions = total_questions
        attempt.total_score = total_score
        attempt.max_possible_score = max_possible_score
        attempt.completed_at = timezone.now()
        attempt.save()

        if hasattr(attempt.user, 'profile'):
            activity_type = 'mini_exam' if ' - Part ' in attempt.test.name else 'exam'
            attempt.user.profile.record_activity(activity_type)

    # Clean up draft
    AutoSaveDraft.objects.filter(attempt=attempt).delete()

    messages.success(request, f"Test completed! Final Score: {total_score}/{max_possible_score}")
    return redirect('exams:test_result', attempt_id=attempt.id)


# ---------------------------------------------------------------------------
# Auto-Save & Anti-Cheat APIs
# ---------------------------------------------------------------------------

@login_required
@require_POST
def api_autosave(request, attempt_id):
    """Accept partial answers via JSON and persist as a draft."""
    attempt = get_object_or_404(
        UserAttempt,
        pk=attempt_id,
        user=request.user,
        completed_at__isnull=True,
    )
    # Reject if time is up
    elapsed = (timezone.now() - attempt.started_at).total_seconds()
    if elapsed > 3600:
        return JsonResponse({'status': 'expired'}, status=410)

    # Server-side throttle: max 1 save per 5 seconds per attempt
    throttle_key = f'autosave_throttle:{attempt_id}'
    if cache.get(throttle_key):
        return JsonResponse({'status': 'throttled'}, status=429)
    cache.set(throttle_key, 1, timeout=5)

    try:
        # Handle both JSON body (fetch) and FormData (sendBeacon)
        content_type = request.content_type or ''
        if 'application/json' in content_type:
            body = json.loads(request.body)
        else:
            payload = request.POST.get('payload', '{}')
            body = json.loads(payload)
        answers = body.get('answers', {})
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)

    if not isinstance(answers, dict):
        return JsonResponse({'status': 'error', 'message': 'answers must be an object'}, status=400)

    draft, _ = AutoSaveDraft.objects.update_or_create(
        attempt=attempt,
        defaults={'data': answers},
    )
    return JsonResponse({'status': 'ok', 'saved': len(answers)})


@login_required
@require_POST
def api_log_tab_blur(request, attempt_id):
    """Log a tab-blur event for anti-cheat tracking."""
    attempt = get_object_or_404(
        UserAttempt,
        pk=attempt_id,
        user=request.user,
        completed_at__isnull=True,
    )
    duration = None
    try:
        body = json.loads(request.body)
        duration = body.get('duration')
        if duration is not None:
            duration = float(duration)
    except (json.JSONDecodeError, AttributeError, ValueError, TypeError):
        pass

    TabBlurEvent.objects.create(attempt=attempt, duration_seconds=duration)
    total = attempt.tab_blur_events.count()
    return JsonResponse({'status': 'ok', 'total_blurs': total})


# ---------------------------------------------------------------------------
# Test Results and History
# ---------------------------------------------------------------------------

@login_required
def test_result_view(request, attempt_id):
    """
    Display the results of a specific user attempt, including statistics,
    breakdown by part, and correct/incorrect answers.
    """
    attempt = get_object_or_404(
        UserAttempt.objects.select_related('test'),
        id=attempt_id,
        user=request.user
    )
    
    # We prefetch all answers and questions to render the results page efficiently
    if attempt.test.test_type == 'reading':
        answers = attempt.reading_answers.select_related('question__part').order_by('question__part__part_number', 'question__question_number')
    else:
        answers = attempt.answers.select_related('question__part').order_by('question__part__part_number', 'question__question_number')
    
    # Calculate stats per part natively
    part_results = attempt.get_part_results()
    
    writing_submissions = attempt.writing_submissions.select_related('task').order_by('task__order') if attempt.test.test_type == 'writing' else None

    speaking_submissions = (
        attempt.speaking_submissions.select_related('question__part').order_by('question__part__part_number', 'question__question_number')
        if attempt.test.test_type == 'speaking' else None
    )

    speaking_part_results = []
    if speaking_submissions:
        part_stats = {}
        for sub in speaking_submissions:
            part = sub.question.part
            part_num = part.part_number
            ppq = float(part.points_per_question)  # max pts per question for this part
            if part_num not in part_stats:
                part_stats[part_num] = {
                    'part_number': part_num,
                    'points_per_question': ppq,
                    'total_questions': 0,
                    'submitted_count': 0,
                    'evaluated_count': 0,
                    'weighted_score_sum': 0.0,
                }

            stats = part_stats[part_num]
            stats['total_questions'] += 1
            if sub.audio_file:
                stats['submitted_count'] += 1
            if sub.is_evaluated:
                stats['evaluated_count'] += 1
                # scale raw 0-10 Gemini score to part's max points
                stats['weighted_score_sum'] += round(float(sub.score or 0) * ppq / 10, 1)

        speaking_part_results = [part_stats[k] for k in sorted(part_stats.keys())]
        for stats in speaking_part_results:
            stats['max_weighted_score'] = round(stats['total_questions'] * stats['points_per_question'], 1)
            if stats['evaluated_count'] > 0:
                stats['weighted_score_earned'] = round(stats['weighted_score_sum'], 1)
            else:
                stats['weighted_score_earned'] = None

    tab_blur_count = attempt.tab_blur_events.count()

    context = {
        'attempt': attempt,
        'answers': answers,
        'part_results': part_results,
        'writing_submissions': writing_submissions,
        'speaking_submissions': speaking_submissions,
        'speaking_part_results': speaking_part_results,
        'tab_blur_count': tab_blur_count,
    }
    return render(request, 'exams/test_result.html', context)


@login_required
def api_speaking_result_status(request, attempt_id):
    """Return live speaking evaluation status for a completed attempt."""
    attempt = get_object_or_404(
        UserAttempt.objects.select_related('test'),
        id=attempt_id,
        user=request.user,
        test__test_type='speaking',
    )

    submissions = list(
        attempt.speaking_submissions
        .select_related('question__part')
        .order_by('question__part__part_number', 'question__question_number')
    )

    # Build per-submission payload including weighted score
    payload = []
    for sub in submissions:
        ppq = float(sub.question.part.points_per_question)
        weighted = round(float(sub.score) * ppq / 10, 1) if sub.is_evaluated else None
        payload.append({
            'id': sub.id,
            'part_number': sub.question.part.part_number,
            'is_evaluated': sub.is_evaluated,
            'feedback_text': sub.feedback_text or '',
            'feedback_json': sub.feedback_json or {},
            'estimated_level': sub.estimated_level or '',
            'transcript': sub.transcript or '',
            'raw_score': float(sub.score) if sub.is_evaluated else None,
            'max_score': ppq,
            'weighted_score': weighted,
        })

    # Aggregate per-part summaries for live section card updates
    part_summaries: dict[str, dict] = {}
    for sub in submissions:
        part_num = str(sub.question.part.part_number)
        ppq = float(sub.question.part.points_per_question)
        if part_num not in part_summaries:
            part_summaries[part_num] = {
                'points_per_question': ppq,
                'total': 0,
                'evaluated': 0,
                'weighted_earned': 0.0,
                'max_weighted': 0.0,
            }
        s = part_summaries[part_num]
        s['total'] += 1
        s['max_weighted'] = round(s['total'] * ppq, 1)
        if sub.is_evaluated:
            s['evaluated'] += 1
            s['weighted_earned'] = round(s['weighted_earned'] + float(sub.score) * ppq / 10, 1)

    return JsonResponse({
        'submissions': payload,
        'part_summaries': part_summaries,
        'all_evaluated': all(sub.is_evaluated for sub in submissions),
    })


@login_required
def exam_history_view(request, category=None):
    """
    Shows either the category selection page or the history for a specific category.
    """
    if not category:
        categories = [
            {'id': 'listening', 'name': 'Listening', 'image': 'images/listening_card.png', 'active': True},
            {'id': 'reading', 'name': 'Reading', 'image': 'images/reading_card.png', 'active': True},
            {'id': 'writing', 'name': 'Writing', 'image': 'images/writing_card.png', 'active': True},
            {'id': 'speaking', 'name': 'Speaking', 'image': 'images/speaking_card.png', 'active': True},
        ]
        radar_data = get_skill_radar_data(request.user)
        recommendations = get_recommendations(request.user)
        return render(request, 'exams/history.html', {
            'show_categories': True,
            'categories': categories,
            'radar_data': radar_data,
            # Pre-computed boolean — avoids fragile string-join comparison
            # in the template and keeps presentation logic out of HTML.
            'has_radar_data': any(v > 0 for v in radar_data['data']),
            'recommendations': recommendations,
        })

    if category not in ['listening', 'writing', 'reading', 'speaking']:
        messages.info(request, f"{category.title()} history is coming soon!")
        return redirect('exams:history')

    # Prefetch everything that get_part_results() and the template loop need
    # so the N+1 query problem is eliminated regardless of how many attempts
    # the user has completed.
    #   test__parts__questions — gives len(part.questions.all()) from cache
    #   answers__question__part — gives part_num for each answer from cache
    #   writing_submissions__task — used by the writing history branch
    all_attempts = list(UserAttempt.objects.filter(
        user=request.user,
        completed_at__isnull=False,
        test__test_type=category
    ).select_related('test')
     .prefetch_related(
         'test__parts__questions',
         'answers__question__part',
         'writing_submissions__task',
         'test__reading_parts__questions',
         'reading_answers__question__part',
     )
     .order_by('-started_at'))
    
    best_overall = None
    best_parts = {}
    
    cefr_scores = {'A1': 20, 'A2': 40, 'B1': 60, 'B2': 80, 'C1': 100, 'N/A': 0, '': 0}
    cefr_levels = {'A1': 1, 'A2': 2, 'B1': 3, 'B2': 4, 'C1': 5, 'N/A': 0, '': 0}

    for attempt in all_attempts:
        attempt.time_taken = attempt.completed_at - attempt.started_at
        
        # Calculate dynamic properties
        if attempt.test.test_type == 'writing':
            submissions = list(attempt.writing_submissions.all())
            total_words = sum(sub.word_count for sub in submissions)
            attempt.writing_words = total_words
            
            levels = [sub.estimated_level for sub in submissions if sub.estimated_level and sub.estimated_level != 'N/A']
            attempt.writing_level = max(levels, key=lambda x: cefr_levels.get(x, 0)) if levels else '...'
            
            # dynamically calculate score percentage for writing
            if attempt.max_possible_score == 0 and submissions:
                total_s = sum(cefr_scores.get(sub.estimated_level, 0) for sub in submissions)
                attempt.dynamic_score_pct = (total_s / (len(submissions) * 100)) * 100 if len(submissions) > 0 else 0
            else:
                attempt.dynamic_score_pct = attempt.score_percentage
        else:
            attempt.dynamic_score_pct = attempt.score_percentage
        
        # Check best overall
        if best_overall is None:
            best_overall = attempt
        else:
            if attempt.dynamic_score_pct > best_overall.dynamic_score_pct:
                best_overall = attempt
            elif attempt.dynamic_score_pct == best_overall.dynamic_score_pct:
                if attempt.time_taken < best_overall.time_taken:
                    best_overall = attempt
                    
        # Check best per part/task
        if attempt.test.test_type == 'writing':
            for sub in submissions:
                task_label = sub.task.get_task_type_display().split('(')[0].strip()
                score = cefr_scores.get(sub.estimated_level, 0)
                if task_label not in best_parts:
                    best_parts[task_label] = {'attempt': attempt, 'part': {'part_number': task_label}, 'pct': score}
                elif score > best_parts[task_label]['pct']:
                    best_parts[task_label] = {'attempt': attempt, 'part': {'part_number': task_label}, 'pct': score}
                elif score == best_parts[task_label]['pct']:
                    if attempt.time_taken < best_parts[task_label]['attempt'].time_taken:
                        best_parts[task_label] = {'attempt': attempt, 'part': {'part_number': task_label}, 'pct': score}
        else:
            part_results = attempt.get_part_results()
            for pr in part_results:
                part_num = pr['part_number']
                part_pct = (pr['score'] / pr['max_score']) * 100 if pr['max_score'] > 0 else 0
                
                if part_num not in best_parts:
                    best_parts[part_num] = {'attempt': attempt, 'part': pr, 'pct': part_pct}
                else:
                    if part_pct > best_parts[part_num]['pct']:
                        best_parts[part_num] = {'attempt': attempt, 'part': pr, 'pct': part_pct}
                    elif part_pct == best_parts[part_num]['pct']:
                        if attempt.time_taken < best_parts[part_num]['attempt'].time_taken:
                            best_parts[part_num] = {'attempt': attempt, 'part': pr, 'pct': part_pct}
                        
        total_seconds = int(attempt.time_taken.total_seconds())
        m = total_seconds // 60
        s = total_seconds % 60
        attempt.time_formatted = f"{m}m {s}s"

    # For writing, best_parts keys are strings, for others they are ints. 
    # Sorting mixed types in Python 3 is an error if not careful.
    if category == 'writing':
        best_parts_list = list(best_parts.values())
    else:
        best_parts_list = [best_parts[k] for k in sorted(best_parts.keys())]

    # Prepare chart data (use all attempts for progress timeline)
    chronological_attempts = list(reversed(all_attempts))
    chart_labels = []
    chart_scores = []
    for att in chronological_attempts:
        label = f"{att.test.name} ({att.started_at.strftime('%b %d')})"
        chart_labels.append(label)
        chart_scores.append(att.dynamic_score_pct)

    # Paginate: 20 cards per page
    paginator = Paginator(all_attempts, 20)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    context = {
        'show_categories': False,
        'category': category,
        'attempts': page_obj,
        'total_attempts_count': len(all_attempts),
        'page_obj': page_obj,
        'best_overall': best_overall,
        'best_parts': best_parts_list,
        # Passed via json_script filter in the template to avoid XSS.
        'chart_data': {'labels': chart_labels, 'scores': chart_scores},
    }
    return render(request, 'exams/history.html', context)

def take_speaking_test_view(request, attempt_id):
    """
    Main view for the student speaking test.
    GET: Renders the speaking test UI with all parts and questions.
    """
    attempt = get_object_or_404(
        UserAttempt.objects.select_related('test'),
        id=attempt_id,
        user=request.user,
        completed_at__isnull=True,
    )
    if attempt.test.test_type != 'speaking':
        return redirect('exams:dashboard')

    # Speaking now uses per-question/per-topic timers on the frontend,
    # so there is no longer a global one-hour session countdown here.
    base_parts = attempt.test.speaking_parts.order_by('part_number')
    for part in base_parts:
        if part.debate_data and not part.questions.exists():
            SpeakingQuestion.objects.get_or_create(
                part=part,
                question_number=1,
                defaults={
                    'question_text': 'Discuss the topic and support your opinion using the ideas provided.',
                },
            )

    parts = attempt.test.speaking_parts.prefetch_related('questions').order_by('part_number')

    label_map = {
        1: '1.1',
        2: '1.2',
        3: '2',
        4: '3',
    }
    parts_data = []
    for p in parts:
        display_label = label_map.get(p.part_number, str(p.part_number))
        raw_image_url = (p.cropped_image.url if p.cropped_image
                         else (p.original_image.url if p.original_image else ''))

        parts_data.append({
            'id': p.id,
            'part_number': p.part_number,
            'display_label': display_label,
            'ocr_label': str(
                (p.validation_data or {}).get('parts', [{}])[0].get('part_number', p.part_number)
            ).strip().replace(',', '.'),
            'image_url': raw_image_url if display_label in {'1.2', '2'} else '',
            'debate_data': p.debate_data or {},
            'questions': [
                {
                    'id': q.id,
                    'number': q.question_number,
                    'text': q.question_text,
                    'audio_url': q.audio_file.url if q.audio_file else '',
                }
                for q in p.questions.all()
            ],
        })

    context = {
        'attempt': attempt,
        'test': attempt.test,
        'parts': parts,
        'parts_data': parts_data,
        'hide_navbar': True,
    }
    return render(request, 'exams/take_speaking_test.html', context)


@login_required
@require_POST
def api_submit_speaking_answer(request, attempt_id):
    """Save a student's recorded audio for a single speaking question."""
    attempt = get_object_or_404(
        UserAttempt,
        pk=attempt_id,
        user=request.user,
        completed_at__isnull=True,
    )
    question_id = request.POST.get('question_id')
    audio_file = request.FILES.get('audio')
    duration = request.POST.get('duration', 0)

    if not question_id or not audio_file:
        return JsonResponse({'error': 'Missing question_id or audio'}, status=400)

    question = get_object_or_404(SpeakingQuestion, pk=question_id, part__test=attempt.test)

    submission, _ = SpeakingSubmission.objects.update_or_create(
        attempt=attempt,
        question=question,
        defaults={
            'audio_file': audio_file,
            'duration_seconds': float(duration),
        },
    )
    return JsonResponse({'status': 'ok', 'submission_id': submission.id})


@login_required
@require_POST
def finalize_speaking_test_view(request, attempt_id):
    """Finalise the speaking test from the student UI."""
    attempt = get_object_or_404(
        UserAttempt,
        pk=attempt_id,
        user=request.user,
        completed_at__isnull=True,
    )
    if attempt.test.test_type != 'speaking':
        return JsonResponse({'error': 'Not a speaking test'}, status=400)
    return _finalize_speaking_test(request, attempt)


def _finalize_speaking_test(request, attempt):
    """Mark a speaking attempt as completed and queue AI evaluation."""
    total_questions = SpeakingQuestion.objects.filter(part__test=attempt.test).count()
    with transaction.atomic():
        attempt.total_questions = total_questions
        attempt.total_correct = 0
        attempt.total_score = 0
        attempt.max_possible_score = total_questions * 10
        attempt.completed_at = timezone.now()
        attempt.save()
        
        if hasattr(attempt.user, 'profile'):
            activity_type = 'mini_exam' if ' - Part ' in attempt.test.name else 'exam'
            attempt.user.profile.record_activity(activity_type)
            
        AutoSaveDraft.objects.filter(attempt=attempt).delete()

    from exams.tasks import evaluate_speaking
    evaluate_speaking.delay(attempt.id)

    return redirect('exams:test_result', attempt_id=attempt.id)


def _evaluate_speaking_background(attempt_id):
    """Background thread: marks all speaking submissions as evaluated via Gemini."""
    from exams.speaking_services import evaluate_speaking_submission
    
    connection.close()
    try:
        attempt = UserAttempt.objects.get(pk=attempt_id)
        for sub in attempt.speaking_submissions.filter(is_evaluated=False).select_related('question'):
            if evaluate_speaking_submission(sub):
                sub.save()
            else:
                sub.is_evaluated = True
                sub.transcript = "[Auto-transcript failed]"
                sub.feedback_text = "Sorry, failed to evaluate the recording."
                sub.estimated_level = "B1"
                sub.save()
    except Exception as e:
        print(f"[Speaking eval error] {e}")
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# AI Chat Assistant
# ---------------------------------------------------------------------------

AI_SYSTEM_PROMPT = """You are CEFRPrep AI — a friendly, knowledgeable English language tutor built into the CEFRPrep learning platform.

CORE RULES:
1. ONLY answer questions about English language learning: grammar, vocabulary, pronunciation, idioms, exam strategies, CEFR levels, study tips, writing, listening, reading, speaking, and related topics.
2. If the user asks about anything UNRELATED to English learning, politely decline: "I'm your English tutor 🎓 I can help with grammar, vocabulary, CEFR preparation, and more — ask me anything about English!"
3. ALWAYS respond in the SAME language the user writes in. If they write in Russian → respond in Russian. English → English. Uzbek → Uzbek. Keep English linguistic terms (CEFR, B2, grammar, etc.) untranslated.
4. Keep responses concise (2-4 short paragraphs max), practical, and encouraging.
5. Use markdown: **bold**, bullet points, numbered lists. Use tables for study plans. Use ```mermaid blocks only when the user explicitly asks for diagrams, timelines, or charts.
6. Be warm and supportive like a personal tutor. When giving grammar explanations, include 2-3 clear examples.

PROGRESS DATA — CRITICAL RULES:
- You may receive the user's progress data as background context below.
- This data is for YOUR INTERNAL REFERENCE ONLY.
- NEVER mention test scores, percentages, skill levels, module progress, or specific test results UNLESS the user EXPLICITLY asks about their progress, scores, results, performance, or what they should focus on.
- If the user asks "how am I doing?", "what should I focus on?", "what are my weak areas?" — THEN and ONLY THEN use the progress data to give a personalized answer.
- If the user asks a grammar question, vocabulary question, or any topic-specific question — just answer the question directly WITHOUT referencing their scores or progress.

SHOW_MODULES RULES:
- ONLY populate show_modules when the user explicitly asks how to IMPROVE or PRACTICE a specific skill.
- "How do I improve my listening?" → show_modules: ["listening"]
- "Tips for better writing" → show_modules: ["writing"]
- "How to prepare for CEFR?" → show_modules: ["listening", "reading", "writing", "speaking"]
- For grammar questions, vocabulary, explanations, corrections → show_modules: []
- When in doubt → show_modules: []

RESPONSE FORMAT — return valid JSON only:
{
  "text": "Your response in markdown",
  "show_modules": []
}"""


def _build_user_progress_context(user):
    """
    Build a compact context string with the user's learning progress.
    Marked as internal-only to prevent the model from volunteering it.
    """
    lines = []
    lines.append("\n[INTERNAL CONTEXT — do NOT mention unless user asks about progress]")
    lines.append(f"Student name: {user.first_name or user.username}")

    # Skill scores (compact)
    try:
        radar = get_skill_radar_data(user)
        if radar and any(v > 0 for v in radar['data']):
            scores = ", ".join(f"{l}: {s}%" for l, s in zip(radar['labels'], radar['data']))
            lines.append(f"Skills: {scores}")
        else:
            lines.append("Skills: No test data yet.")
    except Exception:
        pass

    # Recent attempts (last 5, compact)
    try:
        recent = UserAttempt.objects.filter(
            user=user,
            completed_at__isnull=False,
        ).select_related('test').order_by('-completed_at')[:5]

        if recent:
            for att in recent:
                date_str = att.completed_at.strftime('%b %d')
                lines.append(
                    f"  {att.test.name} ({att.test.test_type}) — "
                    f"{att.score_percentage}% — {date_str}"
                )
            total = UserAttempt.objects.filter(
                user=user, completed_at__isnull=False
            ).count()
            lines.append(f"Total tests: {total}")
    except Exception:
        pass

    lines.append("[END INTERNAL CONTEXT]")
    return "\n".join(lines)




@login_required
def ai_chat_view(request, session_id=None):
    """Render the AI chat page with session sidebar."""
    sessions = request.user.chat_sessions.all()[:50]
    current_session = None
    if session_id:
        current_session = get_object_or_404(ChatSession, pk=session_id, user=request.user)
    return render(request, 'exams/ai_chat.html', {
        'sessions': sessions,
        'current_session': current_session,
        'hide_navbar': True,
        'hide_footer': True,
    })


@login_required
@require_POST
def api_generate_tts(request):
    """Generate TTS using Google Cloud and return base64 audio."""
    from .tts_service import generate_tts_base64
    try:
        body = json.loads(request.body)
        text = body.get('text', '').strip()
        lang = body.get('lang', 'en-US')
        if not text:
            return JsonResponse({'error': 'Empty text'}, status=400)
            
        audio_base64 = generate_tts_base64(text, lang)
        if audio_base64:
            return JsonResponse({'status': 'ok', 'audio_base64': audio_base64})
        else:
            return JsonResponse({'error': 'Failed to generate audio'}, status=500)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_POST
def api_ai_chat_send(request):
    """
    Receive a user message, call Gemini, save both messages, return response.
    Expects JSON: { "message": "...", "session_id": null | int }
    """
    import os

    try:
        body = json.loads(request.body)
        user_message = body.get('message', '').strip()
        session_id = body.get('session_id')
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    if not user_message:
        return JsonResponse({'error': 'Empty message'}, status=400)

    if len(user_message) > 5000:
        return JsonResponse({'error': 'Message too long (max 5000 chars)'}, status=400)

    # Get or create session
    if session_id:
        session = get_object_or_404(ChatSession, pk=session_id, user=request.user)
    else:
        # Auto-generate title from first message
        title = user_message[:80] + ('...' if len(user_message) > 80 else '')
        session = ChatSession.objects.create(user=request.user, title=title)

    # Save user message
    ChatMessage.objects.create(
        session=session,
        role='user',
        content=user_message,
    )

    # Build conversation history for context (last 20 messages)
    history = list(
        session.messages.order_by('-created_at')[:20]
    )
    history.reverse()

    conversation = []
    for msg in history:
        conversation.append({
            'role': msg.role if msg.role == 'user' else 'model',
            'parts': [{'text': msg.content}],
        })

    # Call Gemini
    try:
        from google import genai
        from google.genai import types as T

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project_id:
            adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
            if os.path.exists(adc_path):
                with open(adc_path) as f:
                    project_id = json.load(f).get("quota_project_id")

        client = genai.Client(vertexai=True, project=project_id, location="global")

        generate_cfg = T.GenerateContentConfig(
            response_mime_type="application/json",
            system_instruction=AI_SYSTEM_PROMPT + _build_user_progress_context(request.user),
        )

        resp = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=conversation,
            config=generate_cfg,
        )

        raw_text = resp.text.strip()
        # Clean markdown code fences if present
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]

        result = json.loads(raw_text.strip())
        ai_text = result.get('text', 'Sorry, I could not generate a response.')
        show_modules = result.get('show_modules', [])

        # Validate module IDs
        valid_modules = {'listening', 'reading', 'writing', 'speaking'}
        show_modules = [m for m in show_modules if m in valid_modules]

    except Exception as e:
        print(f"[AI Chat Gemini error] {e}")
        ai_text = "Sorry, I'm experiencing technical difficulties right now. Please try again in a moment! 🔧"
        show_modules = []

    # Save AI response
    ai_msg = ChatMessage.objects.create(
        session=session,
        role='assistant',
        content=ai_text,
        module_cards=show_modules,
    )

    # Update session timestamp
    session.save(update_fields=['updated_at'])

    return JsonResponse({
        'status': 'ok',
        'session_id': session.id,
        'session_title': session.title,
        'message': {
            'id': ai_msg.id,
            'role': 'assistant',
            'content': ai_text,
            'module_cards': show_modules,
            'created_at': ai_msg.created_at.isoformat(),
        }
    })


@login_required
def api_ai_chat_sessions(request):
    """Return all chat sessions for the current user."""
    sessions = request.user.chat_sessions.all()[:50]
    data = [{
        'id': s.id,
        'title': s.title,
        'updated_at': s.updated_at.isoformat(),
        'message_count': s.messages.count(),
    } for s in sessions]
    return JsonResponse({'sessions': data})


@login_required
def api_ai_chat_history(request, session_id):
    """Return all messages for a specific session."""
    session = get_object_or_404(ChatSession, pk=session_id, user=request.user)
    messages_data = [{
        'id': m.id,
        'role': m.role,
        'content': m.content,
        'module_cards': m.module_cards or [],
        'created_at': m.created_at.isoformat(),
    } for m in session.messages.all()]
    return JsonResponse({
        'session_id': session.id,
        'title': session.title,
        'messages': messages_data,
    })


@login_required
@require_POST
def api_ai_chat_delete_session(request, session_id):
    """Delete a chat session and all its messages."""
    session = get_object_or_404(ChatSession, pk=session_id, user=request.user)
    session.delete()
    return JsonResponse({'status': 'ok'})


# ---------------------------------------------------------------------------
# Personal Dictionary
# ---------------------------------------------------------------------------

@login_required
def dictionary_view(request):
    """Render the personal dictionary page."""
    entries = (
        request.user.dictionary_entries
        .select_related('cached_word')
        .order_by('-created_at')
    )
    return render(request, 'exams/dictionary.html', {
        'entries': entries,
        'entries_count': entries.count(),
    })


@login_required
@require_POST
def api_dictionary_lookup(request):
    """
    Look up an English word, enrich it via Gemini (or cache), and add to
    the user's personal dictionary.

    Expects JSON: { "word": "ubiquitous", "source": "manual" | "dblclick" }
    Returns the full card data.
    """
    import os

    try:
        body = json.loads(request.body)
        raw_word = body.get('word', '').strip()
        source = body.get('source', 'manual')
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    if not raw_word or len(raw_word) > 100:
        return JsonResponse({'error': 'Invalid word'}, status=400)

    word = raw_word.lower().strip()

    # 1. Check global cache first
    cached = WordCache.objects.filter(word=word).first()

    if not cached:
        # 2. No cache — call Gemini for enrichment
        try:
            from google import genai
            from google.genai import types as T

            project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
            if not project_id:
                adc_path = os.path.expanduser(
                    "~/.config/gcloud/application_default_credentials.json"
                )
                if os.path.exists(adc_path):
                    with open(adc_path) as f:
                        project_id = json.load(f).get("quota_project_id")

            client = genai.Client(
                vertexai=True, project=project_id, location="global"
            )

            prompt = f"""You are a professional English dictionary and translator.
For the English word or phrase: "{word}"

Provide the following data in JSON format:
{{
  "transcription": "IPA phonetic transcription (e.g. /juːˈbɪkwɪtəs/)",
  "part_of_speech": "noun / verb / adjective / adverb / etc.",
  "register": "formal / informal / neutral",
  "alternative_form": "If the word is informal, provide its formal equivalent (e.g. for 'gonna' → 'going to', 'kids' → 'children', 'get' → 'obtain'). If formal, provide its informal/everyday equivalent (e.g. for 'commence' → 'start', 'utilize' → 'use', 'subsequently' → 'then'). If neutral, return an empty string.",
  "definitions": {{
    "en": "Clear, concise English definition (1-2 sentences max)",
    "ru": "Перевод определения на русский язык",
    "uz": "O'zbek tilida ta'rifi"
  }},
  "example": "One natural example sentence using this word in context",
  "translations": {{
    "en": "English synonym or brief gloss",
    "ru": "Russian translation (перевод на русский)",
    "uz": "Uzbek translation (o'zbek tiliga tarjima)"
  }}
}}

Register classification rules (be strict and precise):
- formal: words used in academic writing, business, or official contexts, OR any word that elevates the text above basic everyday speech. If a simpler, more common alternative exists (e.g. "conversation" vs "chat", "perhaps" vs "maybe", "environment" vs "setting", "obtain" vs "get"), classify the word as FORMAL.
  Examples of FORMAL words: conversation, perhaps, environment, commence, utilize, endeavour, pursuant, henceforth, aforementioned, procure, remuneration, subsequently, nevertheless, considerable, obtain, reside, assist, request, however, provide, require, ensure, therefore, regarding, additional, sufficient, approximately.
- informal: words used in casual conversation, slang, or colloquial speech that would sound out of place in formal writing.
  Examples of INFORMAL words: gonna, wanna, gotta, kinda, sorta, ain't, yeah, nope, awesome, cool (as adjective), kids, get (in sense of 'understand'), stuff, things (vague), basically, literally (exaggerated), totally, chill, hang out, a lot, loads, tons (of something).
- neutral: words that are equally appropriate in both formal and informal contexts without any stylistic marking.
  Examples of NEUTRAL words: walk, house, water, dog, table, read, write, blue, happy, know, name, city, book, eat, sleep, work (verb), day, time, person.

IMPORTANT: If the word has a clear formal or informal bias, do NOT label it neutral. Only use neutral for truly unmarked everyday vocabulary.

Additional rules:
- transcription must be in IPA notation with slashes
- alternative_form: only provide if register is formal or informal; must be a concise word or short phrase
- definition must be in English
- example must be a natural, real-world sentence
- translations must be accurate for each language
- If the word doesn't exist or is not English, still provide your best guess"""

            generate_cfg = T.GenerateContentConfig(
                response_mime_type="application/json"
            )
            resp = client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=[prompt],
                config=generate_cfg,
            )

            raw_text = resp.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.startswith("```"):
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]

            result = json.loads(raw_text.strip())

            cached = WordCache.objects.create(
                word=word,
                transcription=result.get('transcription', ''),
                translations=result.get('translations', {}),
                definitions=result.get('definitions', {}),
                example=result.get('example', ''),
                part_of_speech=result.get('part_of_speech', ''),
                register=result.get('register', ''),
                alternative_form=result.get('alternative_form', ''),
            )

        except Exception as e:
            print(f"[Dictionary Gemini error] {e}")
            # Fallback — create a minimal cache entry
            cached = WordCache.objects.create(
                word=word,
                translations={'en': word, 'ru': word, 'uz': word},
                definitions={'en': '(lookup failed)', 'ru': '(ошибка)', 'uz': '(xato)'},
            )

    # 3. Add to user's personal dictionary (or return existing)
    entry, created = DictionaryEntry.objects.get_or_create(
        user=request.user,
        cached_word=cached,
        defaults={'source': source},
    )

    return JsonResponse({
        'status': 'ok',
        'created': created,
        'entry': {
            'id': entry.id,
            'word': cached.word,
            'transcription': cached.transcription,
            'part_of_speech': cached.part_of_speech,
            'register': cached.register,
            'alternative_form': cached.alternative_form,
            'translations': cached.translations,
            'definitions': cached.definitions,
            'example': cached.example,
            'source': entry.source,
            'created_at': entry.created_at.isoformat(),
        },
    })


@login_required
@require_POST
def api_dictionary_delete(request, entry_id):
    """Remove a word from the user's personal dictionary."""
    entry = get_object_or_404(
        DictionaryEntry, pk=entry_id, user=request.user
    )
    entry.delete()
    return JsonResponse({'status': 'ok'})


@login_required
@require_POST
def api_dictionary_refresh(request, entry_id):
    """
    Force re-enrich a word by updating its WordCache in place via Gemini.
    Used to backfill new fields (register, alternative_form) on cached words.
    """
    import os

    entry = get_object_or_404(DictionaryEntry, pk=entry_id, user=request.user)
    cached = entry.cached_word
    word = cached.word

    # Re-call Gemini and update the existing cache row in place (no CASCADE delete)
    try:
        from google import genai
        from google.genai import types as T

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        if not project_id:
            adc_path = os.path.expanduser(
                "~/.config/gcloud/application_default_credentials.json"
            )
            if os.path.exists(adc_path):
                with open(adc_path) as f:
                    project_id = json.load(f).get("quota_project_id")

        client = genai.Client(vertexai=True, project=project_id, location="global")

        prompt = f"""You are a professional English dictionary and translator.
For the English word or phrase: "{word}"

Provide the following data in JSON format:
{{
  "transcription": "IPA phonetic transcription (e.g. /juːˈbɪkwɪtəs/)",
  "part_of_speech": "noun / verb / adjective / adverb / etc.",
  "register": "formal / informal / neutral",
  "alternative_form": "If the word is informal, provide its formal equivalent (e.g. for 'gonna' → 'going to', 'kids' → 'children', 'get' → 'obtain'). If formal, provide its informal/everyday equivalent (e.g. for 'commence' → 'start', 'utilize' → 'use', 'subsequently' → 'then'). If neutral, return an empty string.",
  "definitions": {{
    "en": "Clear, concise English definition (1-2 sentences max)",
    "ru": "Перевод определения на русский язык",
    "uz": "O'zbek tilida ta'rifi"
  }},
  "example": "One natural example sentence using this word in context",
  "translations": {{
    "en": "English synonym or brief gloss",
    "ru": "Russian translation (перевод на русский)",
    "uz": "Uzbek translation (o'zbek tiliga tarjima)"
  }}
}}

Register classification rules (be strict and precise):
- formal: words used in academic writing, business, or official contexts, OR any word that elevates the text above basic everyday speech. If a simpler, more common alternative exists (e.g. "conversation" vs "chat", "perhaps" vs "maybe", "environment" vs "setting", "obtain" vs "get"), classify the word as FORMAL.
  Examples of FORMAL words: conversation, perhaps, environment, commence, utilize, endeavour, pursuant, henceforth, aforementioned, procure, remuneration, subsequently, nevertheless, considerable, obtain, reside, assist, request, however, provide, require, ensure, therefore, regarding, additional, sufficient, approximately.
- informal: words used in casual conversation, slang, or colloquial speech that would sound out of place in formal writing.
  Examples of INFORMAL words: gonna, wanna, gotta, kinda, sorta, ain't, yeah, nope, awesome, cool (as adjective), kids, get (in sense of 'understand'), stuff, things (vague), basically, literally (exaggerated), totally, chill, hang out, a lot, loads, tons (of something).
- neutral: words that are equally appropriate in both formal and informal contexts without any stylistic marking.
  Examples of NEUTRAL words: walk, house, water, dog, table, read, write, blue, happy, know, name, city, book, eat, sleep, work (verb), day, time, person.

IMPORTANT: If the word has a clear formal or informal bias, do NOT label it neutral. Only use neutral for truly unmarked everyday vocabulary.

Additional rules:
- transcription must be in IPA notation with slashes
- alternative_form: only provide if register is formal or informal; must be a concise word or short phrase
- definition must be in English
- example must be a natural, real-world sentence
- translations must be accurate for each language"""

        generate_cfg = T.GenerateContentConfig(response_mime_type="application/json")
        resp = client.models.generate_content(
            model="gemini-3-flash-preview", contents=[prompt], config=generate_cfg,
        )

        raw_text = resp.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]

        result = json.loads(raw_text.strip())

        # Update the existing WordCache row — no CASCADE, no broken FKs
        cached.transcription = result.get('transcription', cached.transcription)
        cached.translations = result.get('translations', cached.translations)
        cached.definitions = result.get('definitions', cached.definitions)
        cached.example = result.get('example', cached.example)
        cached.part_of_speech = result.get('part_of_speech', cached.part_of_speech)
        cached.register = result.get('register', '')
        cached.alternative_form = result.get('alternative_form', '')
        cached.save(update_fields=[
            'transcription', 'translations', 'definitions', 'example',
            'part_of_speech', 'register', 'alternative_form',
        ])

    except Exception as e:
        print(f"[Dictionary refresh error] {e}")

    return JsonResponse({
        'status': 'ok',
        'entry': {
            'id': entry.id,
            'word': cached.word,
            'transcription': cached.transcription,
            'part_of_speech': cached.part_of_speech,
            'register': cached.register,
            'alternative_form': cached.alternative_form,
            'translations': cached.translations,
            'definitions': cached.definitions,
            'example': cached.example,
            'source': entry.source,
            'created_at': entry.created_at.isoformat(),
        },
    })


# ---------------------------------------------------------------------------
# YouTube Video Lessons
# ---------------------------------------------------------------------------

import uuid
from django.core.cache import cache

@login_required
def video_lessons_view(request):
    """
    GET:  List all video lessons + show form to add a new one.
    POST: Accept a YouTube URL, create a video lesson with AI quizzes.
    """
    from .models import VideoLesson

    if request.method == 'POST':
        youtube_url = request.POST.get('youtube_url', '').strip()
        title = request.POST.get('title', '').strip()

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            if not youtube_url:
                return JsonResponse({'error': 'Please provide a YouTube URL.'}, status=400)
            
            task_id = str(uuid.uuid4())
            cache.set(f"video_task_{task_id}", {"pct": 0, "label": "Starting..."}, timeout=3600)
            
            from exams.tasks import create_video_lesson_task
            create_video_lesson_task.delay(task_id, youtube_url, request.user.id, title, False)
            
            return JsonResponse({'task_id': task_id})

        # Fallback for non-AJAX
        if not youtube_url:
            messages.error(request, "Please provide a YouTube URL.")
            return redirect('exams:video_lessons')

        try:
            from .video_services import create_video_lesson
            lesson = create_video_lesson(
                youtube_url=youtube_url,
                user=request.user,
                title=title,
                is_public=False,  # student-uploaded: private by default
            )
            messages.success(
                request,
                f"Video lesson created with {lesson.quiz_count} quiz questions!"
            )
            return redirect('exams:video_lesson_detail', lesson_id=lesson.id)
        except ValueError as exc:
            messages.error(request, f"Invalid URL: {exc}")
        except RuntimeError as exc:
            messages.error(request, f"Error: {exc}")
        except Exception as exc:
            messages.error(request, f"Unexpected error: {exc}")

        return redirect('exams:video_lessons')

    # GET — list public lessons + lessons the current user created
    from django.db.models import Q
    admin_lessons = VideoLesson.objects.filter(is_public=True).order_by('-created_at')
    student_lessons = VideoLesson.objects.filter(created_by=request.user, is_public=False).order_by('-created_at')
    context = {
        'admin_lessons': admin_lessons,
        'student_lessons': student_lessons,
        'cefr_levels': VideoLesson.CEFR_LEVELS,
    }
    return render(request, 'exams/video_lessons_list.html', context)


@login_required
def video_lesson_detail_view(request, lesson_id):
    """
    Render the video player page with quiz data for the JS timecode tracker.
    """
    from .models import VideoLesson

    lesson = get_object_or_404(VideoLesson, pk=lesson_id)

    # Access control: public lessons are open to everyone;
    # private lessons are accessible by their creator OR users in a classroom where this lesson is assigned.
    from django.db import models
    has_classroom_access = lesson.classrooms.filter(
        models.Q(memberships__student=request.user) | models.Q(mentor=request.user)
    ).exists()

    if not lesson.is_public and lesson.created_by != request.user and not has_classroom_access:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied

    questions = lesson.quiz_questions.all().order_by('trigger_time_seconds')

    quiz_data = [
        {
            'id': q.id,
            'trigger_time_seconds': q.trigger_time_seconds,
            'question_text': q.question_text,
            'options': q.options,
            'correct_option_index': q.correct_option_index,
            'explanation': q.explanation,
        }
        for q in questions
    ]

    context = {
        'lesson': lesson,
        'quiz_json': json.dumps(quiz_data, ensure_ascii=False),
        'quiz_count': len(quiz_data),
    }
    return render(request, 'exams/video_lesson.html', context)


@login_required
def api_video_quiz_data(request, lesson_id):
    """Return quiz questions for a video lesson as JSON."""
    from .models import VideoLesson

    lesson = get_object_or_404(VideoLesson, pk=lesson_id)

    from django.db import models
    has_classroom_access = lesson.classrooms.filter(
        models.Q(memberships__student=request.user) | models.Q(mentor=request.user)
    ).exists()

    if not lesson.is_public and lesson.created_by != request.user and not has_classroom_access:
        return JsonResponse({'status': 'error', 'message': 'Access denied.'}, status=403)

    questions = lesson.quiz_questions.all().order_by('trigger_time_seconds')

    data = [
        {
            'id': q.id,
            'trigger_time_seconds': q.trigger_time_seconds,
            'question_text': q.question_text,
            'options': q.options,
            'correct_option_index': q.correct_option_index,
            'explanation': q.explanation,
        }
        for q in questions
    ]

    return JsonResponse({'status': 'ok', 'questions': data})


@login_required
@require_POST
def api_video_lesson_delete(request, lesson_id):
    """Allow students to delete their own uploaded video lessons."""
    from .models import VideoLesson
    lesson = get_object_or_404(VideoLesson, pk=lesson_id)

    # Allow deleting only if user created it and it's not a public/admin lesson
    if lesson.created_by != request.user or lesson.is_public:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied

    lesson.delete()
    return JsonResponse({'status': 'ok'})

@login_required
def api_video_lesson_progress(request, task_id):
    """Poll the progress of a background video lesson generation."""
    data = cache.get(f"video_task_{task_id}")
    if not data:
        return JsonResponse({'error': 'Task not found or expired'}, status=404)
    return JsonResponse(data)


# ---------------------------------------------------------------------------
# Multiplayer Video Room (Kahoot-style)
# ---------------------------------------------------------------------------

import random
import string
from django.utils.crypto import get_random_string

@login_required
def video_room_host_view(request, lesson_id):
    """
    Host view for a collective video session.
    Generates a room PIN if not active, or resumes an existing one.
    """
    from .models import VideoLesson, VideoRoom, VideoRoomAnswer
    lesson = get_object_or_404(VideoLesson, pk=lesson_id)
    
    # Try to find an existing active room for this user and lesson
    room = VideoRoom.objects.filter(host=request.user, lesson=lesson).exclude(status='finished').first()
    
    if not room:
        # Generate a unique 6-digit PIN
        while True:
            pin = get_random_string(length=6, allowed_chars=string.digits)
            if not VideoRoom.objects.filter(room_code=pin).exists():
                break
        
        room = VideoRoom.objects.create(
            room_code=pin,
            host=request.user,
            lesson=lesson,
            status='waiting'
        )
    
    # The host view uses a modified version of the video_lesson player
    quiz_questions = list(lesson.quiz_questions.order_by('trigger_time_seconds').values(
        'id', 'trigger_time_seconds', 'question_text', 'options', 'correct_option_index', 'explanation'
    ))

    # Restore progress after refresh: questions that were already asked/answered
    asked_question_ids = set(
        VideoRoomAnswer.objects.filter(participant__room=room)
        .values_list('question_id', flat=True)
    )
    if room.current_question_id:
        asked_question_ids.add(room.current_question_id)
    
    context = {
        'room': room,
        'lesson': lesson,
        'quiz_count': len(quiz_questions),
        'quiz_json': json.dumps(quiz_questions),
        'initial_room_status': room.status,
        'initial_current_question_id': room.current_question_id,
        'asked_question_ids_json': json.dumps(sorted(asked_question_ids)),
        'hide_navbar': True,
        'hide_footer': True,
    }
    return render(request, 'exams/video_room_host.html', context)


@login_required
@require_POST
def video_room_end_view(request, room_code):
    """End a hosted room session and redirect to lessons list."""
    from .models import VideoRoom

    room = get_object_or_404(VideoRoom, room_code=room_code, host=request.user)
    room.status = 'finished'
    room.current_question = None
    room.save(update_fields=['status', 'current_question'])

    return redirect('exams:video_lessons')


def video_room_ended_view(request, room_code):
    """
    Display a notification page when the session has been ended by the host.
    This view is shown to students when they're notified that the session is finished.
    """
    from .models import VideoRoom

    room = get_object_or_404(VideoRoom, room_code=room_code)
    
    # Clean up session data for this room
    if f'room_participant_{room_code}' in request.session:
        del request.session[f'room_participant_{room_code}']
        request.session.modified = True

    context = {
        'room': room,
    }
    return render(request, 'exams/video_room_ended.html', context)


@login_required
def video_room_join_view(request):
    """
    Student view to enter a PIN and join a room.
    """
    from .models import VideoRoom, VideoRoomParticipant
    
    if request.method == 'POST':
        pin = request.POST.get('pin', '').strip()
        nickname = request.user.first_name or request.user.username
        
        if not pin:
            messages.error(request, "Please enter a PIN.")
            return redirect('exams:video_room_join')
            
        room = VideoRoom.objects.filter(room_code=pin).exclude(status='finished').first()
        if not room:
            messages.error(request, "Room not found or has finished.")
            return redirect('exams:video_room_join')
            
        # Create or get participant
        participant, created = VideoRoomParticipant.objects.get_or_create(
            room=room,
            nickname=nickname,
            defaults={'score': 0}
        )
        
        # Store participant ID in session so play view knows who they are
        request.session[f'room_participant_{pin}'] = participant.id
        
        return redirect('exams:video_room_play', room_code=pin)
        
    return render(request, 'exams/video_room_join.html')


def video_room_play_view(request, room_code):
    """
    Student participant screen for answering questions.
    """
    from .models import VideoRoom, VideoRoomParticipant
    
    room = get_object_or_404(VideoRoom, room_code=room_code)
    participant_id = request.session.get(f'room_participant_{room_code}')
    
    if not participant_id:
        return redirect('exams:video_room_join')
        
    participant = get_object_or_404(VideoRoomParticipant, id=participant_id, room=room)
    
    # Update last_active
    participant.last_active = timezone.now()
    participant.save(update_fields=['last_active'])
    
    context = {
        'room': room,
        'participant': participant,
        'hide_navbar': True,
        'hide_footer': True,
    }
    return render(request, 'exams/video_room_play.html', context)


# ------------------ ROOM APIs ------------------

@require_POST
def api_room_set_state(request, room_code):
    """Host pushes state changes to the room."""
    from .models import VideoRoom, QuizQuestion
    room = get_object_or_404(VideoRoom, room_code=room_code)
    
    if request.user != room.host:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
        
    try:
        body = json.loads(request.body)
        status = body.get('status')
        question_id = body.get('question_id')
        
        if status in [c[0] for c in VideoRoom.STATUS_CHOICES]:
            room.status = status
            
        if question_id:
            room.current_question = QuizQuestion.objects.get(id=question_id)
        elif status in ['playing', 'waiting', 'finished']:
            room.current_question = None
            
        room.save()
        return JsonResponse({'status': 'ok'})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


def api_room_host_state(request, room_code):
    """Host polls for participants and their answers."""
    from .models import VideoRoom, VideoRoomAnswer
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from accounts.models import UserProfile

    room = get_object_or_404(VideoRoom, room_code=room_code)
    
    if request.user != room.host:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
        
    participants = room.participants.all().order_by('-score')

    User = get_user_model()
    nickname_values = [p.nickname for p in participants if p.nickname]
    users_qs = User.objects.filter(
        Q(username__in=nickname_values) | Q(first_name__in=nickname_values)
    ).select_related('profile')

    users_by_username = {u.username.lower(): u for u in users_qs if u.username}
    users_by_first_name = {u.first_name.lower(): u for u in users_qs if u.first_name}
    
    participants_data = []
    for p in participants:
        avatar_custom_url = None
        avatar_icon = 'ph-user'
        avatar_gradient = 'linear-gradient(135deg, #2B4D56, #1A2F35)'

        lookup_key = (p.nickname or '').lower()
        user = users_by_username.get(lookup_key) or users_by_first_name.get(lookup_key)

        profile = None
        if user:
            try:
                profile = user.profile
            except UserProfile.DoesNotExist:
                profile = None

        if profile:
            avatar_icon = profile.avatar_icon
            avatar_gradient = profile.avatar_gradient
            if profile.avatar == 'custom' and profile.custom_avatar:
                avatar_custom_url = profile.custom_avatar.url

        participants_data.append({
            'id': p.id,
            'nickname': p.nickname,
            'score': p.score,
            'avatar_custom_url': avatar_custom_url,
            'avatar_icon': avatar_icon,
            'avatar_gradient': avatar_gradient,
        })
        
    # If a question is active, check how many answered
    answers_count = 0
    if room.current_question:
        answers_count = VideoRoomAnswer.objects.filter(
            participant__room=room, 
            question=room.current_question
        ).count()
        
    return JsonResponse({
        'status': room.status,
        'participants': participants_data,
        'answers_count': answers_count,
        'total_participants': len(participants_data)
    })


def api_room_participant_state(request, room_code):
    """Participant polls for room state."""
    from .models import VideoRoom, VideoRoomParticipant, VideoRoomAnswer
    room = get_object_or_404(VideoRoom, room_code=room_code)
    
    participant_id = request.session.get(f'room_participant_{room_code}')
    if not participant_id:
        return JsonResponse({'error': 'Not joined'}, status=403)
        
    # Update active timestamp
    VideoRoomParticipant.objects.filter(id=participant_id).update(last_active=timezone.now())
    
    response_data = {
        'status': room.status,
        'session_ended': room.status == 'finished',
    }
    
    if room.status == 'question' and room.current_question:
        # Send question options but NOT the correct answer
        q = room.current_question
        
        # Check if already answered
        answered = VideoRoomAnswer.objects.filter(
            participant_id=participant_id, 
            question=q
        ).exists()
        
        response_data['question'] = {
            'id': q.id,
            'text': q.question_text,
            'options': q.options,
            'answered': answered
        }
        
    elif room.status == 'results' and room.current_question:
        q = room.current_question
        answer = VideoRoomAnswer.objects.filter(
            participant_id=participant_id, 
            question=q
        ).first()
        
        response_data['results'] = {
            'correct_option_index': q.correct_option_index,
            'my_answer': answer.selected_index if answer else None,
            'is_correct': answer.is_correct if answer else False,
            'points': answer.points_awarded if answer else 0,
            'explanation': q.explanation
        }
        
    return JsonResponse(response_data)


@require_POST
def api_room_submit_answer(request, room_code):
    """Participant submits an answer."""
    from .models import VideoRoom, VideoRoomParticipant, VideoRoomAnswer, QuizQuestion
    room = get_object_or_404(VideoRoom, room_code=room_code)
    
    participant_id = request.session.get(f'room_participant_{room_code}')
    if not participant_id:
        return JsonResponse({'error': 'Not joined'}, status=403)
        
    if room.status != 'question' or not room.current_question:
        return JsonResponse({'error': 'Not taking answers right now'}, status=400)
        
    try:
        body = json.loads(request.body)
        selected_index = int(body.get('selected_index', -1))
        
        participant = VideoRoomParticipant.objects.get(id=participant_id)
        question = room.current_question
        
        is_correct = (selected_index == question.correct_option_index)
        points = 1000 if is_correct else 0 # Fixed points for now
        
        # Save answer (only the first one counts due to unique_together and get_or_create)
        answer, created = VideoRoomAnswer.objects.get_or_create(
            participant=participant,
            question=question,
            defaults={
                'selected_index': selected_index,
                'is_correct': is_correct,
                'points_awarded': points
            }
        )
        
        if created and is_correct:
            # Atomic update of score
            from django.db.models import F
            VideoRoomParticipant.objects.filter(id=participant_id).update(score=F('score') + points)
            
        return JsonResponse({'status': 'ok'})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@login_required
def arcade_view(request):
    """
    Main arcade dashboard containing games, quizzes, and stories.
    """
    return render(request, 'exams/arcade.html')


# ---------------------------------------------------------------------------
# Classroom (Student views)
# ---------------------------------------------------------------------------

@login_required
def join_classroom_view(request):
    """
    GET:  Show the join-classroom form (enter code).
    POST: Join a classroom by its code. Students can be in multiple classrooms.
    """
    from .models import Classroom, ClassroomMembership

    # All classrooms the student is currently in
    existing_memberships = (
        ClassroomMembership.objects
        .filter(student=request.user)
        .select_related('classroom__mentor')
    )

    if request.method == 'POST':
        code = request.POST.get('code', '').strip().upper()
        if not code:
            messages.error(request, 'Введите код класса.')
            return render(request, 'exams/classroom_join.html', {'existing_memberships': existing_memberships})

        try:
            classroom = Classroom.objects.get(join_code=code, is_active=True)
        except Classroom.DoesNotExist:
            messages.error(request, 'Класс с таким кодом не найден.')
            return render(request, 'exams/classroom_join.html', {'existing_memberships': existing_memberships})

        # Don't let mentors join as students
        if classroom.mentor == request.user:
            messages.error(request, 'Вы не можете вступить в свой собственный класс.')
            return render(request, 'exams/classroom_join.html', {'existing_memberships': existing_memberships})

        # Check if already in this classroom
        _, created = ClassroomMembership.objects.get_or_create(
            classroom=classroom, student=request.user,
        )
        if created:
            messages.success(request, f'Вы вступили в класс «{classroom.name}»!')
        else:
            messages.info(request, f'Вы уже состоите в классе «{classroom.name}».')
        return redirect('exams:my_classroom')

    return render(request, 'exams/classroom_join.html', {'existing_memberships': existing_memberships})


@login_required
def my_classroom_view(request):
    """
    Show the student's classrooms.
    If the student has multiple classrooms, show a selector.
    The selected classroom's tests, videos, announcements, and leaderboard are displayed.
    """
    from .models import ClassroomMembership, ClassroomAnnouncement, UserAttempt, ClassroomAssignment

    all_memberships = (
        ClassroomMembership.objects
        .filter(student=request.user)
        .select_related('classroom__mentor')
        .order_by('-joined_at')
    )

    if not all_memberships.exists():
        return render(request, 'exams/my_classroom.html', {'membership': None})

    # Determine which classroom to show (default: first, or ?classroom=ID)
    selected_id = request.GET.get('classroom')
    if selected_id:
        membership = all_memberships.filter(classroom_id=selected_id).first()
    else:
        membership = None

    if not membership:
        membership = all_memberships.first()

    classroom = membership.classroom

    # Prefetch classroom data
    tests = list(
        classroom.tests.filter(is_deleted=False)
        .prefetch_related(
            'parts__questions', 'writing_tasks',
            'reading_parts__questions', 'speaking_parts__questions',
        )
        .order_by('test_type', '-created_at')
    )
    video_lessons = classroom.video_lessons.all().order_by('-created_at')

    # Assignment data (deadlines)
    assignments_by_test = {}
    assignments_by_video = {}
    for a in classroom.assignments.all():
        if a.test_id:
            assignments_by_test[a.test_id] = a
        if a.video_lesson_id:
            assignments_by_video[a.video_lesson_id] = a

    # Build per-test attempt status for this student
    attempts = (
        UserAttempt.objects
        .filter(user=request.user, test__in=tests)
        .values('test_id', 'id', 'completed_at', 'total_score', 'max_possible_score')
        .order_by('test_id', '-completed_at')
    )
    latest_by_test = {}
    for a in attempts:
        if a['test_id'] not in latest_by_test:
            latest_by_test[a['test_id']] = a

    tests_with_status = []
    for test in tests:
        a = latest_by_test.get(test.pk)
        if a is None:
            status, score_pct, attempt_id = 'not_started', None, None
        elif a['completed_at'] is None:
            status, score_pct, attempt_id = 'in_progress', None, a['id']
        else:
            max_s = a['max_possible_score'] or 1
            score_pct = round(a['total_score'] / max_s * 100)
            status = 'completed'
            attempt_id = a['id']
        assignment = assignments_by_test.get(test.pk)
        tests_with_status.append({
            'test': test,
            'status': status,
            'score_pct': score_pct,
            'attempt_id': attempt_id,
            'assignment': assignment,
        })

    # ---- Leaderboard ----
    student_ids = list(classroom.memberships.values_list('student_id', flat=True))
    all_attempts_for_leaderboard = (
        UserAttempt.objects
        .filter(user_id__in=student_ids, test__in=tests, completed_at__isnull=False)
        .values('user_id', 'test_id', 'total_score', 'max_possible_score')
        .order_by('user_id', 'test_id', '-completed_at')
    )
    # Keep only latest per (user, test)
    latest_per_user_test = {}
    for a in all_attempts_for_leaderboard:
        key = (a['user_id'], a['test_id'])
        if key not in latest_per_user_test:
            latest_per_user_test[key] = a

    # Aggregate per user
    from collections import defaultdict
    user_scores = defaultdict(list)
    user_completed = defaultdict(int)
    for (uid, tid), a in latest_per_user_test.items():
        max_s = a['max_possible_score'] or 1
        pct = round(a['total_score'] / max_s * 100)
        user_scores[uid].append(pct)
        user_completed[uid] += 1

    from django.contrib.auth import get_user_model
    User = get_user_model()
    students_map = {
        u.pk: u for u in User.objects.filter(pk__in=student_ids).select_related('profile')
    }

    leaderboard = []
    for uid in student_ids:
        scores = user_scores.get(uid, [])
        avg = round(sum(scores) / len(scores)) if scores else 0
        leaderboard.append({
            'student': students_map.get(uid),
            'avg_score': avg,
            'completed': user_completed.get(uid, 0),
            'total_tests': len(tests),
            'is_current_user': uid == request.user.pk,
        })
    leaderboard.sort(key=lambda x: (-x['avg_score'], -x['completed']))
    for i, entry in enumerate(leaderboard):
        entry['rank'] = i + 1

    announcements = classroom.announcements.select_related('author').order_by('-is_pinned', '-created_at')
    classmate_count = classroom.memberships.count() - 1  # exclude self

    context = {
        'membership': membership,
        'classroom': classroom,
        'all_memberships': all_memberships,
        'tests': tests,
        'tests_with_status': tests_with_status,
        'video_lessons': video_lessons,
        'mentor': classroom.mentor,
        'announcements': announcements,
        'classmate_count': max(classmate_count, 0),
        'leaderboard': leaderboard,
    }
    return render(request, 'exams/my_classroom.html', context)


@login_required
@require_POST
def leave_classroom_view(request):
    """Leave a specific classroom (by classroom_id POST param)."""
    from .models import ClassroomMembership
    classroom_id = request.POST.get('classroom_id')
    if classroom_id:
        ClassroomMembership.objects.filter(
            student=request.user, classroom_id=classroom_id
        ).delete()
    else:
        # Fallback: leave the most recent classroom
        membership = ClassroomMembership.objects.filter(student=request.user).first()
        if membership:
            membership.delete()
    messages.success(request, 'Вы вышли из класса.')
    return redirect('exams:my_classroom')


@login_required
def classroom_invite_view(request, token):
    """
    URL-based classroom invitation.
    GET:  Show classroom info with a one-click Join button.
    POST: Join the classroom.
    """
    from .models import Classroom, ClassroomMembership

    classroom = get_object_or_404(Classroom, invite_token=token, is_active=True)

    if request.method == 'POST':
        if classroom.mentor == request.user:
            messages.error(request, 'Вы не можете вступить в свой собственный класс.')
            return redirect('exams:dashboard')

        _, created = ClassroomMembership.objects.get_or_create(
            classroom=classroom, student=request.user,
        )
        if created:
            messages.success(request, f'Вы вступили в класс «{classroom.name}»!')
        else:
            messages.info(request, f'Вы уже состоите в классе «{classroom.name}».')
        return redirect('exams:my_classroom')

    context = {
        'classroom': classroom,
        'already_member': ClassroomMembership.objects.filter(
            classroom=classroom, student=request.user
        ).exists() if request.user.is_authenticated else False,
    }
    return render(request, 'exams/classroom_invite.html', context)


# ---------------------------------------------------------------------------
# Notification API
# ---------------------------------------------------------------------------

@login_required
def api_notifications_list(request):
    """Return recent notifications for the current user."""
    from .models import Notification
    notifications = (
        Notification.objects
        .filter(user=request.user)
        .order_by('-created_at')[:20]
    )
    data = [{
        'id': n.id,
        'type': n.notification_type,
        'title': n.title,
        'body': n.body,
        'url': n.url,
        'is_read': n.is_read,
        'created_at': n.created_at.isoformat(),
    } for n in notifications]

    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    return JsonResponse({'notifications': data, 'unread_count': unread_count})


@login_required
@require_POST
def api_notifications_mark_read(request):
    """Mark all notifications as read, or a specific one by ID."""
    from .models import Notification
    notif_id = request.POST.get('id')
    if notif_id:
        Notification.objects.filter(user=request.user, pk=notif_id).update(is_read=True)
    else:
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({'status': 'ok'})

