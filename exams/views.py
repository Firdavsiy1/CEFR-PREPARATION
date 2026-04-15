"""
Views for the CEFR Exam test-taking interface.

Views:
  - dashboard_view: Lists all active tests for the student to choose from.
  - take_test_view: GET renders the full test form; POST grades and records the attempt.
"""

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import Test, Question, UserAttempt, UserAnswer


# ---------------------------------------------------------------------------
# Dashboard — list of available tests
# ---------------------------------------------------------------------------

@login_required
def dashboard_view(request):
    """Display all active tests so the student can choose one."""
    
    # Auto-cleanup abandoned tests for this user (older than 2 hours)
    cutoff = timezone.now() - timezone.timedelta(hours=2)
    UserAttempt.objects.filter(
        user=request.user,
        completed_at__isnull=True,
        started_at__lt=cutoff
    ).delete()

    tests = (
        Test.objects
        .filter(is_active=True)
        .prefetch_related('parts__questions')
    )
    return render(request, 'exams/dashboard.html', {'tests': tests})


# ---------------------------------------------------------------------------
# Multi-Page Test Flow
# ---------------------------------------------------------------------------

@login_required
def start_test_view(request, test_id):
    """
    Creates a new UserAttempt and redirects to Part 1.
    """
    test = get_object_or_404(Test, pk=test_id, is_active=True)
    
    # Create the attempt record (timer starts now via started_at auto_now_add)
    attempt = UserAttempt.objects.create(
        user=request.user,
        test=test
    )
    
    return redirect('exams:take_test_part', attempt_id=attempt.id, part_number=1)


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
        if part_number < 6:
            return redirect('exams:take_test_part', attempt_id=attempt.id, part_number=part_number + 1)
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
def exam_history_view(request):
    """
    Display a list of all completed tests the user has taken.
    """
    attempts = UserAttempt.objects.filter(
        user=request.user,
        completed_at__isnull=False
    ).select_related('test').order_by('-started_at')
    
    context = {
        'attempts': attempts,
    }
    return render(request, 'exams/history.html', context)
