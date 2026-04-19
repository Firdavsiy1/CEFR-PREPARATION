"""App configuration for the accounts app."""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'

    def ready(self):
        import accounts.signals  # noqa: F401
        
        # Enforce email uniqueness on the default User model
        from django.contrib.auth.models import User
        email_field = User._meta.get_field('email')
        email_field._unique = True
        email_field.blank = False
        email_field.null = False
