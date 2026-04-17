"""
Views for the CEFR Exam test-taking interface.

Views:
  - dashboard_view: Lists all active tests for the student to choose from.
  - take_test_view: GET renders the full test form; POST grades and records the attempt.
"""

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
import json

from .models import Test, Question, UserAttempt, UserAnswer


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
        {'id': 'reading', 'name': 'Reading', 'active': False, 'image': 'images/reading_card.png'},
        {'id': 'writing', 'name': 'Writing', 'active': False, 'image': 'images/writing_card.png'},
        {'id': 'speaking', 'name': 'Speaking', 'active': False, 'image': 'images/speaking_card.png'},
    ]

    context = {
        'show_categories': category is None,
        'categories': categories,
    }

    if category:
        tests = (
            Test.objects
            .filter(is_active=True, test_type=category)
            .annotate(num_parts=Count('parts'))
            .prefetch_related('parts__questions')
            .order_by('-pk')
        )
        
        full_tests = [t for t in tests if t.num_parts >= 6]
        micro_tests = [t for t in tests if t.num_parts > 0 and t.num_parts < 6]
        
        category_name = next((c['name'] for c in categories if c['id'] == category), category.capitalize())
        
        context.update({
            'full_tests': full_tests,
            'micro_tests': micro_tests,
            'category_id': category,
            'category_name': category_name,
        })

    return render(request, 'exams/dashboard.html', context)


# ---------------------------------------------------------------------------
# Multi-Page Test Flow
# ---------------------------------------------------------------------------

@login_required
def test_tutorial_view(request, test_id):
    """
    Displays a tutorial and sound check page before starting the actual test.
    """
    test = get_object_or_404(Test, pk=test_id, is_active=True)
    return render(request, 'exams/tutorial.html', {'test': test})

@login_required
def start_test_view(request, test_id):
    """
    Creates a new UserAttempt and redirects to the first available part.
    """
    test = get_object_or_404(Test, pk=test_id, is_active=True)
    
    # Create the attempt record (timer starts now via started_at auto_now_add)
    attempt = UserAttempt.objects.create(
        user=request.user,
        test=test
    )
    
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
        
        # Determine next step
        if next_part_number:
            return redirect('exams:take_test_part', attempt_id=attempt.id, part_number=next_part_number)
        else:
            return _finalize_test(request, attempt)

    # GET — render the single part form
    # Prefetch questions and choices
    part_with_data = test.parts.filter(part_number=part_number).prefetch_related('questions__choices').first()

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
    }
    return render(request, 'exams/take_test.html', context)


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

    messages.success(request, f"Test completed! Final Score: {total_score}/{max_possible_score}")
    return redirect('exams:test_result', attempt_id=attempt.id)


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
    answers = attempt.answers.select_related('question__part').order_by('question__part__part_number', 'question__question_number')
    
    # Calculate stats per part natively
    part_results = attempt.get_part_results()

    context = {
        'attempt': attempt,
        'answers': answers,
        'part_results': part_results,
    }
    return render(request, 'exams/test_result.html', context)


@login_required
def exam_history_view(request, category=None):
    """
    Shows either the category selection page or the history for a specific category.
    """
    if not category:
        categories = [
            {'id': 'listening', 'name': 'Listening', 'image': 'images/listening_card.png', 'active': True},
            {'id': 'reading', 'name': 'Reading', 'image': 'images/reading_card.png', 'active': False},
            {'id': 'writing', 'name': 'Writing', 'image': 'images/writing_card.png', 'active': False},
            {'id': 'speaking', 'name': 'Speaking', 'image': 'images/speaking_card.png', 'active': False},
        ]
        return render(request, 'exams/history.html', {
            'show_categories': True,
            'categories': categories
        })

    if category != 'listening':
        # Simple "Coming Soon" or redirect for non-listening categories
        messages.info(request, f"{category.title()} history is coming soon!")
        return redirect('exams:history')

    # Existing logic for listening history
    attempts = list(UserAttempt.objects.filter(
        user=request.user,
        completed_at__isnull=False
    ).select_related('test').order_by('-started_at'))
    
    best_overall = None
    best_parts = {}

    for attempt in attempts:
        attempt.time_taken = attempt.completed_at - attempt.started_at
        
        # Check best overall
        if best_overall is None:
            best_overall = attempt
        else:
            if attempt.score_percentage > best_overall.score_percentage:
                best_overall = attempt
            elif attempt.score_percentage == best_overall.score_percentage:
                if attempt.time_taken < best_overall.time_taken:
                    best_overall = attempt
                    
        # Check best per part
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

    best_parts_list = [best_parts[k] for k in sorted(best_parts.keys())]

    # Prepare chart data
    chronological_attempts = reversed(attempts)
    chart_labels = []
    chart_data = []
    for att in chronological_attempts:
        label = f"{att.test.name} ({att.started_at.strftime('%b %d')})"
        chart_labels.append(label)
        chart_data.append(att.score_percentage)

    context = {
        'show_categories': False,
        'category': 'listening',
        'attempts': attempts,
        'best_overall': best_overall,
        'best_parts': best_parts_list,
        'chart_labels_json': json.dumps(chart_labels),
        'chart_data_json': json.dumps(chart_data),
    }
    return render(request, 'exams/history.html', context)
