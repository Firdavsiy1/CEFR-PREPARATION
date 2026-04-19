"""
Signals for the accounts app.

Auto-creates a UserProfile whenever a new User is created.
"""

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from urllib.request import urlopen
from urllib.error import URLError
from django.core.files.base import ContentFile
try:
    from allauth.account.signals import user_logged_in
except ImportError:
    user_logged_in = None

from .models import UserProfile

@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_profile(sender, instance, created, **kwargs):
    """Create a UserProfile for every new User and send a welcome email."""
    if created:
        UserProfile.objects.get_or_create(user=instance)
        # Send welcome email (runs in the same thread but fail_silently=True)
        try:
            from .emails import send_welcome_email
            send_welcome_email(instance)
        except Exception:
            pass


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def save_user_profile(sender, instance, **kwargs):
    """Ensure the profile is saved when the User is saved."""
    try:
        instance.profile.save()
    except UserProfile.DoesNotExist:
        UserProfile.objects.create(user=instance)

if user_logged_in:
    @receiver(user_logged_in)
    def update_user_profile_from_google(request, user, **kwargs):
        """Extract Google profile picture and real name on login."""
        try:
            # Check if this user logged in via google
            social_account = user.socialaccount_set.filter(provider='google').first()
            if not social_account:
                return

            extra_data = social_account.extra_data

            # 1. Update first name and last name if missing or different
            name_changed = False
            given_name = extra_data.get('given_name')
            family_name = extra_data.get('family_name')

            if given_name and user.first_name != given_name:
                user.first_name = given_name
                name_changed = True
            if family_name and user.last_name != family_name:
                user.last_name = family_name
                name_changed = True

            if name_changed:
                user.save(update_fields=['first_name', 'last_name'])

            # 2. Extract and save avatar if we don't have one
            picture_url = extra_data.get('picture')
            profile = user.profile
            
            # If the picture_url is high res, try to remove size limits if there are any
            if picture_url:
                # E.g. '.../photo.jpg=s96-c' -> '.../photo.jpg=s400-c' for better quality
                picture_url = picture_url.replace('=s96-c', '=s400-c')

            # Download if they don't have a custom avatar yet
            if picture_url and not profile.custom_avatar:
                try:
                    response = urlopen(picture_url, timeout=5)
                    if response.status == 200:
                        profile.custom_avatar.save(
                            f"google_avatar_{user.id}.jpg", 
                            ContentFile(response.read()), 
                            save=False
                        )
                        profile.avatar = 'custom'
                        profile.save()
                except URLError:
                    pass

        except Exception as e:
            # If anything fails during fetching social data, we just skip it 
            # to not block the login process.
            pass
