"""
Views for the Mentor Panel.

Access control:
  - Users in the 'Mentors' group can create tests and edit/delete ONLY their own.
  - Superusers can do everything.

Main views:
  - mentor_dashboard: Lists tests the mentor can manage.
  - mentor_upload: Handles ZIP file upload and triggers the ingestion pipeline.
  - mentor_task_progress: Real-time progress page for a background ingestion task.
  - mentor_test_builder: Full AJAX-powered test editor for Parts/Questions/Choices.
  - mentor_delete_test: Confirmation page for deleting a test.

JSON API endpoints (used by the Test Builder frontend):
  - api_test_data: Returns the full test structure as JSON.
  - api_update_part: Updates a Part's fields.
  - api_create_question / api_update_question / api_delete_question
  - api_create_choice / api_update_choice / api_delete_choice
  - api_toggle_test_active: Toggles the is_active flag.
  - api_task_status: Returns ingestion task progress for polling.
"""

import json
import os
import shutil
import tempfile
import threading
import traceback
import zipfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files import File
from django.db import transaction, connection
from django.http import JsonResponse, HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from .models import Test, Part, Question, Choice, IngestionTask


# ---------------------------------------------------------------------------
# Access control helpers
# ---------------------------------------------------------------------------

def is_mentor_or_superuser(user):
    """Check if the user is in the Mentors group or is a superuser."""
    if user.is_superuser:
        return True
    return user.groups.filter(name='Mentors').exists()


def can_manage_test(user, test):
    """Check if the user can edit/delete this specific test."""
    if user.is_superuser:
        return True
    return test.author == user


def mentor_required(view_func):
    """Decorator: login_required + must be mentor or superuser."""
    from functools import wraps

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not is_mentor_or_superuser(request.user):
            return HttpResponseForbidden(
                "Access denied. You must be a Mentor or Superadmin."
            )
        return view_func(request, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Dashboard — list of tests the mentor can manage
# ---------------------------------------------------------------------------

@mentor_required
def mentor_dashboard(request):
    """Display all tests for the mentor to manage."""
    if request.user.is_superuser:
        tests = Test.objects.all().select_related('author')
    else:
        tests = Test.objects.filter(author=request.user).select_related('author')

    tests = tests.prefetch_related('parts__questions').order_by('-created_at')

    # Get active ingestion tasks for this user
    active_tasks = IngestionTask.objects.filter(
        user=request.user,
        status__in=['pending', 'running'],
    )

    context = {
        'tests': tests,
        'is_superuser': request.user.is_superuser,
        'active_tasks': active_tasks,
    }
    return render(request, 'exams/mentor/dashboard.html', context)


# ---------------------------------------------------------------------------
# ZIP Upload + Background Ingestion
# ---------------------------------------------------------------------------

@mentor_required
def mentor_upload(request):
    """
    GET:  Show the upload form.
    POST: Accept a ZIP file, create an IngestionTask, kick off background
          processing, and redirect to the progress page immediately.
    """
    if request.method == 'POST':
        zip_file = request.FILES.get('zip_file')
        if not zip_file:
            messages.error(request, "No file was uploaded.")
            return redirect('exams:mentor_upload')

        if not zip_file.name.endswith('.zip'):
            messages.error(request, "Please upload a .zip file.")
            return redirect('exams:mentor_upload')

        test_name = request.POST.get('test_name', '').strip()
        split_parts = request.POST.get('split_parts') == 'on'
        if not test_name:
            messages.error(request, "Please provide a test name.")
            return redirect('exams:mentor_upload')

        # Save ZIP to a persistent temp location (not TemporaryDirectory,
        # since the thread will outlive this request)
        upload_dir = settings.BASE_DIR / 'media' / 'uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)
        zip_path = upload_dir / f"task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{zip_file.name}"

        with open(zip_path, 'wb') as f:
            for chunk in zip_file.chunks():
                f.write(chunk)

        # Create the task record
        task = IngestionTask.objects.create(
            user=request.user,
            test_name=test_name,
            split_parts=split_parts,
            status='pending',
            stage='Загрузка файла завершена. Запуск обработки...',
        )

        # Start background processing thread
        thread = threading.Thread(
            target=_run_ingestion_background,
            args=(task.id, str(zip_path), test_name, request.user.id, split_parts),
            daemon=True,
        )
        thread.start()

        return redirect('exams:mentor_task_progress', task_id=task.id)

    return render(request, 'exams/mentor/upload.html')


def _run_ingestion_background(task_id, zip_path, test_name, user_id, split_parts):
    """
    Background thread entry point for test ingestion.
    Updates the IngestionTask record with progress as it works.

    IMPORTANT: Django DB connections are per-thread, so we close the
    inherited one and let Django create fresh ones for this thread.
    """
    connection.close()

    from django.contrib.auth import get_user_model
    User = get_user_model()

    try:
        task = IngestionTask.objects.get(id=task_id)
        task.status = 'running'
        task.progress = 5
        task.stage = 'Распаковка ZIP-архива...'
        task.save(update_fields=['status', 'progress', 'stage'])

        user = User.objects.get(id=user_id)
        materials_dir = settings.BASE_DIR / 'materials'
        materials_dir.mkdir(exist_ok=True)
        test_dir = materials_dir / test_name

        # --- Step 1: Extract ZIP ---
        task.update_progress(10, 'Распаковка ZIP-архива...')

        with tempfile.TemporaryDirectory(dir=str(settings.BASE_DIR)) as tmpdir:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmpdir)

            extracted = Path(tmpdir)
            subdirs = [d for d in extracted.iterdir() if d.is_dir()]
            source = subdirs[0] if len(subdirs) == 1 else extracted

            # Find Listening folder
            listening_src = None
            for candidate in [source / 'Listening', source]:
                if (candidate / 'Part 1').exists() or any(
                    d.name.startswith('Part') for d in candidate.iterdir() if d.is_dir()
                ):
                    listening_src = candidate
                    break

            if not listening_src:
                task.status = 'failed'
                task.error_message = (
                    'Не найдена правильная структура в ZIP. '
                    'Ожидается: TestName/Listening/Part 1/, Part 2/, ...'
                )
                task.stage = 'Ошибка структуры архива'
                task.completed_at = timezone.now()
                task.save()
                _cleanup_zip(zip_path)
                return

            task.update_progress(15, 'Копирование файлов...')

            if test_dir.exists():
                shutil.rmtree(test_dir)
            test_dir.mkdir(parents=True)
            target_listening = test_dir / 'Listening'
            shutil.copytree(str(listening_src), str(target_listening))

        # --- Step 2: Run the ingestion command ---
        task.update_progress(20, 'Запуск AI-обработки материалов...')

        from django.core.management import call_command

        stdout_capture = StringIO()
        stderr_capture = StringIO()

        # Create a progress callback that the ingest command can call
        # We'll monkey-patch stdout to intercept progress messages
        class ProgressCapture(StringIO):
            """Captures stdout and updates task progress based on output patterns."""
            def __init__(self, task_obj):
                super().__init__()
                self.task_obj = task_obj
                self._ocr_done = 0
                self._audio_done = 0
                self._total_parts = 6  # typical

            def write(self, text):
                super().write(text)
                # Parse progress from new parallel ingest output
                if '⚡  Running OCR' in text:
                    self.task_obj.update_progress(
                        20, 'Параллельная AI-обработка изображений...'
                    )
                elif 'OCR done' in text:
                    self._ocr_done += 1
                    pct = 20 + int((self._ocr_done / self._total_parts) * 30)
                    self.task_obj.update_progress(
                        min(pct, 50),
                        f'OCR завершён для {self._ocr_done} из ~{self._total_parts} частей...'
                    )
                elif '💾  Saving to database' in text:
                    self.task_obj.update_progress(55, 'Сохранение в базу данных...')
                elif '📁  Part' in text:
                    self.task_obj.update_progress(60, 'Запись частей и вопросов...')
                elif '🧠  Running audio analysis' in text:
                    self.task_obj.update_progress(
                        65, 'Параллельный анализ аудиодорожек...'
                    )
                elif 'audio analysis done' in text:
                    self._audio_done += 1
                    pct = 65 + int((self._audio_done / self._total_parts) * 20)
                    self.task_obj.update_progress(
                        min(pct, 85),
                        f'Аудио анализ завершён для {self._audio_done} из ~{self._total_parts} частей...'
                    )
                elif '✅  Successfully ingested' in text:
                    self.task_obj.update_progress(88, 'Основная обработка завершена...')

        progress_stdout = ProgressCapture(task)

        try:
            call_command(
                'ingest_materials',
                test=test_name,
                stdout=progress_stdout,
                stderr=stderr_capture,
            )
        except Exception as e:
            task.status = 'failed'
            task.error_message = (
                f"{str(e)}\n\nOutput:\n"
                f"{progress_stdout.getvalue()}\n{stderr_capture.getvalue()}"
            )
            task.stage = 'Ошибка обработки'
            task.completed_at = timezone.now()
            task.save()
            _cleanup_zip(zip_path)
            return

        # --- Step 3: Assign author + split parts ---
        task.update_progress(90, 'Назначение автора и постобработка...')

        try:
            test_obj = Test.objects.get(name=test_name)
            test_obj.author = user
            test_obj.save(update_fields=['author'])

            parts_count = test_obj.parts.count()
            questions_count = Question.objects.filter(part__test=test_obj).count()

            if split_parts:
                task.update_progress(93, 'Создание отдельных микро-тестов...')
                _clone_parts_to_individual_tests(test_obj, user)

            # --- Step 4: Success! ---
            task.status = 'completed'
            task.progress = 100
            task.stage = 'Готово!'
            task.result_test_id = test_obj.id
            task.parts_count = parts_count
            task.questions_count = questions_count
            task.completed_at = timezone.now()
            task.save()

        except Test.DoesNotExist:
            task.status = 'failed'
            task.error_message = 'Обработка завершилась, но тест не найден в базе данных.'
            task.stage = 'Ошибка'
            task.completed_at = timezone.now()
            task.save()

    except Exception as e:
        # Catch-all for unexpected errors
        try:
            task = IngestionTask.objects.get(id=task_id)
            task.status = 'failed'
            task.error_message = f"Неожиданная ошибка: {str(e)}\n\n{traceback.format_exc()}"
            task.stage = 'Критическая ошибка'
            task.completed_at = timezone.now()
            task.save()
        except Exception:
            pass  # Can't even update the task, nothing to do

    finally:
        _cleanup_zip(zip_path)
        connection.close()


def _clone_parts_to_individual_tests(test_obj, user):
    """Clone each part of a test into its own standalone micro-test."""
    for part in test_obj.parts.all():
        new_test_name = f"{test_obj.name} - Part {part.part_number}"
        Test.objects.filter(name=new_test_name).delete()

        new_test = Test.objects.create(
            name=new_test_name,
            test_type=test_obj.test_type,
            is_active=True,
            author=user,
        )

        part_clone = Part.objects.get(id=part.id)
        part_clone.id = None
        part_clone.test = new_test
        part_clone.save()

        for q in part.questions.all():
            q_clone = Question.objects.get(id=q.id)
            q_clone.id = None
            q_clone.part = part_clone
            q_clone.save()

            for c in q.choices.all():
                c_clone = Choice.objects.get(id=c.id)
                c_clone.id = None
                c_clone.question = q_clone
                c_clone.save()


def _cleanup_zip(zip_path):
    """Remove the temporary uploaded ZIP file."""
    try:
        if os.path.exists(zip_path):
            os.remove(zip_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Task Progress Page
# ---------------------------------------------------------------------------

@mentor_required
def mentor_task_progress(request, task_id):
    """Render the real-time progress page for an ingestion task."""
    task = get_object_or_404(IngestionTask, pk=task_id)
    if task.user != request.user and not request.user.is_superuser:
        return HttpResponseForbidden("You can only view your own tasks.")

    return render(request, 'exams/mentor/task_progress.html', {'task': task})


# ---------------------------------------------------------------------------
# Task Status API (polling)
# ---------------------------------------------------------------------------

@mentor_required
def api_task_status(request, task_id):
    """Return the current status of an ingestion task as JSON (for polling)."""
    task = get_object_or_404(IngestionTask, pk=task_id)
    if task.user != request.user and not request.user.is_superuser:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data = {
        'id': task.id,
        'status': task.status,
        'progress': task.progress,
        'stage': task.stage,
        'test_name': task.test_name,
        'result_test_id': task.result_test_id,
        'parts_count': task.parts_count,
        'questions_count': task.questions_count,
        'error_message': task.error_message,
    }
    return JsonResponse(data)


# ---------------------------------------------------------------------------
# Test Builder — full editor page
# ---------------------------------------------------------------------------

@mentor_required
def mentor_test_builder(request, test_id):
    """Render the all-in-one Test Builder for a specific test."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return HttpResponseForbidden("You can only edit your own tests.")

    return render(request, 'exams/mentor/test_builder.html', {
        'test': test,
        'is_superuser': request.user.is_superuser,
    })


# ---------------------------------------------------------------------------
# Delete Test
# ---------------------------------------------------------------------------

@mentor_required
def mentor_delete_test(request, test_id):
    """Confirm and delete a test."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return HttpResponseForbidden("You can only delete your own tests.")

    if request.method == 'POST':
        name = test.name
        test.delete()
        messages.success(request, f"Test «{name}» has been deleted.")
        return redirect('exams:mentor_dashboard')

    return render(request, 'exams/mentor/delete_confirm.html', {'test': test})


# ---------------------------------------------------------------------------
# JSON API — Full test data
# ---------------------------------------------------------------------------

@mentor_required
def api_test_data(request, test_id):
    """Return the full test structure as JSON for the Test Builder frontend."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    parts_data = []
    for part in test.parts.all().order_by('part_number'):
        questions_data = []
        for q in part.questions.all().order_by('question_number'):
            choices_data = []
            for c in q.choices.all().order_by('label'):
                choices_data.append({
                    'id': c.id,
                    'label': c.label,
                    'text': c.text,
                })
            questions_data.append({
                'id': q.id,
                'question_number': q.question_number,
                'global_question_number': q.global_question_number,
                'question_text': q.question_text,
                'question_type': q.question_type,
                'correct_answer': q.correct_answer,
                'group_label': q.group_label,
                'explanation': q.explanation,
                'choices': choices_data,
            })

        parts_data.append({
            'id': part.id,
            'part_number': part.part_number,
            'instructions': part.instructions,
            'points_per_question': part.points_per_question,
            'audio_file': part.audio_file.url if part.audio_file else None,
            'question_image': part.question_image.url if part.question_image else None,
            'map_image': part.map_image.url if part.map_image else None,
            'passage_title': part.passage_title,
            'passage_text': part.passage_text,
            'shared_choices_json': part.shared_choices_json,
            'transcript': part.transcript,
            'questions': questions_data,
        })

    return JsonResponse({
        'id': test.id,
        'name': test.name,
        'test_type': test.test_type,
        'is_active': test.is_active,
        'author': test.author.username if test.author else None,
        'parts': parts_data,
    })


# ---------------------------------------------------------------------------
# JSON API — Toggle test active
# ---------------------------------------------------------------------------

@mentor_required
@require_POST
def api_toggle_test_active(request, test_id):
    """Toggle the is_active flag of a test."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    test.is_active = not test.is_active
    test.save(update_fields=['is_active'])
    return JsonResponse({'is_active': test.is_active})


# ---------------------------------------------------------------------------
# JSON API — Update test name / type
# ---------------------------------------------------------------------------

@mentor_required
@require_POST
def api_update_test(request, test_id):
    """Update test-level fields (name, test_type)."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data = json.loads(request.body)
    if 'name' in data:
        test.name = data['name']
    if 'test_type' in data:
        test.test_type = data['test_type']
    test.save()

    return JsonResponse({'success': True, 'name': test.name, 'test_type': test.test_type})


# ---------------------------------------------------------------------------
# JSON API — Part CRUD
# ---------------------------------------------------------------------------

@mentor_required
@require_POST
def api_update_part(request, part_id):
    """Update a Part's editable fields."""
    part = get_object_or_404(Part, pk=part_id)
    if not can_manage_test(request.user, part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data = json.loads(request.body)

    for field in ['instructions', 'passage_title', 'passage_text', 'transcript']:
        if field in data:
            setattr(part, field, data[field])

    if 'shared_choices_json' in data:
        part.shared_choices_json = data['shared_choices_json']

    part.save()
    return JsonResponse({'success': True})


@mentor_required
@require_POST
def api_upload_part_audio(request, part_id):
    """Upload/replace the audio file for a Part."""
    part = get_object_or_404(Part, pk=part_id)
    if not can_manage_test(request.user, part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    audio = request.FILES.get('audio_file')
    if not audio:
        return JsonResponse({'error': 'No audio file provided.'}, status=400)

    slug = part.test.name.lower().replace(' ', '_')
    part.audio_file.save(
        f'{slug}_part{part.part_number}.mp3',
        audio,
        save=True,
    )
    return JsonResponse({'success': True, 'audio_url': part.audio_file.url})


# ---------------------------------------------------------------------------
# JSON API — Question CRUD
# ---------------------------------------------------------------------------

@mentor_required
@require_POST
def api_create_question(request, part_id):
    """Create a new question in a part."""
    part = get_object_or_404(Part, pk=part_id)
    if not can_manage_test(request.user, part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data = json.loads(request.body)

    # Auto-assign next question_number
    last_q = part.questions.order_by('-question_number').first()
    next_num = (last_q.question_number + 1) if last_q else 1

    q = Question.objects.create(
        part=part,
        question_number=next_num,
        question_text=data.get('question_text', ''),
        question_type=data.get('question_type', 'multiple_choice'),
        correct_answer=data.get('correct_answer', ''),
        group_label=data.get('group_label', ''),
    )

    return JsonResponse({
        'success': True,
        'id': q.id,
        'question_number': q.question_number,
    })


@mentor_required
@require_POST
def api_update_question(request, question_id):
    """Update a question's fields."""
    q = get_object_or_404(Question, pk=question_id)
    if not can_manage_test(request.user, q.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data = json.loads(request.body)

    for field in ['question_text', 'question_type', 'correct_answer',
                  'group_label', 'explanation', 'global_question_number']:
        if field in data:
            setattr(q, field, data[field])

    q.save()
    return JsonResponse({'success': True})


@mentor_required
@require_POST
def api_delete_question(request, question_id):
    """Delete a question and its choices."""
    q = get_object_or_404(Question, pk=question_id)
    if not can_manage_test(request.user, q.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    q.delete()
    return JsonResponse({'success': True})


# ---------------------------------------------------------------------------
# JSON API — Choice CRUD
# ---------------------------------------------------------------------------

@mentor_required
@require_POST
def api_create_choice(request, question_id):
    """Create a new choice for a question."""
    q = get_object_or_404(Question, pk=question_id)
    if not can_manage_test(request.user, q.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data = json.loads(request.body)
    c = Choice.objects.create(
        question=q,
        label=data.get('label', ''),
        text=data.get('text', ''),
    )
    return JsonResponse({'success': True, 'id': c.id})


@mentor_required
@require_POST
def api_update_choice(request, choice_id):
    """Update a choice's label and text."""
    c = get_object_or_404(Choice, pk=choice_id)
    if not can_manage_test(request.user, c.question.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data = json.loads(request.body)
    if 'label' in data:
        c.label = data['label']
    if 'text' in data:
        c.text = data['text']
    c.save()
    return JsonResponse({'success': True})


@mentor_required
@require_POST
def api_delete_choice(request, choice_id):
    """Delete a choice."""
    c = get_object_or_404(Choice, pk=choice_id)
    if not can_manage_test(request.user, c.question.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    c.delete()
    return JsonResponse({'success': True})
