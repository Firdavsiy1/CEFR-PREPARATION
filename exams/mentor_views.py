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

import base64
import json
import logging
import os
import shutil
import tempfile
import traceback
import zipfile

logger = logging.getLogger(__name__)
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction, connection
from django.db.models import Exists, OuterRef
from django.http import JsonResponse, HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from .image_processing import align_perspective, crop_image
from .models import (
    Test, Part, Question, Choice, IngestionTask, WritingTask,
    ReadingTest, ReadingPart, ReadingPassage, ReadingQuestion,
    SpeakingPart, SpeakingQuestion,
)
from .reading_services import build_parts_data_from_folder, parse_reading_materials, ingest_reading_json, generate_reading_explanations
from .speaking_services import process_speaking_page, generate_alt_text


# ---------------------------------------------------------------------------
# Access control helpers
# ---------------------------------------------------------------------------

def is_mentor_or_superuser(user):
    """Check if the user has mentor role or is a superuser."""
    if user.is_superuser:
        return True
    try:
        return user.profile.role in ['mentor', 'sysmentor']
    except Exception:
        return False

def is_sysmentor_or_superuser(user):
    """Check if the user has sysmentor role or is a superuser."""
    if user.is_superuser:
        return True
    try:
        return user.profile.role == 'sysmentor'
    except Exception:
        return False


def is_admin_user(user):
    """Check if the user has admin privileges (superuser only)."""
    return user.is_superuser


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

def sysmentor_required(view_func):
    """Decorator: login_required + must be sysmentor or superuser."""
    from functools import wraps

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not is_sysmentor_or_superuser(request.user):
            return HttpResponseForbidden(
                "Access denied. You must be a SysMentor or Superadmin."
            )
        return view_func(request, *args, **kwargs)

    return wrapper


def admin_required(view_func):
    """Decorator: login_required + must be admin (Admins group or superuser)."""
    from functools import wraps

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not is_admin_user(request.user):
            return HttpResponseForbidden(
                "Access denied. You must be an Admin."
            )
        return view_func(request, *args, **kwargs)

    return wrapper


def _issue(severity, code, message, location=None):
    return {
        'severity': severity,
        'code': code,
        'message': message,
        'location': location or '',
    }


def _build_test_validation_report(test):
    """Return blockers/warnings that determine whether a test is ready to publish."""
    blockers = []
    warnings = []

    def add_blocker(code, message, location=None):
        blockers.append(_issue('blocker', code, message, location))

    def add_warning(code, message, location=None):
        warnings.append(_issue('warning', code, message, location))

    if not (test.name or '').strip():
        add_blocker('test.name.empty', 'У теста отсутствует название.', 'Тест')

    if test.test_type == 'reading':
        parts = list(test.reading_parts.select_related('passage').prefetch_related('questions').order_by('part_number'))
        if not parts:
            add_blocker('reading.parts.empty', 'В reading тесте нет ни одной части.', 'Reading')
        for part in parts:
            location = f'Reading Part {part.part_number}'
            if not (part.instruction or '').strip():
                add_warning('reading.instruction.empty', 'У части нет инструкции.', location)
            questions = list(part.questions.all())
            if not questions:
                add_blocker('reading.questions.empty', 'В части нет вопросов.', location)
            if (part.question_number_start is None) != (part.question_number_end is None):
                add_warning('reading.range.partial', 'Диапазон вопросов указан неполностью.', location)
            if part.question_number_start and part.question_number_end and part.question_number_start > part.question_number_end:
                add_blocker('reading.range.invalid', 'Начальный номер вопроса больше конечного.', location)
            has_passage = hasattr(part, 'passage')
            for question in questions:
                q_loc = f'{location} · Q{question.question_number}'
                if not (question.correct_answer or '').strip():
                    add_blocker('reading.correct_answer.empty', 'У вопроса отсутствует правильный ответ.', q_loc)
                if question.question_type in {'multiple_choice', 'matching'} and not question.options:
                    add_blocker('reading.options.empty', 'Для этого типа вопроса нужны варианты ответа.', q_loc)
                if question.question_type == 'multiple_choice' and len(question.options) < 2:
                    add_blocker('reading.options.too_few', 'Для multiple choice нужно минимум 2 варианта.', q_loc)
                if question.question_type == 'fill_in_the_blank' and not has_passage:
                    add_blocker('reading.passage.missing', 'Fill in the blank требует passage.', q_loc)
            if has_passage:
                passage = part.passage
                if not (passage.content or '').strip():
                    add_blocker('reading.passage.empty', 'Passage существует, но его текст пуст.', location)
            else:
                if any(q.question_type == 'fill_in_the_blank' for q in questions):
                    add_blocker('reading.passage.required', 'Для gap-fill части отсутствует passage.', location)

    elif test.test_type == 'writing':
        tasks = list(test.writing_tasks.all().order_by('order'))
        if not tasks:
            add_blocker('writing.tasks.empty', 'В writing тесте нет заданий.', 'Writing')
        expected_types = {'informal', 'formal', 'essay'}
        present_types = {task.task_type for task in tasks}
        missing_types = expected_types - present_types
        if missing_types:
            add_blocker('writing.tasks.missing', f'Отсутствуют обязательные writing task types: {", ".join(sorted(missing_types))}.', 'Writing')
        for task in tasks:
            location = f'Writing {task.get_task_type_display()}'
            if not (task.prompt or '').strip():
                add_blocker('writing.prompt.empty', 'У задания отсутствует prompt.', location)
            if task.task_type in {'informal', 'formal'} and not (task.input_text or '').strip():
                add_warning('writing.input_text.empty', 'У письма отсутствует входной текст/ситуация.', location)
            if task.min_words and task.max_words and task.min_words > task.max_words:
                add_blocker('writing.word_range.invalid', 'Минимум слов больше максимума.', location)
            if task.max_words == 0:
                add_warning('writing.word_range.empty', 'Для задания не задан лимит слов.', location)

    elif test.test_type == 'speaking':
        parts = list(test.speaking_parts.prefetch_related('questions').order_by('part_number'))
        if not parts:
            add_blocker('speaking.parts.empty', 'В speaking тесте нет частей.', 'Speaking')
        for part in parts:
            location = f'Speaking Part {part.part_number}'
            questions = list(part.questions.all())
            if not questions:
                add_blocker('speaking.questions.empty', 'У части нет вопросов.', location)
            if not (part.instructions or '').strip():
                add_warning('speaking.instructions.empty', 'У части нет инструкции.', location)
            if not part.is_validated:
                add_blocker('speaking.validation.pending', 'Часть не прошла валидацию после OCR. Откройте Speaking Builder и подтвердите все части.', location)
            if part.part_number in {2, 3} and not (part.cropped_image or part.original_image):
                add_blocker('speaking.image.missing', 'Для части {n} обязателен reference image (cropped или original). Загрузите изображение в Speaking Builder.'.format(n=part.part_number), location)
            for question in questions:
                q_loc = f'{location} · Q{question.question_number}'
                if not (question.question_text or '').strip():
                    add_blocker('speaking.question.empty', 'Текст вопроса пустой.', q_loc)
                if not question.audio_file:
                    add_warning('speaking.audio.missing', 'Для вопроса ещё не сгенерировано TTS-аудио.', q_loc)

    else:
        parts = list(test.parts.prefetch_related('questions__choices').order_by('part_number'))
        if not parts:
            add_blocker('listening.parts.empty', 'В listening тесте нет частей.', 'Listening')
        for part in parts:
            location = f'Listening Part {part.part_number}'
            questions = list(part.questions.all())
            if not questions:
                add_blocker('listening.questions.empty', 'У части нет вопросов.', location)
            if not (part.instructions or '').strip():
                add_warning('listening.instructions.empty', 'У части нет инструкции.', location)
            if not part.audio_file:
                add_blocker('listening.audio.missing', 'У части отсутствует аудио.', location)
            if part.part_number == 4 and not part.map_image:
                add_warning('listening.map.missing', 'Для Part 4 обычно ожидается map image.', location)
            for question in questions:
                q_loc = f'{location} · Q{question.question_number}'
                if not (question.correct_answer or '').strip():
                    add_blocker('listening.correct_answer.empty', 'У вопроса отсутствует правильный ответ.', q_loc)
                if question.question_type == 'multiple_choice' and question.choices.count() < 2:
                    add_blocker('listening.choices.too_few', 'Для multiple choice нужно минимум 2 варианта.', q_loc)
                if question.question_type in {'fill_blank', 'map_label'} and not (question.question_text or '').strip() and not part.question_image:
                    add_warning('listening.question_context.thin', 'У вопроса мало текстового контекста и нет question image.', q_loc)

    all_issues = blockers + warnings
    return {
        'test_id': test.id,
        'test_type': test.test_type,
        'is_publishable': not blockers,
        'blockers': blockers,
        'warnings': warnings,
        'summary': {
            'blockers': len(blockers),
            'warnings': len(warnings),
            'parts': test.num_parts,
            'questions': test.total_questions,
        },
        'preview_url': reverse('exams:mentor_test_preview', args=[test.id]),
        'issues': all_issues,
    }


def _get_manageable_tests_for_user(user):
    qs = Test.objects.filter(is_deleted=False, is_class_only=False).select_related('author')
    if user.is_superuser:
        return qs
    return qs.filter(author=user)


def _build_reading_preview_parts(test):
    return list(
        test.reading_parts.select_related('passage').prefetch_related('questions').order_by('part_number')
    )


def _build_listening_preview_parts(test):
    return list(test.parts.prefetch_related('questions__choices').order_by('part_number'))


def _build_speaking_preview_parts(test):
    return list(test.speaking_parts.prefetch_related('questions').order_by('part_number'))


def _coerce_speaking_part_number(raw_part_number, fallback_number, used_numbers=None):
    """Convert OCR part labels to safe integer part numbers (1..4) without collisions."""
    if used_numbers is None:
        used_numbers = set()

    raw = str(raw_part_number).strip().replace(',', '.') if raw_part_number is not None else ''

    preferred = None
    if raw in ('1.1', '1'):
        preferred = 1
    elif raw == '1.2':
        preferred = 2
    else:
        try:
            num = int(float(raw))
            if 1 <= num <= 4:
                preferred = num
        except (TypeError, ValueError):
            preferred = None

    if preferred is None:
        preferred = fallback_number if 1 <= fallback_number <= 4 else 2

    if preferred not in used_numbers:
        return preferred

    for n in (1, 2, 3, 4):
        if n not in used_numbers:
            return n

    raise ValueError(
        f"Cannot allocate a unique speaking part number: "
        f"all slots 1-4 are occupied (used={used_numbers})."
    )


def _auto_generate_tts_for_part(speaking_part):
    """Silently pre-generate TTS audio for all questions in a part.

    For Part 3 (debate), the TTS narration covers the full table
    (topic + FOR points + AGAINST points) so the student hears the
    complete context.  Any failure is logged and silently suppressed
    so ingestion is never blocked by TTS unavailability.
    """
    try:
        from .tts_service import generate_tts_base64
        from django.core.files.base import ContentFile as _CF

        debate_data = speaking_part.debate_data or {}
        is_debate_part = (speaking_part.part_number == 4)

        for question in speaking_part.questions.all().order_by('question_number'):
            if question.audio_file:
                continue  # already has audio — skip

            text = question.question_text.strip()

            # For Part 3 build the full table narration
            if is_debate_part and debate_data.get('topic'):
                for_points = '. '.join(debate_data.get('for_points') or [])
                against_points = '. '.join(debate_data.get('against_points') or [])
                text = f"Discussion topic: {debate_data['topic']}."
                if for_points:
                    text += f" Arguments for: {for_points}."
                if against_points:
                    text += f" Arguments against: {against_points}."

            if not text:
                continue

            audio_b64 = generate_tts_base64(text, language_code='en-US')
            if not audio_b64:
                continue

            audio_bytes = base64.b64decode(audio_b64)
            question.audio_file.save(f"q_{question.id}.mp3", _CF(audio_bytes), save=True)

    except Exception as exc:
        logger.warning("TTS auto-gen failed for part %s: %s", speaking_part.id, exc)


def _auto_save_speaking_questions(speaking_part, raw_questions: list):
    """Persist questions from OCR data directly to SpeakingQuestion records."""
    from .models import SpeakingQuestion
    speaking_part.questions.all().delete()
    used_numbers = set()
    for idx, q in enumerate(raw_questions, start=1):
        if not isinstance(q, dict):
            continue
        q_text = str(q.get('text') or '').strip()
        if not q_text:
            continue
        try:
            q_num = int(q.get('q_num', idx))
        except (TypeError, ValueError):
            q_num = idx
        if q_num <= 0:
            q_num = idx
        while q_num in used_numbers:
            q_num += 1
        used_numbers.add(q_num)
        SpeakingQuestion.objects.create(
            part=speaking_part,
            question_number=q_num,
            question_text=q_text,
        )


# ---------------------------------------------------------------------------
# Dashboard — list of tests the mentor can manage
# ---------------------------------------------------------------------------

@sysmentor_required
def mentor_dashboard(request):
    """Display all tests for the mentor to manage."""
    if request.method == 'POST':
        action = request.POST.get('bulk_action', '').strip()
        selected_ids = [int(pk) for pk in request.POST.getlist('selected_tests') if str(pk).isdigit()]
        managed_tests = list(_get_manageable_tests_for_user(request.user).filter(pk__in=selected_ids))
        managed_map = {test.pk: test for test in managed_tests}

        if not selected_ids:
            messages.warning(request, 'Не выбрано ни одного теста для массового действия.')
            return redirect('exams:mentor_dashboard')

        skipped = max(0, len(selected_ids) - len(managed_tests))
        changed = 0
        validation_failures = []

        for test in managed_tests:
            if action == 'publish':
                report = _build_test_validation_report(test)
                if not report['is_publishable']:
                    validation_failures.append(f'{test.name}: {report["summary"]["blockers"]} blockers')
                    continue
                if not test.is_active:
                    test.is_active = True
                    test.save(update_fields=['is_active'])
                    changed += 1
            elif action == 'unpublish':
                if test.is_active:
                    test.is_active = False
                    test.save(update_fields=['is_active'])
                    changed += 1
            elif action == 'duplicate':
                clone_name = f'{test.name} — Copy {timezone.now().strftime("%H%M%S")}'
                _duplicate_test_as_draft(test, request.user, clone_name)
                changed += 1
            elif action == 'hide':
                if not test.is_deleted:
                    test.is_deleted = True
                    test.is_active = False
                    test.deleted_at = timezone.now()
                    test.save(update_fields=['is_deleted', 'is_active', 'deleted_at'])
                    changed += 1

        if action == 'publish' and validation_failures:
            messages.warning(request, f'Не все тесты опубликованы: {"; ".join(validation_failures[:3])}')
        if changed:
            messages.success(request, f'Массовое действие «{action}» выполнено для {changed} тестов.')
        elif skipped:
            messages.warning(request, 'Часть выбранных тестов недоступна для текущего пользователя.')
        return redirect('exams:mentor_dashboard')

    tests = _get_manageable_tests_for_user(request.user)

    tests = list(
        tests.prefetch_related('parts__questions', 'speaking_parts__questions', 'cloned_tests').annotate(
            has_unvalidated_speaking=Exists(
                SpeakingPart.objects.filter(test=OuterRef('pk'), is_validated=False)
            ),
        ).order_by('-created_at')
    )

    # Pre-compute summary stats (avoids JS flicker on first render)
    active_count = sum(1 for t in tests if t.is_active)
    total_questions = sum(t.total_questions for t in tests)

    # Get active ingestion tasks for this user
    task_qs = IngestionTask.objects.all()
    if not request.user.is_superuser:
        task_qs = task_qs.filter(user=request.user)
    active_tasks = task_qs.filter(status__in=['pending', 'running'])[:6]
    recent_tasks = task_qs.order_by('-created_at')[:12]

    context = {
        'tests': tests,
        'is_superuser': request.user.is_superuser,
        'active_tasks': active_tasks,
        'recent_tasks': recent_tasks,
        'active_count': active_count,
        'total_questions': total_questions,
    }
    return render(request, 'exams/mentor/dashboard.html', context)


# ---------------------------------------------------------------------------
# ZIP Upload + Background Ingestion & Manual Creation
# ---------------------------------------------------------------------------

@mentor_required
def mentor_create_empty_test(request):
    """Create a new blank test and redirect to the appropriate builder."""
    if request.method == 'POST':
        test_type = request.POST.get('test_type', 'listening')
        new_test = Test.objects.create(
            name=f"Новый тест (Черновик) - {timezone.now().strftime('%Y-%m-%d %H:%M')}",
            test_type=test_type,
            author=request.user,
            is_active=False,
        )
        if test_type == 'reading':
            ReadingTest.objects.create(test=new_test)
            return redirect('exams:mentor_reading_builder', test_id=new_test.id)
        elif test_type == 'speaking':
            return redirect('exams:mentor_speaking_builder', test_id=new_test.id)
        return redirect('exams:mentor_test_builder', test_id=new_test.id)
    return redirect('exams:mentor_dashboard')


@mentor_required
def mentor_speaking_create_for_upload(request):
    """Create a speaking draft and open Speaking Builder (AI upload starts there)."""
    new_test = Test.objects.create(
        name=f"Новый Speaking тест (Черновик) - {timezone.now().strftime('%Y-%m-%d %H:%M')}",
        test_type='speaking',
        author=request.user,
        is_active=False,
    )
    return redirect('exams:mentor_speaking_builder', test_id=new_test.id)

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

        from exams.tasks import run_ingestion
        run_ingestion.delay(task.id, str(zip_path), test_name, request.user.id, split_parts)

        return redirect('exams:mentor_task_progress', task_id=task.id)

    return render(request, 'exams/mentor/upload.html')



@mentor_required
def mentor_upload_speaking(request):
    """
    GET:  Show the dedicated Speaking upload form.
    POST: Accept an image, create test, parse via Gemini, and redirect to builder Wizard.
    """
    if request.method == 'POST':
        image_file = request.FILES.get('image')
        if not image_file:
            messages.error(request, "Файл не загружен.")
            return redirect('exams:mentor_upload_speaking')

        is_image = image_file.name.lower().endswith(('.jpg', '.jpeg', '.png'))
        if not is_image:
            messages.error(request, "Пожалуйста, загрузите изображение (.jpg, .jpeg, .png).")
            return redirect('exams:mentor_upload_speaking')

        test_name = request.POST.get('test_name', '').strip()
        if not test_name:
            messages.error(request, "Пожалуйста, укажите название теста.")
            return redirect('exams:mentor_upload_speaking')

        # Reuse existing test with same name instead of crashing on unique constraint.
        existing_test = Test.objects.filter(name=test_name).first()

        if existing_test:
            if existing_test.test_type != 'speaking':
                messages.error(
                    request,
                    "Тест с таким названием уже существует в другом модуле. "
                    "Выберите другое название."
                )
                return redirect('exams:mentor_upload_speaking')
            if not can_manage_test(request.user, existing_test):
                return HttpResponseForbidden("You can only edit your own tests.")

        # Save uploaded image to temp location
        upload_dir = settings.BASE_DIR / 'media' / 'uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        safe_name = test_name.replace(' ', '_').replace('/', '_')
        _, ext = os.path.splitext(image_file.name)
        temp_image_name = f"speaking_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}{ext}"
        temp_image_path = upload_dir / temp_image_name
        
        with open(temp_image_path, 'wb') as f:
            for chunk in image_file.chunks():
                f.write(chunk)

        # Create the task record
        task = IngestionTask.objects.create(
            user=request.user,
            test_name=test_name,
            status='pending',
            stage='Загрузка изображения завершена. Запуск обработки...',
        )

        from exams.tasks import run_speaking_ingestion
        run_speaking_ingestion.delay(task.id, str(temp_image_path), test_name, request.user.id, image_file.name)

        return redirect('exams:mentor_task_progress', task_id=task.id)

    return render(request, 'exams/mentor/upload_speaking.html')


def _run_speaking_background(task_id, temp_image_path, test_name, user_id, original_filename):
    """Background thread for Speaking AI processing."""
    connection.close()
    
    from django.contrib.auth import get_user_model
    User = get_user_model()
    
    try:
        task = IngestionTask.objects.get(id=task_id)
        task.status = 'running'
        task.progress = 10
        task.stage = 'Инициализация контекста...'
        task.save(update_fields=['status', 'progress', 'stage'])
        
        user = User.objects.get(id=user_id)

        new_test = Test.objects.create(
            name=test_name,
            test_type='speaking',
            author=user,
            is_active=False,
        )

        task.result_test_id = new_test.id
        task.save(update_fields=['result_test_id'])
            
        task.update_progress(30, 'Чтение изображения...')

        with open(temp_image_path, 'rb') as f:
            raw_bytes = f.read()

        from exams.image_processing import align_perspective
        from exams.speaking_services import process_speaking_page
        
        task.update_progress(40, 'Очистка и выравнивание перспективы...')
        cleaned_bytes = align_perspective(raw_bytes)
        
        task.update_progress(60, 'Анализ заданий через AI OCR...')
        structure = process_speaking_page(cleaned_bytes)
        
        if structure.get('status') == 'error':
            raise Exception(structure.get('message', 'Failed to parse image via Gemini'))

        parts_data = structure.get('parts', [])
        if not parts_data:
            parts_data = [{'part_number': 2}]
            
        task.update_progress(80, 'Сохранение в базу данных...')
            
        new_test.speaking_parts.all().delete()
        from django.core.files.base import ContentFile

        # Default instructions per part number (used when OCR returns nothing)
        DEFAULT_INSTRUCTIONS = {
            1: "Please answer the following personal questions.",
            2: "Look at the pictures and answer the following questions.",
            3: "Look at the photograph and answer the questions below.",
            4: "Study the information below and discuss the topic.",
        }
        
        part_ids = []
        used_numbers = set()
        question_count = 0
        for idx, p in enumerate(parts_data, start=1):
            raw_part_num = p.get('part_number', idx)
            part_num = _coerce_speaking_part_number(raw_part_num, fallback_number=idx, used_numbers=used_numbers)
            used_numbers.add(part_num)
            raw_part_num_str = str(raw_part_num).strip().replace(',', '.')

            # Inject default instruction if OCR returned nothing
            if not (p.get('instructions') or '').strip():
                p['instructions'] = DEFAULT_INSTRUCTIONS.get(part_num, '')
            
            speaking_part, created = SpeakingPart.objects.get_or_create(
                test=new_test,
                part_number=part_num,
                defaults={'is_validated': False}
            )
            # save image
            speaking_part.original_image.save(original_filename, ContentFile(cleaned_bytes), save=False)
            speaking_part.validation_data = {"parts": [p]}
            speaking_part.is_validated = False
            speaking_part.save()
            part_ids.append(speaking_part.id)
            
            # Handle debate table data for Part 3
            debate_table = p.get('debate_table')
            if debate_table and isinstance(debate_table, dict):
                speaking_part.debate_data = debate_table
                # Build instructions from debate topic if none provided
                topic = debate_table.get('topic', '')
                if topic and not (p.get('instructions') or '').strip():
                    speaking_part.instructions = topic
                speaking_part.save(update_fields=['debate_data', 'instructions'])
            
            # Auto-save questions for parts that don't go through the image crop wizard.
            # Parts 1.2 and 2 need the crop wizard (they have images);
            # Parts 1/1.1, 3, 4 do NOT need cropping.
            needs_crop = (raw_part_num_str in ('1.2', '2', '2.0') or
                          (raw_part_num_str not in ('1', '1.1', '3', '4') and part_num == 2))
            if not needs_crop:
                _auto_save_speaking_questions(speaking_part, p.get('questions', []))
                speaking_part.instructions = (p.get('instructions') or speaking_part.instructions or '').strip()
                speaking_part.is_validated = True
                speaking_part.save(update_fields=['instructions', 'is_validated'])

                # Part 3 (debate): if OCR returned no questions, auto-create one from the debate topic
                if part_num == 4 and not speaking_part.questions.exists():
                    debate_topic = (speaking_part.debate_data or {}).get('topic', '').strip()
                    if not debate_topic and debate_table:
                        debate_topic = debate_table.get('topic', '').strip()
                    if debate_topic:
                        SpeakingQuestion.objects.create(
                            part=speaking_part,
                            question_number=1,
                            question_text=debate_topic,
                        )
                        question_count += 1

                # Auto-generate TTS for all questions in this validated part
                _auto_generate_tts_for_part(speaking_part)

            question_count += len(p.get('questions', []))

        task.status = 'completed'
        task.progress = 100
        task.stage = 'Готово'
        task.parts_count = len(part_ids)
        task.questions_count = question_count
        task.completed_at = timezone.now()
        task.save()
        
    except Exception as exc:
        if 'task' in locals() and task:
            task.status = 'failed'
            task.error_message = str(exc)
            task.stage = 'Ошибка обработки'
            task.completed_at = timezone.now()
            task.save()
            
    finally:
        if os.path.exists(temp_image_path):
            os.remove(temp_image_path)

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

        from exams.tasks import run_ingestion
        run_ingestion.delay(task.id, str(zip_path), test_name, request.user.id, split_parts)

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

        # --- Step 2: Pre-create Test record, then run the ingestion command ---
        task.update_progress(20, 'Запуск AI-обработки материалов...')

        from django.core.management import call_command

        # Detect primary module type for progress display
        detected_modules = [m for _, m in found_modules]
        is_reading = 'Reading' in detected_modules
        is_writing = 'Writing' in detected_modules
        has_audio = not is_reading and not is_writing

        # Determine test_type for pre-created record
        if is_reading:
            pre_test_type = 'reading'
        elif is_writing:
            pre_test_type = 'writing'
        else:
            pre_test_type = 'listening'

        # Pre-create the Test so its ID can be passed to ingest_materials.
        # This avoids a name-based lookup after the command and allows
        # multiple tests with the same name to coexist.
        test_obj = Test.objects.create(
            name=test_name,
            test_type=pre_test_type,
            is_active=True,
            author=user,
        )
        task.result_test_id = test_obj.id
        task.save(update_fields=['result_test_id'])

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
                    if has_audio:
                        pct = 20 + int((self._ocr_done / self._total_parts) * 30)
                        self.task_obj.update_progress(
                            min(pct, 50),
                            f'OCR завершён для {self._ocr_done} из ~{self._total_parts} частей...'
                        )
                    else:
                        # Reading/Writing: no audio step, so OCR done → jump to 70
                        self.task_obj.update_progress(
                            70, 'OCR завершён. Подготовка к сохранению...'
                        )
                elif '💾  Saving to database' in text:
                    self.task_obj.update_progress(
                        75 if not has_audio else 55,
                        'Сохранение в базу данных...'
                    )
                elif '📁  Part' in text:
                    self.task_obj.update_progress(
                        80 if not has_audio else 60,
                        'Запись частей и вопросов...'
                    )
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
                    self.task_obj.update_progress(85, 'Основная обработка завершена...')
                elif 'Generating AI explanations' in text:
                    self.task_obj.update_progress(88, 'Генерация объяснений ответов...')
                elif 'Explanations generated' in text:
                    self.task_obj.update_progress(92, 'Объяснения сохранены.')

        progress_stdout = ProgressCapture(task)

        try:
            call_command(
                'ingest_materials',
                test=test_name,
                test_id=test_obj.id,
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

        # --- Step 3: Update author + split parts ---
        task.update_progress(90, 'Назначение автора и постобработка...')

        try:
            test_obj.refresh_from_db()
            if not test_obj.author_id:
                test_obj.author = user
                test_obj.save(update_fields=['author'])

            if test_obj.test_type == 'writing':
                parts_count = 1  # Or logically 2 parts, but let's count tasks
                questions_count = test_obj.writing_tasks.count()
            elif test_obj.test_type == 'reading':
                ReadingTest.objects.get_or_create(test=test_obj)
                parts_count = test_obj.reading_parts.count()
                questions_count = ReadingQuestion.objects.filter(part__test=test_obj).count()
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
                name=new_test_name, test_type=test_obj.test_type, is_active=True, author=user,
                clone_of=test_obj,
            )
            for t in task_group:
                t.id = None
                t.test = new_test
                t.save()
        return
        
    if test_obj.test_type == 'reading':
        return _clone_reading_parts_to_individual_tests(test_obj, user)
        
    if test_obj.test_type == 'speaking':
        return _clone_speaking_parts_to_individual_tests(test_obj, user)

    for part in test_obj.parts.all():

        new_test_name = f"{test_obj.name} - Part {part.part_number}"
        Test.objects.filter(name=new_test_name).delete()

        new_test = Test.objects.create(
            name=new_test_name,
            test_type=test_obj.test_type,
            is_active=True,
            author=user,
            clone_of=test_obj,
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


def _clone_speaking_parts_to_individual_tests(test_obj, user):
    """Clone each SpeakingPart into its own standalone micro-test."""
    from exams.models import SpeakingPart, SpeakingQuestion
    
    for part in test_obj.speaking_parts.prefetch_related('questions').all():
        new_test_name = f"{test_obj.name} - Part {part.part_number}"
        Test.objects.filter(name=new_test_name).delete()

        new_test = Test.objects.create(
            name=new_test_name,
            test_type='speaking',
            is_active=True,
            author=user,
            clone_of=test_obj,
        )

        new_part = SpeakingPart.objects.create(
            test=new_test,
            part_number=part.part_number,
            instructions=part.instructions,
            original_image=part.original_image if part.original_image else None,
            cropped_image=part.cropped_image if hasattr(part, 'cropped_image') and part.cropped_image else None,
            alt_text=part.alt_text,
            debate_data=part.debate_data,
            validation_data=part.validation_data,
            is_validated=part.is_validated,
        )

        for q in part.questions.all():
            SpeakingQuestion.objects.create(
                part=new_part,
                question_number=q.question_number,
                question_text=q.question_text,
                audio_file=q.audio_file if q.audio_file else None,
            )


def _clone_reading_parts_to_individual_tests(test_obj, user):
    """Clone each ReadingPart into its own standalone micro-test."""
    for part in test_obj.reading_parts.select_related('passage').prefetch_related('questions').all():
        new_test_name = f"{test_obj.name} - Part {part.part_number}"
        Test.objects.filter(name=new_test_name).delete()

        new_test = Test.objects.create(
            name=new_test_name,
            test_type='reading',
            is_active=True,
            author=user,
            clone_of=test_obj,
        )
        ReadingTest.objects.get_or_create(test=new_test)

        new_part = ReadingPart.objects.create(
            test=new_test,
            part_number=part.part_number,
            instruction=part.instruction,
            question_number_start=part.question_number_start,
            question_number_end=part.question_number_end,
        )

        if hasattr(part, 'passage'):
            ReadingPassage.objects.create(
                part=new_part,
                title=part.passage.title,
                content=part.passage.content,
            )

        for q in part.questions.all():
            ReadingQuestion.objects.create(
                part=new_part,
                question_number=q.question_number,
                question_type=q.question_type,
                question_text=q.question_text,
                options=q.options,
                correct_answer=q.correct_answer,
                explanation=q.explanation,
            )


def _duplicate_test_as_draft(test_obj, user, clone_name):
    """Create a full draft duplicate of a test without publishing it."""
    new_test = Test.objects.create(
        name=clone_name,
        test_type=test_obj.test_type,
        is_active=False,
        author=user,
    )

    if test_obj.test_type == 'writing':
        for task in test_obj.writing_tasks.all().order_by('order'):
            WritingTask.objects.create(
                test=new_test,
                task_type=task.task_type,
                input_text=task.input_text,
                prompt=task.prompt,
                min_words=task.min_words,
                max_words=task.max_words,
                order=task.order,
            )
        return new_test

    if test_obj.test_type == 'reading':
        ReadingTest.objects.get_or_create(test=new_test)
        for part in test_obj.reading_parts.select_related('passage').prefetch_related('questions').all().order_by('part_number'):
            new_part = ReadingPart.objects.create(
                test=new_test,
                part_number=part.part_number,
                instruction=part.instruction,
                question_number_start=part.question_number_start,
                question_number_end=part.question_number_end,
            )
            if hasattr(part, 'passage'):
                ReadingPassage.objects.create(
                    part=new_part,
                    title=part.passage.title,
                    content=part.passage.content,
                )
            for question in part.questions.all().order_by('question_number'):
                ReadingQuestion.objects.create(
                    part=new_part,
                    question_number=question.question_number,
                    question_type=question.question_type,
                    question_text=question.question_text,
                    options=question.options,
                    correct_answer=question.correct_answer,
                    explanation=question.explanation,
                )
        return new_test

    if test_obj.test_type == 'speaking':
        for part in test_obj.speaking_parts.prefetch_related('questions').all().order_by('part_number'):
            new_part = SpeakingPart.objects.create(
                test=new_test,
                part_number=part.part_number,
                instructions=part.instructions,
                points_per_question=part.points_per_question,
                original_image=part.original_image if part.original_image else None,
                cropped_image=part.cropped_image if part.cropped_image else None,
                alt_text=part.alt_text,
                debate_data=part.debate_data,
                validation_data=part.validation_data,
                is_validated=part.is_validated,
            )
            for question in part.questions.all().order_by('question_number'):
                SpeakingQuestion.objects.create(
                    part=new_part,
                    question_number=question.question_number,
                    question_text=question.question_text,
                    audio_file=question.audio_file if question.audio_file else None,
                )
        return new_test

    for part in test_obj.parts.prefetch_related('questions__choices').all().order_by('part_number'):
        new_part = Part.objects.create(
            test=new_test,
            part_number=part.part_number,
            instructions=part.instructions,
            audio_file=part.audio_file,
            question_image=part.question_image,
            map_image=part.map_image,
            passage_title=part.passage_title,
            passage_text=part.passage_text,
            shared_choices_json=part.shared_choices_json,
            transcript=part.transcript,
            points_per_question=part.points_per_question,
        )
        for question in part.questions.all().order_by('question_number'):
            new_question = Question.objects.create(
                part=new_part,
                question_number=question.question_number,
                global_question_number=question.global_question_number,
                group_label=question.group_label,
                question_text=question.question_text,
                question_type=question.question_type,
                correct_answer=question.correct_answer,
                explanation=question.explanation,
            )
            for choice in question.choices.all().order_by('label'):
                Choice.objects.create(
                    question=new_question,
                    label=choice.label,
                    text=choice.text,
                )

    return new_test


def _cleanup_zip(zip_path):
    """Remove the temporary uploaded ZIP file."""
    try:
        if os.path.exists(zip_path):
            os.remove(zip_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Reading Module — OCR Upload
# ---------------------------------------------------------------------------

@mentor_required
def mentor_upload_reading(request):
    """
    GET:  Show the reading ZIP upload form.
    POST: Accept a ZIP archive, create IngestionTask, launch standard
          background thread (ingest_materials.py handles Reading module).
    """
    if request.method == 'POST':
        zip_file = request.FILES.get('zip_file')
        if not zip_file:
            messages.error(request, "Файл не был загружен.")
            return redirect('exams:mentor_upload_reading')

        if not zip_file.name.lower().endswith('.zip'):
            messages.error(request, "Пожалуйста, загрузите ZIP-архив с Reading материалами.")
            return redirect('exams:mentor_upload_reading')

        test_name = request.POST.get('test_name', '').strip()
        split_parts = request.POST.get('split_parts') == 'on'

        if not test_name:
            messages.error(request, "Пожалуйста, укажите название теста.")
            return redirect('exams:mentor_upload_reading')

        upload_dir = settings.BASE_DIR / 'media' / 'uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)

        zip_path = upload_dir / f"task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{zip_file.name}"
        with open(zip_path, 'wb') as output_file:
            for chunk in zip_file.chunks():
                output_file.write(chunk)

        task = IngestionTask.objects.create(
            user=request.user,
            test_name=test_name,
            split_parts=split_parts,
            status='pending',
            stage='Загрузка файла завершена. Запуск обработки...',
        )

        from exams.tasks import run_ingestion
        run_ingestion.delay(task.id, str(zip_path), test_name, request.user.id, split_parts)

        return redirect('exams:mentor_task_progress', task_id=task.id)

    return render(request, 'exams/mentor/upload_reading.html')


@mentor_required
def mentor_reading_builder(request, test_id):
    """Render the dedicated Reading Builder for a specific reading test."""
    test = get_object_or_404(Test, pk=test_id, test_type='reading')
    if test.is_deleted:
        raise Http404
    if not can_manage_test(request.user, test):
        return HttpResponseForbidden("You can only edit your own tests.")

    ReadingTest.objects.get_or_create(test=test)

    return render(request, 'exams/mentor/reading_builder.html', {
        'test': test,
        'is_superuser': request.user.is_superuser,
    })


@mentor_required
def api_reading_test_data(request, test_id):
    """Return full reading test structure as JSON."""
    test = get_object_or_404(Test, pk=test_id, test_type='reading')
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    parts = []
    for rp in test.reading_parts.order_by('part_number').prefetch_related('passage', 'questions'):
        passage = None
        if hasattr(rp, 'passage'):
            passage = {'id': rp.passage.id, 'title': rp.passage.title, 'content': rp.passage.content}

        questions = [
            {
                'id': q.id,
                'question_number': q.question_number,
                'question_type': q.question_type,
                'question_text': q.question_text,
                'options': q.options,
                'correct_answer': q.correct_answer,
                'explanation': q.explanation,
            }
            for q in rp.questions.order_by('question_number')
        ]

        parts.append({
            'id': rp.id,
            'part_number': rp.part_number,
            'instruction': rp.instruction,
            'question_number_start': rp.question_number_start,
            'question_number_end': rp.question_number_end,
            'passage': passage,
            'questions': questions,
        })

    return JsonResponse({
        'id': test.id,
        'name': test.name,
        'is_active': test.is_active,
        'parts': parts,
    })


@mentor_required
@require_POST
def api_reading_create_part(request, test_id):
    """Create a new ReadingPart."""
    test = get_object_or_404(Test, pk=test_id, test_type='reading')
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    existing_numbers = set(test.reading_parts.values_list('part_number', flat=True))
    new_number = 1
    while new_number in existing_numbers:
        new_number += 1

    rp = ReadingPart.objects.create(
        test=test,
        part_number=new_number,
        instruction='',
    )
    return JsonResponse({'id': rp.id, 'part_number': rp.part_number, 'instruction': ''})


@mentor_required
@require_http_methods(['PATCH', 'POST'])
def api_reading_update_part(request, part_id):
    """Update ReadingPart fields and its associated passage."""
    rp = get_object_or_404(ReadingPart, pk=part_id)
    if not can_manage_test(request.user, rp.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    for field in ('instruction', 'question_number_start', 'question_number_end'):
        if field in data:
            setattr(rp, field, data[field])
    rp.save()

    # Passage upsert
    if 'passage' in data:
        pd = data['passage']
        if pd is None:
            ReadingPassage.objects.filter(part=rp).delete()
        else:
            title = pd.get('title', '')
            content = pd.get('content', '')
            if content:
                ReadingPassage.objects.update_or_create(
                    part=rp,
                    defaults={'title': title, 'content': content},
                )
            else:
                ReadingPassage.objects.filter(part=rp).delete()

    return JsonResponse({'ok': True})


@mentor_required
@require_POST
def api_reading_delete_part(request, part_id):
    """Delete a ReadingPart (cascades to passage + questions)."""
    rp = get_object_or_404(ReadingPart, pk=part_id)
    if not can_manage_test(request.user, rp.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    rp.delete()
    return JsonResponse({'ok': True})


@mentor_required
@require_POST
def api_reading_create_question(request, part_id):
    """Create a new ReadingQuestion in the given part."""
    rp = get_object_or_404(ReadingPart, pk=part_id)
    if not can_manage_test(request.user, rp.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    # Auto-assign the next question number
    existing = set(rp.questions.values_list('question_number', flat=True))
    qn = (max(existing) + 1) if existing else (rp.question_number_start or 1)

    q = ReadingQuestion.objects.create(
        part=rp,
        question_number=qn,
        question_type='multiple_choice',
        question_text='',
        options=[],
        correct_answer='',
    )
    return JsonResponse({
        'id': q.id,
        'question_number': q.question_number,
        'question_type': q.question_type,
        'question_text': q.question_text,
        'options': q.options,
        'correct_answer': q.correct_answer,
        'explanation': q.explanation,
    })


@mentor_required
@require_http_methods(['PATCH', 'POST'])
def api_reading_update_question(request, question_id):
    """Update a ReadingQuestion."""
    q = get_object_or_404(ReadingQuestion, pk=question_id)
    if not can_manage_test(request.user, q.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    for field in ('question_number', 'question_type', 'question_text', 'correct_answer', 'explanation'):
        if field in data:
            setattr(q, field, data[field])

    if 'options' in data:
        opts = data['options']
        if isinstance(opts, str):
            # Accept newline-separated string from the textarea
            opts = [line.strip() for line in opts.splitlines() if line.strip()]
        q.options = opts

    q.save()
    return JsonResponse({'ok': True})


@mentor_required
@require_POST
def api_reading_delete_question(request, question_id):
    """Delete a ReadingQuestion."""
    q = get_object_or_404(ReadingQuestion, pk=question_id)
    if not can_manage_test(request.user, q.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    q.delete()
    return JsonResponse({'ok': True})

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

    test_type = None
    if task.result_test_id:
        try:
            test_type = Test.objects.values_list('test_type', flat=True).get(pk=task.result_test_id)
        except Test.DoesNotExist:
            pass
    # Fallback: infer from the task name while the background job is still running
    if not test_type:
        lower_name = (task.test_name or '').lower()
        for keyword in ('reading', 'writing', 'listening', 'speaking'):
            if keyword in lower_name:
                test_type = keyword
                break

    data = {
        'id': task.id,
        'status': task.status,
        'progress': task.progress,
        'stage': task.stage,
        'test_name': task.test_name,
        'result_test_id': task.result_test_id,
        'result_test_type': test_type,
        'has_audio': test_type not in ('reading', 'writing', 'speaking') if test_type else True,
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
    if test.is_deleted:
        raise Http404
    if not can_manage_test(request.user, test):
        return HttpResponseForbidden("You can only edit your own tests.")

    # Dedicated builders for reading and speaking test types
    if test.test_type == 'reading':
        return redirect('exams:mentor_reading_builder', test_id=test.id)
    if test.test_type == 'speaking':
        return redirect('exams:mentor_speaking_builder', test_id=test.id)

    return render(request, 'exams/mentor/test_builder.html', {
        'test': test,
        'is_superuser': request.user.is_superuser,
    })


# ---------------------------------------------------------------------------
# Delete Test
# ---------------------------------------------------------------------------

@mentor_required
def mentor_delete_test(request, test_id):
    """Confirm and delete a test, with optional cascaded deletion of cloned parts."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return HttpResponseForbidden("You can only delete your own tests.")

    cloned_tests = list(test.cloned_tests.filter(is_deleted=False))

    if request.method == 'POST':
        from django.utils import timezone as tz
        now = tz.now()
        name = test.name

        if request.POST.get('delete_clones') == '1' and cloned_tests:
            Test.objects.filter(
                pk__in=[c.pk for c in cloned_tests]
            ).update(is_deleted=True, is_active=False, deleted_at=now)

        test.is_deleted = True
        test.is_active = False
        test.deleted_at = now
        test.save(update_fields=['is_deleted', 'is_active', 'deleted_at'])
        messages.success(request, f"Тест «{name}» удалён.")
        return redirect('exams:mentor_dashboard')

    return render(request, 'exams/mentor/delete_confirm.html', {
        'test': test,
        'cloned_tests': cloned_tests,
    })


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
    for part in test.parts.prefetch_related('questions__choices').order_by('part_number'):
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
    if not is_sysmentor_or_superuser(request.user) and not test.is_active:
        return JsonResponse({'error': 'Only sysmentors can publish tests'}, status=403)
    test.is_active = not test.is_active
    test.save(update_fields=['is_active'])
    return JsonResponse({'is_active': test.is_active})


@mentor_required
@require_POST
def api_publish_test(request, test_id):
    """Set test to active, and optionally clone its parts."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if not is_sysmentor_or_superuser(request.user):
        return JsonResponse({'error': 'Only sysmentors can publish tests'}, status=403)
        
    try:
        data = json.loads(request.body)
        split_parts = data.get('split_parts', False)
        report = _build_test_validation_report(test)

        if not report['is_publishable']:
            return JsonResponse({
                'error': 'Validation failed',
                'validation': report,
            }, status=400)
        
        test.is_active = True
        test.save(update_fields=['is_active'])
        
        if split_parts:
            _clone_parts_to_individual_tests(test, request.user)
            
        return JsonResponse({'success': True, 'is_active': test.is_active, 'validation': report})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@mentor_required
def api_validate_test(request, test_id):
    """Return publish-readiness report for a test."""
    test = get_object_or_404(Test, pk=test_id)
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    return JsonResponse(_build_test_validation_report(test))


@mentor_required
def mentor_test_preview(request, test_id):
    """Render a student-facing preview using the original student templates."""
    test = get_object_or_404(Test, pk=test_id)
    if test.is_deleted:
        raise Http404
    if not can_manage_test(request.user, test):
        return HttpResponseForbidden('You can only preview your own tests.')

    preview_attempt = SimpleNamespace(id=0, started_at=timezone.now())
    base_context = {
        'attempt': preview_attempt,
        'test': test,
        'preview_mode': True,
        'hide_navbar': True,
        'draft_answers': '{}',
        'time_remaining': 3600,
        'endtime_iso': (timezone.now() + timezone.timedelta(hours=1)).isoformat(),
    }

    if test.test_type == 'reading':
        all_parts = list(
            test.reading_parts.select_related('passage').prefetch_related('questions').order_by('part_number')
        )
        if not all_parts:
            raise Http404
        try:
            req_part = int(request.GET.get('part', 1))
        except (ValueError, TypeError):
            req_part = 1
        part = next((p for p in all_parts if p.part_number == req_part), all_parts[0])
        part_index = all_parts.index(part)
        return render(request, 'exams/take_reading_test.html', {
            **base_context,
            'part': part,
            'part_number': part.part_number,
            'all_parts': all_parts,
            'next_part_number': all_parts[part_index + 1].part_number if part_index + 1 < len(all_parts) else None,
            'prev_part_number': all_parts[part_index - 1].part_number if part_index > 0 else None,
            'total_parts': len(all_parts),
            'current_part_num': part_index + 1,
        })

    if test.test_type == 'writing':
        return render(request, 'exams/take_writing_test.html', {
            **base_context,
            'tasks': list(test.writing_tasks.all().order_by('order')),
        })

    if test.test_type == 'speaking':
        parts = list(test.speaking_parts.prefetch_related('questions').order_by('part_number'))
        label_map = {1: '1.1', 2: '1.2', 3: '2', 4: '3'}
        parts_data = []
        for part in parts:
            display_label = label_map.get(part.part_number, str(part.part_number))
            raw_image_url = part.cropped_image.url if part.cropped_image else (part.original_image.url if part.original_image else '')
            parts_data.append({
                'id': part.id,
                'part_number': part.part_number,
                'display_label': display_label,
                'ocr_label': str((part.validation_data or {}).get('parts', [{}])[0].get('part_number', part.part_number)).strip().replace(',', '.'),
                'image_url': raw_image_url if display_label in {'1.2', '2'} else '',
                'debate_data': part.debate_data or {},
                'questions': [
                    {
                        'id': question.id,
                        'number': question.question_number,
                        'text': question.question_text,
                        'audio_url': question.audio_file.url if question.audio_file else '',
                    }
                    for question in part.questions.all()
                ],
            })
        return render(request, 'exams/take_speaking_test.html', {
            **base_context,
            'parts': parts,
            'parts_data': parts_data,
        })

    all_parts = list(test.parts.prefetch_related('questions__choices').order_by('part_number'))
    if not all_parts:
        raise Http404
    try:
        req_part = int(request.GET.get('part', 1))
    except (ValueError, TypeError):
        req_part = 1
    part = next((p for p in all_parts if p.part_number == req_part), all_parts[0])
    part_index = all_parts.index(part)
    return render(request, 'exams/take_test.html', {
        **base_context,
        'part': part,
        'part_number': part.part_number,
        'all_parts': all_parts,
        'next_part_number': all_parts[part_index + 1].part_number if part_index + 1 < len(all_parts) else None,
        'prev_part_number': all_parts[part_index - 1].part_number if part_index > 0 else None,
        'total_parts': len(all_parts),
        'current_part_num': part_index + 1,
    })


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


@mentor_required
def mentor_speaking_builder(request, test_id):
    """
    Render the Speaking Test Builder page with validation UI.
    """
    test = get_object_or_404(Test, pk=test_id, test_type='speaking')
    if test.is_deleted:
        raise Http404
    if not can_manage_test(request.user, test):
        return HttpResponseForbidden("You can only edit your own tests.")

    # We can pre-fetch any existing parts if the mentor is editing an existing test.
    parts = SpeakingPart.objects.filter(test=test)
    
    context = {
        'test': test,
        'parts': parts,
    }
    return render(request, 'exams/mentor/speaking_builder.html', context)

@require_POST
@mentor_required
def upload_speaking_page(request, test_id):
    """
    API endpoint for mentors to upload a speaking page image.
    Performs OCR, creates an unvalidated SpeakingPart, and returns the recognized 
    structure for the Validation Step UI.
    """
    test = get_object_or_404(Test, pk=test_id)
    if request.FILES.get('image'):
        image_file = request.FILES['image']
        raw_bytes = image_file.read()
        
        try:
            # 1. Clean and align the image
            cleaned_bytes = align_perspective(raw_bytes)
            
            # 2. Extract structure via GCP & VertexAI (returns json dict)
            structure = process_speaking_page(cleaned_bytes)
            
            # 3. Create unvalidated SpeakingParts with the original photo
            parts_data = structure.get('parts', [])
            if not parts_data:
                # Fallback if Gemini failed to parse structure cleanly
                parts_data = [{'part_number': 2}]
                
            part_ids = []
            first_part_id = None
            used_numbers = set()
            
            for idx, p in enumerate(parts_data, start=1):
                raw_part_num = p.get('part_number', idx)
                part_num = _coerce_speaking_part_number(raw_part_num, fallback_number=idx, used_numbers=used_numbers)
                used_numbers.add(part_num)
                with transaction.atomic():
                    try:
                        speaking_part, created = SpeakingPart.objects.get_or_create(
                            test=test,
                            part_number=part_num,
                            defaults={'is_validated': False}
                        )
                    except IntegrityError:
                        speaking_part = SpeakingPart.objects.get(test=test, part_number=part_num)
                        created = False
                speaking_part.original_image.save(image_file.name, ContentFile(cleaned_bytes), save=False)
                speaking_part.validation_data = {"parts": [p]} # store single part data for validation step
                speaking_part.is_validated = False
                speaking_part.save()
                
                part_ids.append(speaking_part.id)
                if not first_part_id:
                    first_part_id = speaking_part.id
            
            return JsonResponse({
                'status': 'success', 
                'part_id': first_part_id, # backwards compat
                'part_ids': part_ids, # list of all created part rows
                'data': structure,
                'image_url': speaking_part.original_image.url if speaking_part.original_image else ''
            })
        
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    
    return JsonResponse({'status': 'error', 'message': 'No image provided'}, status=400)

@require_POST
@mentor_required
def save_speaking_validation(request, test_id):
    """
    Called when mentor clicks 'Confirm & Save' from the UI.
    It takes the validated data/crop boundaries, crops the image, saves questions,
    and marks the test/part as validated.
    """
    test = get_object_or_404(Test, pk=test_id)
    try:
        payload = json.loads(request.body)
        part_id = payload.get('part_id')
        validated_data = payload.get('validated_data') or {}
        
        part = get_object_or_404(SpeakingPart, id=part_id, test=test)
        
        if 'bounding_box' in validated_data:
            # Re-crop the image based on validated bounding boxes
            with part.original_image.open('rb') as f:
                img_bytes = f.read()
            cropped_bytes = crop_image(img_bytes, validated_data['bounding_box'])
            part.cropped_image.save(f"crop_{part.id}.jpg", ContentFile(cropped_bytes), save=False)
            
            # Generate Alt-Text
            part.alt_text = generate_alt_text(cropped_bytes)
        
        # Handle debate table data (Part 3)
        debate_table = validated_data.get('debate_table')
        if debate_table and isinstance(debate_table, dict):
            part.debate_data = debate_table

        part.instructions = (validated_data.get('instructions') or part.instructions or '').strip()
        part.is_validated = True
        part.save()
        
        # Recreate questions based on validated data
        raw_questions = validated_data.get('questions') or []
        normalized_questions = []
        used_numbers = set()

        for idx, q in enumerate(raw_questions, start=1):
            if not isinstance(q, dict):
                continue

            q_text = str(q.get('text') or '').strip()
            if not q_text:
                continue

            raw_num = q.get('q_num', idx)
            try:
                q_num = int(raw_num)
            except (TypeError, ValueError):
                q_num = idx

            if q_num <= 0:
                q_num = idx
            while q_num in used_numbers:
                q_num += 1

            used_numbers.add(q_num)
            normalized_questions.append((q_num, q_text))

        part.questions.all().delete()
        for q_num, q_text in sorted(normalized_questions, key=lambda item: item[0]):
            SpeakingQuestion.objects.create(
                part=part,
                question_number=q_num,
                question_text=q_text,
            )

        # Auto-generate TTS for the freshly validated questions
        _auto_generate_tts_for_part(part)

        test.is_active = True
        test.save()
            
        return JsonResponse({'status': 'success'})
        
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


# ---------------------------------------------------------------------------
# Speaking Module — CRUD JSON API
# ---------------------------------------------------------------------------

@require_http_methods(['GET'])
@mentor_required
def api_speaking_test_data(request, test_id):
    """Return full speaking test structure as JSON."""
    test = get_object_or_404(Test, pk=test_id, test_type='speaking')
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    parts_data = []
    for part in test.speaking_parts.prefetch_related('questions').order_by('part_number'):
        questions = []
        for q in part.questions.all().order_by('question_number'):
            questions.append({
                'id': q.id,
                'question_number': q.question_number,
                'question_text': q.question_text,
                'has_audio': bool(q.audio_file),
                'audio_url': q.audio_file.url if q.audio_file else None,
            })
        parts_data.append({
            'id': part.id,
            'part_number': part.part_number,
            'instructions': part.instructions,
            'is_validated': part.is_validated,
            'validation_data': part.validation_data,
            'debate_data': part.debate_data or {},
            'original_image_url': part.original_image.url if part.original_image else None,
            'image_url': (part.cropped_image.url if part.cropped_image
                          else (part.original_image.url if part.original_image else None)),
            'questions': questions,
        })

    return JsonResponse({
        'id': test.id,
        'name': test.name,
        'is_active': test.is_active,
        'parts': parts_data,
    })


@require_POST
@mentor_required
def api_speaking_update_test(request, test_id):
    """Rename a speaking test."""
    test = get_object_or_404(Test, pk=test_id, test_type='speaking')
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    name = body.get('name', '').strip()
    if name:
        test.name = name
        test.save(update_fields=['name'])
    return JsonResponse({'success': True, 'name': test.name})


@require_POST
@mentor_required
def api_speaking_toggle_active(request, test_id):
    """Toggle is_active flag for a speaking test.

    Activating is guarded by the full validation report — the test cannot be
    published while any blockers exist (e.g. unvalidated parts, missing images).
    Deactivating (unpublishing) is always allowed.
    """
    test = get_object_or_404(Test, pk=test_id, test_type='speaking')
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    if not is_sysmentor_or_superuser(request.user) and not test.is_active:
        return JsonResponse({'error': 'Only sysmentors can publish tests'}, status=403)

    # Only validate when attempting to activate (publish)
    if not test.is_active:
        report = _build_test_validation_report(test)
        if not report['is_publishable']:
            return JsonResponse({
                'error': 'Тест нельзя опубликовать: есть блокирующие проблемы.',
                'blockers': report['blockers'],
                'is_publishable': False,
            }, status=400)

    test.is_active = not test.is_active
    test.save(update_fields=['is_active'])
    return JsonResponse({'is_active': test.is_active})


@require_POST
@mentor_required
def api_speaking_create_part(request, test_id):
    """Create a new empty SpeakingPart."""
    test = get_object_or_404(Test, pk=test_id, test_type='speaking')
    if not can_manage_test(request.user, test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    with transaction.atomic():
        existing_nums = set(
            test.speaking_parts.select_for_update().values_list('part_number', flat=True)
        )
        next_num = 1
        while next_num in existing_nums:
            next_num += 1
        if next_num > 4:
            return JsonResponse({'error': 'Максимум 4 части'}, status=400)

        try:
            part = SpeakingPart.objects.create(
                test=test,
                part_number=next_num,
                instructions='',
                is_validated=True,
            )
        except IntegrityError:
            return JsonResponse({'error': 'Конфликт номера части, попробуйте ещё раз'}, status=409)
    return JsonResponse({
        'id': part.id,
        'part_number': part.part_number,
        'instructions': '',
        'is_validated': True,
        'image_url': None,
        'questions': [],
    })


@require_POST
@mentor_required
def api_speaking_update_part(request, part_id):
    """Update instructions for a SpeakingPart."""
    part = get_object_or_404(SpeakingPart, pk=part_id)
    if not can_manage_test(request.user, part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    if 'instructions' in body:
        part.instructions = body['instructions']
    part.save()
    return JsonResponse({'success': True})


@require_POST
@mentor_required
def api_speaking_delete_part(request, part_id):
    """Delete a SpeakingPart and all its questions."""
    part = get_object_or_404(SpeakingPart, pk=part_id)
    if not can_manage_test(request.user, part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    part.delete()
    return JsonResponse({'success': True})


@require_POST
@mentor_required
def api_speaking_create_question(request, part_id):
    """Create a new empty SpeakingQuestion."""
    part = get_object_or_404(SpeakingPart, pk=part_id)
    if not can_manage_test(request.user, part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    existing_nums = set(part.questions.values_list('question_number', flat=True))
    next_num = 1
    while next_num in existing_nums:
        next_num += 1

    question = SpeakingQuestion.objects.create(
        part=part,
        question_number=next_num,
        question_text='',
    )
    return JsonResponse({
        'id': question.id,
        'question_number': question.question_number,
        'question_text': '',
        'has_audio': False,
        'audio_url': None,
    })


@require_POST
@mentor_required
def api_speaking_update_question(request, question_id):
    """Update text for a SpeakingQuestion."""
    question = get_object_or_404(SpeakingQuestion, pk=question_id)
    if not can_manage_test(request.user, question.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    if 'question_text' in body:
        question.question_text = body['question_text']
    question.save()
    return JsonResponse({'success': True})


@require_POST
@mentor_required
def api_speaking_delete_question(request, question_id):
    """Delete a SpeakingQuestion."""
    question = get_object_or_404(SpeakingQuestion, pk=question_id)
    if not can_manage_test(request.user, question.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)
    question.delete()
    return JsonResponse({'success': True})


@require_POST
@mentor_required
def api_speaking_generate_question_tts(request, question_id):
    """Generate TTS audio for a SpeakingQuestion and save it to the model."""
    question = get_object_or_404(SpeakingQuestion, pk=question_id)
    if not can_manage_test(request.user, question.part.test):
        return JsonResponse({'error': 'Forbidden'}, status=403)

    # Build narration text — for Part 3 include the full debate table
    text = question.question_text.strip()
    if question.part.part_number == 4:
        debate_data = question.part.debate_data or {}
        if debate_data.get('topic'):
            for_points = '. '.join(debate_data.get('for_points') or [])
            against_points = '. '.join(debate_data.get('against_points') or [])
            text = f"Discussion topic: {debate_data['topic']}."
            if for_points:
                text += f" Arguments for: {for_points}."
            if against_points:
                text += f" Arguments against: {against_points}."

    if not text:
        return JsonResponse({'error': 'Question text is empty'}, status=400)

    from .tts_service import generate_tts_base64
    audio_b64 = generate_tts_base64(text, language_code='en-US')
    if not audio_b64:
        return JsonResponse({'error': 'TTS generation failed — check Google Cloud credentials'}, status=500)

    from django.core.files.base import ContentFile
    audio_bytes = base64.b64decode(audio_b64)
    filename = f"q_{question_id}.mp3"
    question.audio_file.save(filename, ContentFile(audio_bytes), save=True)

    return JsonResponse({'success': True, 'audio_url': question.audio_file.url})


# ---------------------------------------------------------------------------
# User Management (admin-only)
# ---------------------------------------------------------------------------

@admin_required
def mentor_users_list(request):
    """List all users with their roles. Admin-only."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    User = get_user_model()
    Group.objects.get_or_create(name='Mentors')

    users = (
        User.objects
        .prefetch_related('groups')
        .select_related('profile')
        .order_by('-date_joined')
    )

    user_rows = []
    for u in users:
        group_names = {g.name for g in u.groups.all()}
        if u.is_superuser:
            role = 'superuser'
        elif 'Mentors' in group_names:
            role = 'mentor'
        else:
            role = 'student'
        profile = getattr(u, 'profile', None)
        user_rows.append({
            'obj': u,
            'role': role,
            'is_self': u.pk == request.user.pk,
            'avatar_icon': profile.avatar_icon if profile else 'ph-user',
            'avatar_gradient': profile.avatar_gradient if profile else 'linear-gradient(135deg, #58CC02, #46A302)',
            'custom_avatar': profile.custom_avatar if profile else None,
        })

    return render(request, 'exams/mentor/users.html', {
        'user_rows': user_rows,
        'page_group': 'users',
        'title': 'Управление пользователями',
        'subtitle': 'Назначайте роли и управляйте доступом пользователей к платформе.',
        'icon': 'ph-fill ph-users-three',
        'accent': 'blue',
        'is_admin': True,
    })


@require_POST
@admin_required
def api_user_set_role(request, user_id):
    """Set a user's role (mentor / student). Admin-only."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    User = get_user_model()
    target = get_object_or_404(User, pk=user_id)

    if target.is_superuser:
        return JsonResponse({'error': 'Cannot change superuser role'}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    role = body.get('role', 'student')
    if role not in ('student', 'mentor'):
        return JsonResponse({'error': 'Invalid role'}, status=400)

    mentors_group, _ = Group.objects.get_or_create(name='Mentors')

    target.groups.remove(mentors_group)
    if role == 'mentor':
        target.groups.add(mentors_group)

    return JsonResponse({'success': True, 'role': role})


@require_POST
@admin_required
def api_user_delete(request, user_id):
    """Delete a user. Admin-only. Cannot delete self or superusers."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    target = get_object_or_404(User, pk=user_id)

    if target.pk == request.user.pk:
        return JsonResponse({'error': 'Cannot delete yourself'}, status=403)
    if target.is_superuser:
        return JsonResponse({'error': 'Cannot delete superuser'}, status=403)

    target.delete()
    return JsonResponse({'success': True})


# ---------------------------------------------------------------------------
# Mentor: YouTube Video Lessons (public — visible to all students)
# ---------------------------------------------------------------------------

@sysmentor_required
def mentor_video_lessons(request):
    """
    GET:  List all public video lessons managed by mentors.
    POST: Upload a new YouTube video lesson (is_public=True, visible to all).
    Accessible only to sysmentors and superusers.
    """
    from .models import VideoLesson

    if request.method == 'POST':
        youtube_url = request.POST.get('youtube_url', '').strip()
        title = request.POST.get('title', '').strip()

        if not youtube_url:
            messages.error(request, "Please provide a YouTube URL.")
            return redirect('exams:mentor_video_lessons')

        try:
            from .video_services import create_video_lesson
            lesson = create_video_lesson(
                youtube_url=youtube_url,
                user=request.user,
                title=title,
                is_public=True,  # mentor-uploaded: visible to all students
            )
            messages.success(
                request,
                f"Video lesson \"{lesson.title}\" created with {lesson.quiz_count} quiz questions!"
            )
            return redirect('exams:mentor_video_lessons')
        except ValueError as exc:
            messages.error(request, f"Invalid URL: {exc}")
        except RuntimeError as exc:
            messages.error(request, f"Error: {exc}")
        except Exception as exc:
            messages.error(request, f"Unexpected error: {exc}")

        return redirect('exams:mentor_video_lessons')

    # GET — list all public lessons
    lessons = VideoLesson.objects.filter(is_public=True).order_by('-created_at')
    context = {
        'lessons': lessons,
        'cefr_levels': VideoLesson.CEFR_LEVELS,
    }
    return render(request, 'exams/mentor_video_lessons.html', context)


@sysmentor_required
def mentor_video_lesson_delete(request, lesson_id):
    """Delete a public video lesson. Accessible only to sysmentors/superusers."""
    from .models import VideoLesson

    if not is_sysmentor_or_superuser(request.user):
        return HttpResponseForbidden("Only sysmentors can access this page.")

    lesson = get_object_or_404(VideoLesson, pk=lesson_id, is_public=True)
    lesson.delete()
    messages.success(request, "Video lesson deleted.")
    return redirect('exams:mentor_video_lessons')


# ---------------------------------------------------------------------------
# Classroom Management (Mentor)
# ---------------------------------------------------------------------------

@mentor_required
def classroom_list(request):
    """List all classrooms created by this mentor."""
    classrooms = request.user.mentored_classrooms.prefetch_related(
        'memberships__student', 'tests'
    ).all()
    context = {
        'classrooms': classrooms,
        'page_group': 'classrooms',
    }
    return render(request, 'exams/mentor/classrooms.html', context)


@mentor_required
def classroom_create(request):
    """Create a new classroom."""
    if request.method == 'POST':
        from .models import Classroom, CLASSROOM_COLORS
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        emoji = request.POST.get('emoji', '🎓').strip() or '🎓'
        color = request.POST.get('color', 'blue').strip()
        valid_colors = [c[0] for c in CLASSROOM_COLORS]
        if color not in valid_colors:
            color = 'blue'
        if not name:
            messages.error(request, 'Введите название класса.')
            return redirect('exams:classroom_list')
        classroom = Classroom.objects.create(
            name=name,
            description=description,
            emoji=emoji,
            color=color,
            mentor=request.user,
        )
        messages.success(request, f'Класс «{classroom.name}» создан! Код: {classroom.join_code}')
        return redirect('exams:classroom_detail', classroom_id=classroom.id)
    return redirect('exams:classroom_list')


@mentor_required
def classroom_detail(request, classroom_id):
    """Show classroom details: students, tests, join code, progress matrix, announcements."""
    from .models import Classroom, ClassroomAnnouncement
    classroom = get_object_or_404(
        Classroom.objects.prefetch_related('memberships__student', 'tests', 'video_lessons'),
        pk=classroom_id,
        mentor=request.user,
    )

    # Get tests owned by this mentor that can be added
    mentor_tests = _get_manageable_tests_for_user(request.user).exclude(
        pk__in=classroom.tests.values_list('pk', flat=True)
    )

    from .models import VideoLesson
    mentor_video_lessons = VideoLesson.objects.filter(created_by=request.user, is_public=False).exclude(
        pk__in=classroom.video_lessons.values_list('pk', flat=True)
    )

    # Student results: latest attempts for classroom tests
    student_ids = list(classroom.memberships.values_list('student_id', flat=True))
    from .models import UserAttempt
    recent_attempts = (
        UserAttempt.objects
        .filter(user_id__in=student_ids, test__in=classroom.tests.all(), completed_at__isnull=False)
        .select_related('user', 'test')
        .order_by('-completed_at')[:50]
    )

    # Build progress matrix: per-student × per-test latest completed attempt
    classroom_tests = list(classroom.tests.all().order_by('name'))
    students = [m.student for m in classroom.memberships.select_related('student')]

    all_attempts = (
        UserAttempt.objects
        .filter(user_id__in=student_ids, test__in=classroom_tests, completed_at__isnull=False)
        .values('user_id', 'test_id', 'id', 'total_score', 'max_possible_score', 'completed_at')
        .order_by('user_id', 'test_id', '-completed_at')
    )
    # Keep only latest per (user, test)
    latest = {}
    for a in all_attempts:
        key = (a['user_id'], a['test_id'])
        if key not in latest:
            latest[key] = a

    students_progress = []
    total_scores, attempt_count = [], 0
    completed_pairs, total_pairs = 0, len(students) * len(classroom_tests)
    for student in students:
        test_results = {}
        for test in classroom_tests:
            a = latest.get((student.pk, test.pk))
            if a:
                attempt_count += 1
                completed_pairs += 1
                max_s = a['max_possible_score'] or 1
                pct = round(a['total_score'] / max_s * 100)
                total_scores.append(pct)
                status = 'passed' if pct >= 60 else 'failed'
                test_results[test.pk] = {
                    'status': status,
                    'score': pct,
                    'attempt_id': a['id'],
                }
            else:
                test_results[test.pk] = {'status': 'not_started', 'score': None, 'attempt_id': None}
        students_progress.append({'student': student, 'test_results': test_results})

    avg_score = round(sum(total_scores) / len(total_scores)) if total_scores else 0
    completion_rate = round(completed_pairs / total_pairs * 100) if total_pairs else 0

    stats = {
        'student_count': len(students),
        'test_count': len(classroom_tests),
        'avg_score': avg_score,
        'completion_rate': completion_rate,
    }

    announcements = classroom.announcements.select_related('author').order_by('-is_pinned', '-created_at')

    context = {
        'classroom': classroom,
        'mentor_tests': mentor_tests,
        'mentor_video_lessons': mentor_video_lessons,
        'recent_attempts': recent_attempts,
        'classroom_tests': classroom_tests,
        'students_progress': students_progress,
        'stats': stats,
        'announcements': announcements,
        'page_group': 'classrooms',
    }
    return render(request, 'exams/mentor/classroom_detail.html', context)


@mentor_required
@require_POST
def api_classroom_add_test(request, classroom_id):
    """Add a test to a classroom (legacy UI, creates assignment without deadline)."""
    from .models import Classroom, Test, ClassroomAssignment, Notification
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    test_id = request.POST.get('test_id')
    if test_id:
        test = get_object_or_404(Test, pk=test_id)
        if can_manage_test(request.user, test):
            classroom.tests.add(test)
            if not test.is_class_only:
                test.is_class_only = True
                test.save(update_fields=['is_class_only'])
            
            # Create assignment record if it doesn't exist
            assignment, created = ClassroomAssignment.objects.get_or_create(
                classroom=classroom,
                test=test,
                defaults={'title': f"Тест: {test.name}"}
            )
            
            if created:
                # Notify students
                from django.urls import reverse
                students = [m.student for m in classroom.memberships.select_related('student')]
                url = reverse('exams:my_classroom') + f"?classroom={classroom.id}"
                Notification.objects.bulk_create([
                    Notification(
                        user=s,
                        notification_type='assignment',
                        title=f"Новый тест в классе {classroom.name}: {test.name}",
                        url=url
                    ) for s in students
                ])

            messages.success(request, f'Тест «{test.name}» назначен классу.')
    return redirect('exams:classroom_detail', classroom_id=classroom.id)


@mentor_required
@require_POST
def api_classroom_remove_test(request, classroom_id):
    """Remove a test from a classroom."""
    from .models import Classroom, Test, ClassroomAssignment
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    test_id = request.POST.get('test_id')
    if test_id:
        test = get_object_or_404(Test, pk=test_id)
        classroom.tests.remove(test)
        # Remove assignment
        ClassroomAssignment.objects.filter(classroom=classroom, test=test).delete()
        # Reset is_class_only if test is no longer in ANY classroom
        if test.is_class_only and not test.classrooms.exists():
            test.is_class_only = False
            test.save(update_fields=['is_class_only'])
        messages.success(request, 'Тест удалён из класса.')
    return redirect('exams:classroom_detail', classroom_id=classroom.id)


@mentor_required
@require_POST
def api_classroom_add_video_lesson(request, classroom_id):
    """Add a video lesson to a classroom."""
    from .models import Classroom, VideoLesson
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    lesson_id = request.POST.get('lesson_id')
    if lesson_id:
        lesson = get_object_or_404(VideoLesson, pk=lesson_id, created_by=request.user, is_public=False)
        classroom.video_lessons.add(lesson)
        messages.success(request, f'Видеоурок «{lesson.title}» добавлен в класс.')
    return redirect('exams:classroom_detail', classroom_id=classroom.id)


@mentor_required
@require_POST
def api_classroom_remove_video_lesson(request, classroom_id):
    """Remove a video lesson from a classroom."""
    from .models import Classroom
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    lesson_id = request.POST.get('lesson_id')
    if lesson_id:
        classroom.video_lessons.remove(lesson_id)
        messages.success(request, 'Видеоурок удалён из класса.')
    return redirect('exams:classroom_detail', classroom_id=classroom.id)


@mentor_required
@require_POST
def api_classroom_remove_student(request, classroom_id):
    """Remove a student from a classroom."""
    from .models import Classroom, ClassroomMembership
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    student_id = request.POST.get('student_id')
    if student_id:
        ClassroomMembership.objects.filter(classroom=classroom, student_id=student_id).delete()
        messages.success(request, 'Ученик удалён из класса.')
    return redirect('exams:classroom_detail', classroom_id=classroom.id)


@mentor_required
@require_POST
def api_classroom_regenerate_code(request, classroom_id):
    """Regenerate the join code for a classroom."""
    from .models import Classroom, _generate_join_code
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    classroom.join_code = _generate_join_code()
    classroom.save(update_fields=['join_code'])
    messages.success(request, f'Новый код: {classroom.join_code}')
    return redirect('exams:classroom_detail', classroom_id=classroom.id)


@mentor_required
@require_POST
def api_classroom_delete(request, classroom_id):
    """Delete a classroom."""
    from .models import Classroom
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    name = classroom.name
    classroom.delete()
    messages.success(request, f'Класс «{name}» удалён.')
    return redirect('exams:classroom_list')


@mentor_required
def api_classroom_toggle_active(request, classroom_id):
    """Toggle classroom active/inactive state."""
    from .models import Classroom
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    classroom.is_active = not classroom.is_active
    classroom.save(update_fields=['is_active'])
    state = 'активирован' if classroom.is_active else 'деактивирован'
    messages.success(request, f'Класс «{classroom.name}» {state}.')
    return redirect('exams:classroom_detail', classroom_id=classroom_id)


@mentor_required
def api_classroom_edit(request, classroom_id):
    """Edit classroom name, description, emoji and color."""
    from .models import Classroom
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    name = request.POST.get('name', '').strip()
    description = request.POST.get('description', '').strip()
    emoji = request.POST.get('emoji', '').strip()
    color = request.POST.get('color', 'blue').strip()
    if name:
        classroom.name = name
    classroom.description = description
    if emoji:
        classroom.emoji = emoji
    from .models import CLASSROOM_COLORS
    valid_colors = [c[0] for c in CLASSROOM_COLORS]
    if color in valid_colors:
        classroom.color = color
    classroom.save(update_fields=['name', 'description', 'emoji', 'color'])
    messages.success(request, f'Класс «{classroom.name}» обновлён.')
    return redirect('exams:classroom_detail', classroom_id=classroom_id)


@mentor_required
def api_classroom_announcement_create(request, classroom_id):
    """Create an announcement in a classroom."""
    from .models import Classroom, ClassroomAnnouncement
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    title = request.POST.get('title', '').strip()
    body = request.POST.get('body', '').strip()
    is_pinned = request.POST.get('is_pinned') == '1'
    if not title:
        messages.error(request, 'Заголовок объявления не может быть пустым.')
        return redirect('exams:classroom_detail', classroom_id=classroom_id)
    ClassroomAnnouncement.objects.create(
        classroom=classroom,
        author=request.user,
        title=title,
        body=body,
        is_pinned=is_pinned,
    )
    
    # Notify students
    from .models import Notification
    from django.urls import reverse
    students = [m.student for m in classroom.memberships.select_related('student')]
    url = reverse('exams:my_classroom') + f"?classroom={classroom.id}"
    Notification.objects.bulk_create([
        Notification(
            user=s,
            notification_type='announcement',
            title=f"Новое объявление в классе {classroom.name}",
            body=title,
            url=url
        ) for s in students
    ])
    
    messages.success(request, 'Объявление опубликовано.')
    return redirect('exams:classroom_detail', classroom_id=classroom_id)

@mentor_required
@require_POST
def api_classroom_add_assignment(request, classroom_id):
    """Assign a test or video lesson with a deadline."""
    from .models import Classroom, Test, VideoLesson, ClassroomAssignment, Notification
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    
    target_type = request.POST.get('target_type')  # 'test' or 'video'
    target_id = request.POST.get('target_id')
    title = request.POST.get('title', '').strip()
    instructions = request.POST.get('instructions', '').strip()
    due_date_str = request.POST.get('due_date')
    
    if not target_id or target_type not in ['test', 'video']:
        messages.error(request, 'Некорректные параметры задания.')
        return redirect('exams:classroom_detail', classroom_id=classroom.id)
        
    due_date = None
    if due_date_str:
        from django.utils.dateparse import parse_datetime
        from django.utils.timezone import make_aware, get_current_timezone, is_naive
        parsed = parse_datetime(due_date_str)
        if parsed:
            due_date = make_aware(parsed, get_current_timezone()) if is_naive(parsed) else parsed

    test = None
    video_lesson = None
    target_name = ''
    
    if target_type == 'test':
        test = get_object_or_404(Test, pk=target_id)
        if not can_manage_test(request.user, test):
            return redirect('exams:classroom_detail', classroom_id=classroom.id)
        classroom.tests.add(test)
        if not test.is_class_only:
            test.is_class_only = True
            test.save(update_fields=['is_class_only'])
        target_name = test.name
    else:
        video_lesson = get_object_or_404(VideoLesson, pk=target_id)
        if video_lesson.author != request.user and not request.user.profile.role == 'sysmentor':
            return redirect('exams:classroom_detail', classroom_id=classroom.id)
        classroom.video_lessons.add(video_lesson)
        target_name = video_lesson.title

    if not title:
        title = f"Задание: {target_name}"

    assignment, created = ClassroomAssignment.objects.update_or_create(
        classroom=classroom,
        test=test,
        video_lesson=video_lesson,
        defaults={
            'title': title,
            'instructions': instructions,
            'due_date': due_date,
        }
    )

    if created:
        from django.urls import reverse
        students = [m.student for m in classroom.memberships.select_related('student')]
        url = reverse('exams:my_classroom') + f"?classroom={classroom.id}"
        Notification.objects.bulk_create([
            Notification(
                user=s,
                notification_type='assignment',
                title=f"Новое задание: {title}",
                url=url
            ) for s in students
        ])

    messages.success(request, f'Задание «{title}» успешно добавлено.')
    return redirect('exams:classroom_detail', classroom_id=classroom.id)


@mentor_required
def api_classroom_announcement_delete(request, classroom_id, announcement_id):
    """Delete an announcement."""
    from .models import Classroom, ClassroomAnnouncement
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    announcement = get_object_or_404(ClassroomAnnouncement, pk=announcement_id, classroom=classroom)
    announcement.delete()
    messages.success(request, 'Объявление удалено.')
    return redirect('exams:classroom_detail', classroom_id=classroom_id)


@mentor_required
def api_classroom_announcement_pin(request, classroom_id, announcement_id):
    """Toggle pin state of an announcement."""
    from .models import Classroom, ClassroomAnnouncement
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    classroom = get_object_or_404(Classroom, pk=classroom_id, mentor=request.user)
    announcement = get_object_or_404(ClassroomAnnouncement, pk=announcement_id, classroom=classroom)
    announcement.is_pinned = not announcement.is_pinned
    announcement.save(update_fields=['is_pinned'])
    state = 'закреплено' if announcement.is_pinned else 'откреплено'
    messages.success(request, f'Объявление {state}.')
    return redirect('exams:classroom_detail', classroom_id=classroom_id)


# ---------------------------------------------------------------------------
# Mentor: Manual Grading of Writing & Speaking Submissions
# ---------------------------------------------------------------------------

@mentor_required
def mentor_grade_submissions(request, attempt_id):
    """
    Mentor view for manually grading a student's Writing or Speaking attempt.
    Access is granted if the mentor authored the test or it belongs to one of
    their classrooms.
    """
    from .models import UserAttempt, Classroom

    attempt = get_object_or_404(
        UserAttempt.objects.select_related('user', 'test'),
        pk=attempt_id,
        test__test_type__in=['writing', 'speaking'],
    )

    # Permission: mentor must own or manage this test
    if not can_manage_test(request.user, attempt.test):
        # Also allow if the attempt belongs to a student in one of this mentor's classrooms
        in_classroom = Classroom.objects.filter(
            mentor=request.user,
            memberships__student=attempt.user,
            tests=attempt.test,
        ).exists()
        if not in_classroom:
            return JsonResponse({'error': 'Forbidden'}, status=403)

    test_type = attempt.test.test_type

    if test_type == 'writing':
        submissions = list(
            attempt.writing_submissions
            .select_related('task')
            .order_by('task__order')
        )
    else:  # speaking
        submissions = list(
            attempt.speaking_submissions
            .select_related('question__part')
            .order_by('question__part__part_number', 'question__question_number')
        )

    return render(request, 'exams/mentor/grade_submissions.html', {
        'attempt': attempt,
        'test_type': test_type,
        'submissions': submissions,
        'page_group': 'classrooms',
    })


@mentor_required
@require_POST
def api_mentor_grade_writing(request, submission_id):
    """Save mentor score + feedback for a WritingSubmission."""
    from .models import WritingSubmission, Classroom

    sub = get_object_or_404(WritingSubmission.objects.select_related('attempt__test', 'attempt__user'), pk=submission_id)

    if not can_manage_test(request.user, sub.attempt.test):
        in_classroom = Classroom.objects.filter(
            mentor=request.user,
            memberships__student=sub.attempt.user,
            tests=sub.attempt.test,
        ).exists()
        if not in_classroom:
            return JsonResponse({'error': 'Forbidden'}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    score = body.get('mentor_score')
    feedback = body.get('mentor_feedback', '').strip()

    if score is not None:
        try:
            score = float(score)
            if not (0 <= score <= 100):
                return JsonResponse({'error': 'Score must be 0–100'}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({'error': 'Invalid score'}, status=400)

    sub.mentor_score = score
    sub.mentor_feedback = feedback
    sub.save(update_fields=['mentor_score', 'mentor_feedback'])
    return JsonResponse({'success': True, 'mentor_score': sub.mentor_score, 'mentor_feedback': sub.mentor_feedback})


@mentor_required
@require_POST
def api_mentor_grade_speaking(request, submission_id):
    """Save mentor score + feedback for a SpeakingSubmission."""
    from .models import SpeakingSubmission, Classroom

    sub = get_object_or_404(SpeakingSubmission.objects.select_related('attempt__test', 'attempt__user', 'question__part'), pk=submission_id)

    if not can_manage_test(request.user, sub.attempt.test):
        in_classroom = Classroom.objects.filter(
            mentor=request.user,
            memberships__student=sub.attempt.user,
            tests=sub.attempt.test,
        ).exists()
        if not in_classroom:
            return JsonResponse({'error': 'Forbidden'}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    score = body.get('mentor_score')
    feedback = body.get('mentor_feedback', '').strip()

    if score is not None:
        try:
            score = float(score)
            if not (0 <= score <= 10):
                return JsonResponse({'error': 'Score must be 0–10'}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({'error': 'Invalid score'}, status=400)

    sub.mentor_score = score
    sub.mentor_feedback = feedback
    sub.save(update_fields=['mentor_score', 'mentor_feedback'])
    return JsonResponse({'success': True, 'mentor_score': sub.mentor_score, 'mentor_feedback': sub.mentor_feedback})

