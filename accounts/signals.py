"""
Signals for the accounts app.

Auto-creates a UserProfile whenever a new User is created.
"""

import ipaddress

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in as django_user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from urllib.request import urlopen, Request
from urllib.error import URLError
from django.core.files.base import ContentFile
try:
    from allauth.account.signals import user_logged_in
except ImportError:
    user_logged_in = None
try:
    from allauth.socialaccount.signals import pre_social_login
except ImportError:
    pre_social_login = None

from .models import UserProfile


if pre_social_login:
    @receiver(pre_social_login)
    def connect_google_account_by_email(request, sociallogin, **kwargs):
        """Link Google social login to an existing user with the same verified email."""
        if request.user.is_authenticated or sociallogin.is_existing:
            return
        if sociallogin.account.provider != 'google':
            return

        email = (sociallogin.user.email or sociallogin.account.extra_data.get('email') or '').strip().lower()
        if not email:
            return

        # Google sends email_verified for accounts with confirmed mailbox ownership.
        email_verified = sociallogin.account.extra_data.get('email_verified')
        if email_verified is False:
            return

        User = get_user_model()
        existing_user = User.objects.filter(email__iexact=email).first()
        if not existing_user:
            return

        sociallogin.connect(request, existing_user)


def _extract_client_ip(request):
    """Extract client IP from request headers."""
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()[:64]
    return request.META.get('REMOTE_ADDR', '')[:64]


def _geolocate_ip(ip_address):
    """Return 'City, Country' for a public IP via ipapi.co. Returns '' on any failure."""
    if not ip_address:
        return ''
    try:
        ip_obj = ipaddress.ip_address(ip_address)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved or ip_obj.is_link_local:
            return ''
    except ValueError:
        return ''

    try:
        import json as _json
        req = Request(
            f'https://ipapi.co/{ip_address}/json/',
            headers={'User-Agent': 'CEFRPrep/1.0'},
        )
        with urlopen(req, timeout=3) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        if data.get('error'):
            return ''
        city = data.get('city', '')
        country = data.get('country_name', '')
        if city and country:
            return f'{city}, {country}'
        return country or city
    except Exception:
        return ''


def _update_session_city_bg(session_key, ip_address):
    """Background task: geolocate IP and patch session metadata in DB."""
    time.sleep(1)  # Wait for session middleware to commit session to DB
    city = _geolocate_ip(ip_address)
    if not city:
        return
    try:
        from django.contrib.sessions.backends.db import SessionStore
        store = SessionStore(session_key=session_key)
        store.load()
        meta = store.get('login_meta') or {}
        if meta.get('city'):  # Already set
            return
        meta['city'] = city
        store['login_meta'] = meta
        store.modified = True
        store.save()
    except Exception:
        pass
    finally:
        try:
            from django import db
            db.close_old_connections()
        except Exception:
            pass

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


@receiver(django_user_logged_in)
def attach_login_metadata(sender, request, user, **kwargs):
    """Persist login metadata in session; start background geolocation."""
    if not request:
        return

    ip_address = _extract_client_ip(request)
    request.session['login_meta'] = {
        'ip_address': ip_address,
        'user_agent': request.META.get('HTTP_USER_AGENT', '')[:255],
        'login_at': timezone.now().isoformat(timespec='seconds'),
        'city': '',  # Filled by background geolocation thread
    }
    request.session.modified = True

    # Dispatch geolocation to a Celery task to avoid login latency.
    session_key = request.session.session_key
    if session_key and ip_address:
        from accounts.tasks import update_session_city
        update_session_city.delay(session_key, ip_address)
