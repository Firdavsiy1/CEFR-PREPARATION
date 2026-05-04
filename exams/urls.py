"""URL configuration for the exams app."""

from django.urls import path

from . import views
from . import mentor_views

app_name = 'exams'

urlpatterns = [
    # Public pages
    path('', views.landing_page_view, name='landing_page'),

    # Student-facing views
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('dashboard/<str:category>/', views.dashboard_view, name='dashboard_category'),
    path('test/<int:test_id>/tutorial/', views.test_tutorial_view, name='test_tutorial'),
    path('test/<int:test_id>/start/', views.start_test_view, name='start_test'),
    path('attempt/<int:attempt_id>/part/<int:part_number>/', views.take_test_part_view, name='take_test_part'),
    path('take-writing-test/<int:attempt_id>/', views.take_writing_test_view, name='take_writing_test'),
    path('take-reading-test/<int:attempt_id>/', views.take_reading_test_view, name='take_reading_test'),
    path('take-reading-test/<int:attempt_id>/part/<int:part_number>/', views.take_reading_part_view, name='take_reading_part'),
    path('history/', views.exam_history_view, name='history'),
    path('history/<str:category>/', views.exam_history_view, name='history_category'),
    path('result/<int:attempt_id>/', views.test_result_view, name='test_result'),

    # Student APIs — auto-save & anti-cheat
    path('api/attempt/<int:attempt_id>/autosave/', views.api_autosave, name='api_autosave'),
    path('api/attempt/<int:attempt_id>/tab-blur/', views.api_log_tab_blur, name='api_log_tab_blur'),
    path('api/result/<int:attempt_id>/speaking-status/', views.api_speaking_result_status, name='api_speaking_result_status'),

    # -----------------------------------------------------------------------
    # Mentor Panel
    # -----------------------------------------------------------------------
    path('mentor/', mentor_views.mentor_dashboard, name='mentor_dashboard'),
    path('mentor/upload/', mentor_views.mentor_upload, name='mentor_upload'),
    path('mentor/upload-writing/', mentor_views.mentor_upload_writing, name='mentor_upload_writing'),
    path('mentor/upload-reading/', mentor_views.mentor_upload_reading, name='mentor_upload_reading'),
    path('mentor/upload-speaking/', mentor_views.mentor_upload_speaking, name='mentor_upload_speaking'),
    path('mentor/speaking-test/new/', mentor_views.mentor_speaking_create_for_upload, name='mentor_speaking_create_for_upload'),
    path('mentor/test/create/', mentor_views.mentor_create_empty_test, name='mentor_create_test'),
    path('mentor/task/<int:task_id>/progress/', mentor_views.mentor_task_progress, name='mentor_task_progress'),
    path('mentor/test/<int:test_id>/builder/', mentor_views.mentor_test_builder, name='mentor_test_builder'),
    path('mentor/test/<int:test_id>/reading-builder/', mentor_views.mentor_reading_builder, name='mentor_reading_builder'),
    path('mentor/test/<int:test_id>/preview/', mentor_views.mentor_test_preview, name='mentor_test_preview'),
    path('mentor/test/<int:test_id>/delete/', mentor_views.mentor_delete_test, name='mentor_delete_test'),

    # -----------------------------------------------------------------------
    # Mentor Panel — JSON API
    # -----------------------------------------------------------------------
    path('api/mentor/test/<int:test_id>/data/', mentor_views.api_test_data, name='api_test_data'),
    path('api/mentor/test/<int:test_id>/toggle-active/', mentor_views.api_toggle_test_active, name='api_toggle_test_active'),
    path('api/mentor/test/<int:test_id>/validate/', mentor_views.api_validate_test, name='api_validate_test'),
    path('api/mentor/test/<int:test_id>/publish/', mentor_views.api_publish_test, name='api_publish_test'),
    path('api/mentor/test/<int:test_id>/update/', mentor_views.api_update_test, name='api_update_test'),
    path('api/mentor/test/<int:test_id>/part/create/', mentor_views.api_create_part, name='api_create_part'),
    path('api/mentor/task/<int:task_id>/status/', mentor_views.api_task_status, name='api_task_status'),
    path('api/mentor/part/<int:part_id>/update/', mentor_views.api_update_part, name='api_update_part'),
    path('api/mentor/part/<int:part_id>/delete/', mentor_views.api_delete_part, name='api_delete_part'),
    path('api/mentor/part/<int:part_id>/upload-audio/', mentor_views.api_upload_part_audio, name='api_upload_part_audio'),
    path('api/mentor/part/<int:part_id>/question/create/', mentor_views.api_create_question, name='api_create_question'),
    path('api/mentor/question/<int:question_id>/update/', mentor_views.api_update_question, name='api_update_question'),
    path('api/mentor/question/<int:question_id>/delete/', mentor_views.api_delete_question, name='api_delete_question'),
    path('api/mentor/question/<int:question_id>/choice/create/', mentor_views.api_create_choice, name='api_create_choice'),
    path('api/mentor/choice/<int:choice_id>/update/', mentor_views.api_update_choice, name='api_update_choice'),
    path('api/mentor/choice/<int:choice_id>/delete/', mentor_views.api_delete_choice, name='api_delete_choice'),
    
    # Writing Task mentor APIs
    path('api/mentor/test/generate-writing/', mentor_views.api_generate_writing_test, name='api_generate_writing_test'),
    path('api/mentor/writing-task/<int:task_id>/update/', mentor_views.api_update_writing_task, name='api_update_writing_task'),
    path('api/mentor/test/<int:test_id>/writing-tasks/create/', mentor_views.api_create_writing_tasks_for_test, name='api_create_writing_tasks_for_test'),

    # Reading Module — JSON API
    path('api/mentor/reading-test/<int:test_id>/data/', mentor_views.api_reading_test_data, name='api_reading_test_data'),
    path('api/mentor/reading-test/<int:test_id>/part/create/', mentor_views.api_reading_create_part, name='api_reading_create_part'),
    path('api/mentor/reading-part/<int:part_id>/update/', mentor_views.api_reading_update_part, name='api_reading_update_part'),
    path('api/mentor/reading-part/<int:part_id>/delete/', mentor_views.api_reading_delete_part, name='api_reading_delete_part'),
    path('api/mentor/reading-part/<int:part_id>/question/create/', mentor_views.api_reading_create_question, name='api_reading_create_question'),
    path('api/mentor/reading-question/<int:question_id>/update/', mentor_views.api_reading_update_question, name='api_reading_update_question'),
    path('api/mentor/reading-question/<int:question_id>/delete/', mentor_views.api_reading_delete_question, name='api_reading_delete_question'),
    
    # Speaking Module
    path('mentor/speaking-test/<int:test_id>/builder/', mentor_views.mentor_speaking_builder, name='mentor_speaking_builder'),
    path('api/mentor/speaking-test/<int:test_id>/upload/', mentor_views.upload_speaking_page, name='api_speaking_upload'),
    path('api/mentor/speaking-test/<int:test_id>/save-validation/', mentor_views.save_speaking_validation, name='api_speaking_save_validation'),
    path('api/mentor/speaking-test/<int:test_id>/data/', mentor_views.api_speaking_test_data, name='api_speaking_test_data'),
    path('api/mentor/speaking-test/<int:test_id>/update/', mentor_views.api_speaking_update_test, name='api_speaking_update_test'),
    path('api/mentor/speaking-test/<int:test_id>/toggle-active/', mentor_views.api_speaking_toggle_active, name='api_speaking_toggle_active'),
    path('api/mentor/speaking-test/<int:test_id>/part/create/', mentor_views.api_speaking_create_part, name='api_speaking_create_part'),
    path('api/mentor/speaking-part/<int:part_id>/update/', mentor_views.api_speaking_update_part, name='api_speaking_update_part'),
    path('api/mentor/speaking-part/<int:part_id>/delete/', mentor_views.api_speaking_delete_part, name='api_speaking_delete_part'),
    path('api/mentor/speaking-part/<int:part_id>/question/create/', mentor_views.api_speaking_create_question, name='api_speaking_create_question'),
    path('api/mentor/speaking-question/<int:question_id>/update/', mentor_views.api_speaking_update_question, name='api_speaking_update_question'),
    path('api/mentor/speaking-question/<int:question_id>/delete/', mentor_views.api_speaking_delete_question, name='api_speaking_delete_question'),
    path('api/mentor/speaking-question/<int:question_id>/generate-tts/', mentor_views.api_speaking_generate_question_tts, name='api_speaking_generate_question_tts'),

    # Mentor Grading
    path('mentor/grade/<int:attempt_id>/', mentor_views.mentor_grade_submissions, name='mentor_grade_submissions'),
    path('api/mentor/grade/writing/<int:submission_id>/', mentor_views.api_mentor_grade_writing, name='api_mentor_grade_writing'),
    path('api/mentor/grade/speaking/<int:submission_id>/', mentor_views.api_mentor_grade_speaking, name='api_mentor_grade_speaking'),

    # User Management (admin-only)
    path('mentor/users/', mentor_views.mentor_users_list, name='mentor_users_list'),
    path('api/mentor/user/<int:user_id>/set-role/', mentor_views.api_user_set_role, name='api_user_set_role'),
    path('api/mentor/user/<int:user_id>/delete/', mentor_views.api_user_delete, name='api_user_delete'),

    # Speaking Test Student
    path('take-speaking-test/<int:attempt_id>/', views.take_speaking_test_view, name='take_speaking_test'),
    path('api/speaking/<int:attempt_id>/submit-answer/', views.api_submit_speaking_answer, name='api_submit_speaking_answer'),
    path('api/speaking/<int:attempt_id>/finalize/', views.finalize_speaking_test_view, name='finalize_speaking_test'),

    # AI Chat Assistant
    path('api/tts/', views.api_generate_tts, name='api_generate_tts'),
    path('ai-assistant/', views.ai_chat_view, name='ai_chat'),
    path('ai-assistant/<int:session_id>/', views.ai_chat_view, name='ai_chat_detail'),
    path('api/ai-chat/send/', views.api_ai_chat_send, name='api_ai_chat_send'),
    path('api/ai-chat/sessions/', views.api_ai_chat_sessions, name='api_ai_chat_sessions'),
    path('api/ai-chat/session/<int:session_id>/messages/', views.api_ai_chat_history, name='api_ai_chat_history'),
    path('api/ai-chat/session/<int:session_id>/delete/', views.api_ai_chat_delete_session, name='api_ai_chat_delete_session'),

    # Personal Dictionary
    path('dictionary/', views.dictionary_view, name='dictionary'),
    path('api/dictionary/lookup/', views.api_dictionary_lookup, name='api_dictionary_lookup'),
    path('api/dictionary/<int:entry_id>/delete/', views.api_dictionary_delete, name='api_dictionary_delete'),
    path('api/dictionary/<int:entry_id>/refresh/', views.api_dictionary_refresh, name='api_dictionary_refresh'),

    # Arcade Page
    path('arcade/', views.arcade_view, name='arcade'),

    # YouTube Video Lessons
    path('video-lessons/', views.video_lessons_view, name='video_lessons'),
    path('video-lesson/<int:lesson_id>/', views.video_lesson_detail_view, name='video_lesson_detail'),
    path('api/video-lesson/<int:lesson_id>/quiz-data/', views.api_video_quiz_data, name='api_video_quiz_data'),
    path('api/video-lesson/<int:lesson_id>/delete/', views.api_video_lesson_delete, name='api_video_lesson_delete'),
    path('api/video-lesson/progress/<str:task_id>/', views.api_video_lesson_progress, name='api_video_lesson_progress'),

    # Mentor: YouTube Video Lessons
    path('mentor/video-lessons/', mentor_views.mentor_video_lessons, name='mentor_video_lessons'),
    path('mentor/video-lesson/<int:lesson_id>/delete/', mentor_views.mentor_video_lesson_delete, name='mentor_video_lesson_delete'),

    # Multiplayer Video Room (Kahoot-style)
    path('video-room/join/', views.video_room_join_view, name='video_room_join'),
    path('video-room/play/<str:room_code>/', views.video_room_play_view, name='video_room_play'),
    path('video-room/host/<int:lesson_id>/', views.video_room_host_view, name='video_room_host'),
    path('video-room/<str:room_code>/end/', views.video_room_end_view, name='video_room_end'),
    path('video-room/<str:room_code>/ended/', views.video_room_ended_view, name='video_room_ended'),
    
    # Video Room APIs
    path('api/video-room/<str:room_code>/state/', views.api_room_participant_state, name='api_room_participant_state'),
    path('api/video-room/<str:room_code>/answer/', views.api_room_submit_answer, name='api_room_submit_answer'),
    path('api/video-room/<str:room_code>/host/state/', views.api_room_host_state, name='api_room_host_state'),
    path('api/video-room/<str:room_code>/host/set-state/', views.api_room_set_state, name='api_room_set_state'),

    # -----------------------------------------------------------------------
    # Mentor — Classroom Management
    # -----------------------------------------------------------------------
    path('mentor/classrooms/', mentor_views.classroom_list, name='classroom_list'),
    path('mentor/classroom/create/', mentor_views.classroom_create, name='classroom_create'),
    path('mentor/classroom/<int:classroom_id>/', mentor_views.classroom_detail, name='classroom_detail'),
    path('mentor/classroom/<int:classroom_id>/add-test/', mentor_views.api_classroom_add_test, name='classroom_add_test'),
    path('mentor/classroom/<int:classroom_id>/remove-test/', mentor_views.api_classroom_remove_test, name='classroom_remove_test'),
    path('mentor/classroom/<int:classroom_id>/add-video-lesson/', mentor_views.api_classroom_add_video_lesson, name='classroom_add_video_lesson'),
    path('mentor/classroom/<int:classroom_id>/remove-video-lesson/', mentor_views.api_classroom_remove_video_lesson, name='classroom_remove_video_lesson'),
    path('mentor/classroom/<int:classroom_id>/remove-student/', mentor_views.api_classroom_remove_student, name='classroom_remove_student'),
    path('mentor/classroom/<int:classroom_id>/regenerate-code/', mentor_views.api_classroom_regenerate_code, name='classroom_regenerate_code'),
    path('mentor/classroom/<int:classroom_id>/delete/', mentor_views.api_classroom_delete, name='classroom_delete'),
    path('mentor/classroom/<int:classroom_id>/edit/', mentor_views.api_classroom_edit, name='classroom_edit'),
    path('mentor/classroom/<int:classroom_id>/toggle-active/', mentor_views.api_classroom_toggle_active, name='classroom_toggle_active'),
    path('mentor/classroom/<int:classroom_id>/announcement/create/', mentor_views.api_classroom_announcement_create, name='classroom_announcement_create'),
    path('mentor/classroom/<int:classroom_id>/announcement/<int:announcement_id>/delete/', mentor_views.api_classroom_announcement_delete, name='classroom_announcement_delete'),
    path('mentor/classroom/<int:classroom_id>/announcement/<int:announcement_id>/pin/', mentor_views.api_classroom_announcement_pin, name='classroom_announcement_pin'),

    # -----------------------------------------------------------------------
    # Student — Classroom
    # -----------------------------------------------------------------------
    path('classroom/join/', views.join_classroom_view, name='join_classroom'),
    path('my-classroom/', views.my_classroom_view, name='my_classroom'),
    path('classroom/leave/', views.leave_classroom_view, name='leave_classroom'),
    path('classroom/invite/<uuid:token>/', views.classroom_invite_view, name='classroom_invite'),

    # -----------------------------------------------------------------------
    # Notifications
    # -----------------------------------------------------------------------
    path('api/notifications/', views.api_notifications_list, name='api_notifications_list'),
    path('api/notifications/mark-read/', views.api_notifications_mark_read, name='api_notifications_mark_read'),

    # -----------------------------------------------------------------------
    # Mentor — Assignment with Deadline
    # -----------------------------------------------------------------------
    path('mentor/classroom/<int:classroom_id>/add-assignment/', mentor_views.api_classroom_add_assignment, name='classroom_add_assignment'),
]
