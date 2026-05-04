"""Adapters for allauth customization."""

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.urls import reverse


class CEFRSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Redirect users back to profile after connecting a social account."""

    def get_connect_redirect_url(self, request, socialaccount):
        return reverse('accounts:profile')

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        
        # Read the preferred role from cookies (set on the registration page)
        role = request.COOKIES.get('preferred_role')
        if role in ['student', 'mentor'] and hasattr(user, 'profile'):
            user.profile.role = role
            user.profile.save(update_fields=['role'])
            
        return user
