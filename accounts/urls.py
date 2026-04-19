"""URL configuration for the accounts app."""

from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', auth_views.LoginView.as_view(template_name='accounts/login.html', redirect_authenticated_user=True), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/accounts/login/'), name='logout'),
    path('register/', views.register_view, name='register'),
    path('verify-email/', views.verify_email_view, name='verify_email'),
    path('resend-code/', views.resend_code_view, name='resend_code'),
    path('forgot-password/', views.forgot_password_view, name='forgot_password'),
    path('reset-verify/', views.reset_verify_code_view, name='reset_verify_code'),
    path('resend-reset-code/', views.resend_reset_code_view, name='resend_reset_code'),
    path('reset-password/', views.reset_set_password_view, name='reset_set_password'),
    path('profile/', views.profile_view, name='profile'),
    path('profile/update/', views.update_profile, name='update_profile'),
    path('profile/password/', views.change_password, name='change_password'),
    path('profile/avatar/', views.update_avatar, name='update_avatar'),
    path('profile/language/', views.set_language, name='set_language'),
    path('profile/delete/', views.delete_account, name='delete_account'),
]
