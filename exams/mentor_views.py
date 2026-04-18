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

from .models import Test, Part, Question, Choice, IngestionTask, WritingTask


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
# ZIP Upload + Background Ingestion & Manual Creation
# ---------------------------------------------------------------------------

@mentor_required
def mentor_create_empty_test(request):
    """Create a new blank test and redirect to its builder."""
    if request.method == 'POST':
        # Create a blank test
        test_type = request.POST.get('test_type', 'listening')
        new_test = Test.objects.create(
            name=f"Новый тест (Черновик) - {timezone.now().strftime('%Y-%m-%d %H:%M')}",
            test_type=test_type,
            author=request.user,
            is_active=False
        )
        return redirect('exams:mentor_test_builder', test_id=new_test.id)
    return redirect('exams:mentor_dashboard')

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

        is_zip = zip_file.name.lower().endswith('.zip')
        is_image = zip_file.name.lower().endswith(('.jpg', '.jpeg', '.png'))

        if not is_zip and not is_image:
            messages.error(request, "Please upload a .zip file or an image (.jpg, .png).")
            return redirect('exams:mentor_upload')

        test_name = request.POST.get('test_name', '').strip()
        split_parts = request.POST.get('split_parts') == 'on'
        photo_test_type = request.POST.get('photo_test_type', 'Writing')

        if not test_name:
            messages.error(request, "Please provide a test name.")
            return redirect('exams:mentor_upload')

        # Save ZIP to a persistent temp location
        upload_dir = settings.BASE_DIR / 'media' / 'uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        if is_image:
            # Wrap image in a ZIP file to match the ingestion pipeline structure
            safe_name = test_name.replace(' ', '_').replace('/', '_')
            zip_filename = f"task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}_photo.zip"
            zip_path = upload_dir / zip_filename
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Seed the same image into Part 1 and Part 2 (especially for Writing!)
                _, ext = os.path.splitext(zip_file.name)
                archive_path_1 = f"{safe_name}/{photo_test_type}/Part 1/questions{ext}"
                archive_path_2 = f"{safe_name}/{photo_test_type}/Part 2/questions{ext}"
                file_bytes = zip_file.read()
                zf.writestr(archive_path_1, file_bytes)
                zf.writestr(archive_path_2, file_bytes)
        else:
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


@mentor_required
def mentor_upload_writing(request):
    """
    GET:  Show the dedicated Writing upload form.
    POST: Accept a ZIP file or image, kick off background processing.
    """
    if request.method == 'POST':
        zip_file = request.FILES.get('zip_file')
        if not zip_file:
            messages.error(request, "No file was uploaded.")
            return redirect('exams:mentor_upload_writing')

        is_zip = zip_file.name.lower().endswith('.zip')
        is_image = zip_file.name.lower().endswith(('.jpg', '.jpeg', '.png'))

        if not is_zip and not is_image:
            messages.error(request, "Please upload a .zip file or an image (.jpg, .png).")
            return redirect('exams:mentor_upload_writing')

        test_name = request.POST.get('test_name', '').strip()
        split_parts = request.POST.get('split_parts') == 'on'
        photo_test_type = 'Writing'  # Hardcoded for this view

        if not test_name:
            messages.error(request, "Please provide a test name.")
            return redirect('exams:mentor_upload_writing')

        upload_dir = settings.BASE_DIR / 'media' / 'uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        if is_image:
            safe_name = test_name.replace(' ', '_').replace('/', '_')
            zip_filename = f"task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}_photo.zip"
            zip_path = upload_dir / zip_filename
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                _, ext = os.path.splitext(zip_file.name)
                archive_path_1 = f"{safe_name}/{photo_test_type}/Part 1/questions{ext}"
                archive_path_2 = f"{safe_name}/{photo_test_type}/Part 2/questions{ext}"
                file_bytes = zip_file.read()
                zf.writestr(archive_path_1, file_bytes)
                zf.writestr(archive_path_2, file_bytes)
        else:
            zip_path = upload_dir / f"task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{zip_file.name}"
            with open(zip_path, 'wb') as f:
                for chunk in zip_file.chunks():
                    f.write(chunk)

        task = IngestionTask.objects.create(
            user=request.user,
            test_name=test_name,
            split_parts=split_parts,
            status='pending',
            stage='Загрузка файла завершена. Запуск обработки...',
        )

        thread = threading.Thread(
            target=_run_ingestion_background,
            args=(task.id, str(zip_path), test_name, request.user.id, split_parts),
            daemon=True,
        )
        thread.start()

        return redirect('exams:mentor_task_progress', task_id=task.id)

    return render(request, 'exams/mentor/upload_writing.html')


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

            valid_modules = ['Listening', 'Reading', 'Writing', 'Speaking']
            found_modules = []

            for module in valid_modules:
                candidate = source / module
                if candidate.exists() and candidate.is_dir():
                    found_modules.append((candidate, module))
            
            # Fallback if no explicit module folders (e.g. root just has "Part 1")
            if not found_modules and any(d.name.startswith('Part') for d in source.iterdir() if d.is_dir()):
                # Guess from the task or default to 'Listening' for legacy compat
                # But since we wrapped photos in `<TestType>/Part 1`, this will just work.
                found_modules.append((source, 'Listening'))

            if not found_modules:
                task.status = 'failed'
                task.error_message = (
                    'Не найдена правильная структура в ZIP. '
                    'Ожидается папка Listening/, Reading/, Writing/ или Speaking/ '
                    'содержащая Part 1, Part 2 и т.д.'
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
            
            for src_dir, module_name in found_modules:
                target_dir = test_dir / module_name
                shutil.copytree(str(src_dir), str(target_dir))

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

            if test_obj.test_type == 'writing':
                parts_count = 1  # Or logically 2 parts, but let's count tasks
                questions_count = test_obj.writing_tasks.count()
            else:
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
    """Clone each part of a test into its own standalone micro-test. Supports Writing too."""
    if test_obj.test_type == 'writing':
        tasks = test_obj.writing_tasks.all()
        # Group tasks by their logical 'part' based on order (1, 2 = Part 1; 3 = Part 2)
        part1_tasks = [t for t in tasks if t.order in (1, 2)]
        part2_tasks = [t for t in tasks if t.order == 3]
        
        for part_num, task_group in [(1, part1_tasks), (2, part2_tasks)]:
            if not task_group: continue
            new_test_name = f"{test_obj.name} - Part {part_num}"
            Test.objects.filter(name=new_test_name).delete()
            new_test = Test.objects.create(
                name=new_test_name, test_type=test_obj.test_type, is_active=True, author=user
            )
            for t in task_group:
                t.id = None
                t.test = new_test
                t.save()
        return

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
        'writing_tasks': [
            {
                'id': wt.id,
                'task_type': wt.task_type,
                'input_text': wt.input_text,
                'prompt': wt.prompt,
                'min_words': wt.min_words,
                'max_words': wt.max_words,
                'order': wt.order,
            } for wt in test.writing_tasks.all().order_by('order')
        ],
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

@mentor_required
@require_POST
def api_publish_test(request, test_id):
    """Set test to active, and optionally clone its parts."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
        
    try:
        data = json.loads(request.body)
        split_parts = data.get('split_parts', False)
        
        test.is_active = True
        test.save(update_fields=['is_active'])
        
        if split_parts:
            _clone_parts_to_individual_tests(test, request.user)
            
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

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
# JSON API — Writing test specific CRUD
# ---------------------------------------------------------------------------

@mentor_required
@require_POST
def api_generate_writing_test(request):
    """Generate a complete 3-part writing test using Gemini AI."""
    import os
    try:
        from google import genai
        from google.genai import types as T
    except ImportError:
        return JsonResponse({'error': 'google-genai SDK not installed'}, status=500)
    
    data = json.loads(request.body)
    topic = data.get('topic', 'Technology')
    
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        if os.path.exists(adc_path):
            with open(adc_path) as f:
                project_id = json.load(f).get("quota_project_id")
                
    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
    except Exception as e:
        return JsonResponse({'error': f'Failed to init GenAI client: {e}'}, status=500)
    
    prompt = f"""You are an expert UzbMB/DTM CEFR B2 Writing exam paper creator for the Uzbekistan national format.
Generate a COMPLETE and REALISTIC writing exam on the topic: "{topic}".

CRITICAL: Study and follow these REAL exam examples EXACTLY:

=== EXAMPLE 1 (Topic: Theatre) ===
PART 1:
situation_context: "You are a member of the theatre group. You receive this email from the secretary."
shared_input_text: "Dear member,\\nSome of our members have said that they are interested in a theatre trip to London in July. Therefore, I have checked the theatre listings and I have found a musical that I think would appeal to everybody. It is called 'Summer in the City' and is on 17th July. If you are interested in going, please let us know as soon as possible, so that we can book the tickets and a coach."
task_1_1_prompt: "Write an email to your friend. Write about whether you would like to attend and why it is/isn't of interest to you. Write about 50 words."
task_1_2_prompt: "Write an email to the secretary. Write about whether you would like to attend and why it is/isn't of interest to you. Write about 120-150 words."

PART 2:
essay_theme: "We are running a writing competition on our website. The theme this month is entertainment."
essay_instruction: "Write your essay in response to this statement:"
essay_statement: "Entertainment plays an important role in the happiness of all people."
essay_word_count: "Write 180-200 words."

=== EXAMPLE 2 (Topic: Sports) ===
PART 1:
situation_context: "You are a member of the sports club. You receive this email from the secretary."
shared_input_text: "Dear member,\\nMany of our members have expressed an interest in buying second-hand sports equipment. As a result, we will be organising a bring-and-buy sale of used sports equipment at the sports club, on Friday 8th June. Please let the club secretary know if you would be interested in the sale and if you could bring any equipment for sale."
task_1_1_prompt: "Write an e-mail to your friend. Write about whether you would like to attend and why it is/isn't of interest to you. Write about 50 words."
task_1_2_prompt: "Write an email to the secretary. Write about whether you would like to attend and why it is/isn't of interest to you. Write about 120-150 words."

PART 2:
essay_theme: "We are running a writing competition on our website. The theme this month is sport and exercise."
essay_instruction: "Write your essay in response to this statement:"
essay_statement: "Sport and exercise can be beneficial for everyone"
essay_word_count: "Write 180-200 words."

=== IMPORTANT FORMAT RULES ===
1. PART 1: The student is ALWAYS a member of some club/group/course. They receive an email from an organiser/secretary/teacher about an upcoming event/activity.
2. The shared_input_text is ALWAYS a polite email (Dear member/student, ...) describing the event and asking for interest/participation.
3. Task 1.1 is ALWAYS "Write an email to your friend..." (informal, ~50 words)
4. Task 1.2 is ALWAYS "Write an email to the [sender role]..." (formal, ~120-150 words)
5. BOTH tasks reference the SAME situation and input text.
6. PART 2 essay ALWAYS follows this structure: theme intro sentence, "Write your essay in response to this statement:", then a quotable statement in quotes, then word count.

Now generate a NEW, ORIGINAL exam for the topic "{topic}".
Return STRICTLY this JSON:
{{
  "test_name": "Writing Test — [Topic Name]",
  "situation_context": "You are a member of ... You receive this email from the ...",
  "shared_input_text": "Dear ...,\\n[3-5 sentences describing an event/opportunity related to {topic}]",
  "task_1_1_prompt": "Write an email to your friend. Write about whether you would like to ... Write about 50 words.",
  "task_1_2_prompt": "Write an email to the [role]. Write about whether you would like to ... Write about 120-150 words.",
  "essay_theme": "We are running a writing competition on our website. The theme this month is [topic area].",
  "essay_statement": "[A debatable statement related to {topic}]"
}}"""

    generate_cfg = T.GenerateContentConfig(
        response_mime_type="application/json",
    )
    
    try:
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[prompt],
            config=generate_cfg,
        )
        
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
        
        result_data = json.loads(raw_text.strip())
        
        # Build the structured content from the AI response
        situation = result_data.get('situation_context', '')
        shared_input = result_data.get('shared_input_text', '')
        # Combine situation + letter into the input_text field
        full_input_text = f"{situation}\n\n{shared_input}" if situation else shared_input
        
        essay_theme = result_data.get('essay_theme', '')
        essay_statement = result_data.get('essay_statement', '')
        essay_input = f'{essay_theme}\n\nWrite your essay in response to this statement:\n\n"{essay_statement}"'
        
        with transaction.atomic():
            test_obj = Test.objects.create(
                name=result_data.get('test_name', f"Writing Test — {topic}"),
                test_type='writing',
                author=request.user,
                is_active=False
            )
            
            # Task 1.1 — Informal (to a friend)
            WritingTask.objects.create(
                test=test_obj, task_type='informal', order=1,
                input_text=full_input_text,
                prompt=result_data.get('task_1_1_prompt', 'Write an email to your friend. Write about 50 words.'),
                min_words=40, max_words=60
            )
            
            # Task 1.2 — Formal (to the organiser)
            WritingTask.objects.create(
                test=test_obj, task_type='formal', order=2,
                input_text=full_input_text,  # Same input — shared
                prompt=result_data.get('task_1_2_prompt', 'Write an email to the organiser. Write about 120-150 words.'),
                min_words=120, max_words=150
            )
            
            # Task 2 — Essay
            WritingTask.objects.create(
                test=test_obj, task_type='essay', order=3,
                input_text=essay_input,
                prompt='Write 180-200 words.',
                min_words=180, max_words=200
            )
            
        return JsonResponse({'success': True, 'test_id': test_obj.id})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@mentor_required
@require_POST
def api_update_writing_task(request, task_id):
    """Update fields of a manual WritingTask."""
    wt = get_object_or_404(WritingTask, pk=task_id)
    if not can_manage_test(request.user, wt.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data = json.loads(request.body)
    for field in ['input_text', 'prompt', 'min_words', 'max_words']:
        if field in data:
            setattr(wt, field, data[field])
    wt.save()
    return JsonResponse({'success': True})


@mentor_required
@require_POST
def api_create_writing_tasks_for_test(request, test_id):
    """Initialize empty writing tasks manually for a test."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
        
    if test.writing_tasks.exists():
        return JsonResponse({'error': 'Writing tasks already initialized.'}, status=400)
        
    with transaction.atomic():
        WritingTask.objects.create(test=test, task_type='informal', order=1, prompt='', min_words=40, max_words=60)
        WritingTask.objects.create(test=test, task_type='formal', order=2, prompt='', min_words=120, max_words=150)
        WritingTask.objects.create(test=test, task_type='essay', order=3, prompt='', min_words=180, max_words=200)
        
    return JsonResponse({'success': True})


# ---------------------------------------------------------------------------
# JSON API — Part CRUD
# ---------------------------------------------------------------------------

@mentor_required
@require_POST
def api_create_part(request, test_id):
    """Create a new Part in a specific Test."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    # find next part number
    last_part = test.parts.order_by('-part_number').first()
    next_num = (last_part.part_number + 1) if last_part else 1
    
    # max 10 parts, arbitrary safe limit
    if next_num > 10:
        return JsonResponse({'error': 'Too many parts'}, status=400)

    # Get default points (fallback to 2.0)
    pts = Part.PART_WEIGHTS.get(next_num, 2.0)

    part = Part.objects.create(
        test=test,
        part_number=next_num,
        points_per_question=pts
    )

    return JsonResponse({
        'success': True,
        'id': part.id,
        'part_number': part.part_number,
        'points_per_question': part.points_per_question
    })

@mentor_required
@require_POST
def api_delete_part(request, part_id):
    """Delete a Part and renumber subsequent parts."""
    part = get_object_or_404(Part, pk=part_id)
    if not can_manage_test(request.user, part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
        
    test = part.test
    part.delete()
    
    # renumber subsequent parts to ensure sequential part numbers if needed
    # for simplicity, we let the user reorganize manually if desired, or auto renumber
    for i, p in enumerate(test.parts.order_by('part_number'), 1):
        if p.part_number != i:
            p.part_number = i
            p.save(update_fields=['part_number'])
            
    return JsonResponse({'success': True})

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
