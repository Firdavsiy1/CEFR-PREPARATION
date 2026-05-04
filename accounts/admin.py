"""Admin configuration for the accounts app."""

from django.contrib import admin
from .models import UserProfile, PasswordResetCode


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'avatar', 'language')
    list_filter = ('role', 'language', 'avatar')
    search_fields = ('user__username', 'user__email')
    raw_id_fields = ('user',)


@admin.register(PasswordResetCode)
class PasswordResetCodeAdmin(admin.ModelAdmin):
    list_display = ('email', 'code', 'created_at', 'is_used')
    list_filter = ('is_used',)
    search_fields = ('email',)
    readonly_fields = ('code', 'created_at')
