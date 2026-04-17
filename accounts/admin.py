"""Admin configuration for the accounts app."""

from django.contrib import admin
from .models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'avatar', 'language')
    list_filter = ('language', 'avatar')
    search_fields = ('user__username', 'user__email')
    raw_id_fields = ('user',)
