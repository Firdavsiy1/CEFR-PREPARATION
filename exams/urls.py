"""URL configuration for the exams app."""

from django.urls import path

from . import views
from . import mentor_views

app_name = 'exams'

urlpatterns = [
    # Student-facing views
    path('', views.dashboard_view, name='dashboard'),
    path('dashboard/<str:category>/', views.dashboard_view, name='dashboard_category'),
    path('test/<int:test_id>/tutorial/', views.test_tutorial_view, name='test_tutorial'),
    path('test/<int:test_id>/start/', views.start_test_view, name='start_test'),
    path('attempt/<int:attempt_id>/part/<int:part_number>/', views.take_test_part_view, name='take_test_part'),
    path('history/', views.exam_history_view, name='history'),
    path('history/<str:category>/', views.exam_history_view, name='history_category'),
    path('result/<int:attempt_id>/', views.test_result_view, name='test_result'),

    # -----------------------------------------------------------------------
    # Mentor Panel
    # -----------------------------------------------------------------------
    path('mentor/', mentor_views.mentor_dashboard, name='mentor_dashboard'),
    path('mentor/upload/', mentor_views.mentor_upload, name='mentor_upload'),
    path('mentor/test/<int:test_id>/builder/', mentor_views.mentor_test_builder, name='mentor_test_builder'),
    path('mentor/test/<int:test_id>/delete/', mentor_views.mentor_delete_test, name='mentor_delete_test'),

    # -----------------------------------------------------------------------
    # Mentor Panel — JSON API
    # -----------------------------------------------------------------------
    path('api/mentor/test/<int:test_id>/data/', mentor_views.api_test_data, name='api_test_data'),
    path('api/mentor/test/<int:test_id>/toggle-active/', mentor_views.api_toggle_test_active, name='api_toggle_test_active'),
    path('api/mentor/test/<int:test_id>/update/', mentor_views.api_update_test, name='api_update_test'),
    path('api/mentor/part/<int:part_id>/update/', mentor_views.api_update_part, name='api_update_part'),
    path('api/mentor/part/<int:part_id>/upload-audio/', mentor_views.api_upload_part_audio, name='api_upload_part_audio'),
    path('api/mentor/part/<int:part_id>/question/create/', mentor_views.api_create_question, name='api_create_question'),
    path('api/mentor/question/<int:question_id>/update/', mentor_views.api_update_question, name='api_update_question'),
    path('api/mentor/question/<int:question_id>/delete/', mentor_views.api_delete_question, name='api_delete_question'),
    path('api/mentor/question/<int:question_id>/choice/create/', mentor_views.api_create_choice, name='api_create_choice'),
    path('api/mentor/choice/<int:choice_id>/update/', mentor_views.api_update_choice, name='api_update_choice'),
    path('api/mentor/choice/<int:choice_id>/delete/', mentor_views.api_delete_choice, name='api_delete_choice'),
]
