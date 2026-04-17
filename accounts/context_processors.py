"""
Context processors for the accounts app.

Injects user profile data (avatar, language) and the list of
available languages into every template.
"""

from .models import UserProfile


def user_profile_context(request):
    """
    Add the user's profile and available languages to template context.
    """
    context = {
        'available_languages': UserProfile.LANGUAGE_CHOICES,
        'current_language': 'ru',  # default
    }

    if request.user.is_authenticated:
        try:
            profile = request.user.profile
        except UserProfile.DoesNotExist:
            profile = UserProfile.objects.create(user=request.user)

        context['user_profile'] = profile
        context['current_language'] = profile.language

    return context
