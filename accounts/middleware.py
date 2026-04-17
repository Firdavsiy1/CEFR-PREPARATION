"""
Middleware for the accounts app.

LanguageMiddleware activates the user's preferred language for each request.
"""

from django.utils import translation


class LanguageMiddleware:
    """
    Read the user's language preference from their UserProfile
    and activate it for the current request.
    Falls back to session-stored language for anonymous users.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        lang = None

        # 1. Authenticated users: read from profile
        if request.user.is_authenticated:
            try:
                lang = request.user.profile.language
            except Exception:
                pass

        # 2. Fall back to session
        if not lang:
            lang = request.session.get('django_language', 'ru')

        # Activate the language for this request
        translation.activate(lang)
        request.LANGUAGE_CODE = lang

        response = self.get_response(request)

        # Deactivate after response
        translation.deactivate()

        return response
