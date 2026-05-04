"""
Context processors for the exams app.
Provides global template context variables.
"""


def mentor_status(request):
    """
    Add `user_is_mentor`, `user_is_admin`, and `user_role` to the template
    context so the navbar and mentor panel can conditionally show/hide controls.

    Uses profile.role instead of checking the Mentors group for forward
    compatibility, while still falling back to the group check for safety.
    """
    if request.user.is_authenticated:
        try:
            role = request.user.profile.role
        except Exception:
            role = 'student'
        is_mentor = (role in ['mentor', 'sysmentor']) or request.user.is_superuser
        is_sysmentor = (role == 'sysmentor') or request.user.is_superuser
        return {
            'user_is_mentor': is_mentor,
            'user_is_sysmentor': is_sysmentor,
            'user_is_admin': request.user.is_superuser,
            'user_role': role,
        }
    return {'user_is_mentor': False, 'user_is_sysmentor': False, 'user_is_admin': False, 'user_role': 'guest'}
