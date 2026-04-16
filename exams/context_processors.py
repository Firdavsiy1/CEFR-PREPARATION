"""
Context processors for the exams app.
Provides global template context variables.
"""


def mentor_status(request):
    """
    Add `user_is_mentor` to the template context so the navbar
    can conditionally show the Mentor Panel link.
    """
    if request.user.is_authenticated:
        return {
            'user_is_mentor': request.user.groups.filter(name='Mentors').exists(),
        }
    return {'user_is_mentor': False}
