"""URL configuration for the exams app."""

from django.urls import path

from . import views

app_name = 'exams'

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('test/<int:test_id>/start/', views.start_test_view, name='start_test'),
    path('attempt/<int:attempt_id>/part/<int:part_number>/', views.take_test_part_view, name='take_test_part'),
    path('history/', views.exam_history_view, name='history'),
    path('result/<int:attempt_id>/', views.test_result_view, name='test_result'),
]
