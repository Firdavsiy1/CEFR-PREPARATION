"""
Email utilities for the accounts app.

Sends styled HTML welcome emails on registration.
"""

import logging
from datetime import datetime

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


def send_welcome_email(user):
    """
    Send a beautiful HTML welcome email to a newly registered user.

    Works for both manual registration and Google OAuth sign-ups.
    Silently fails if email sending is not configured or if
    the user doesn't have an email address.
    """
    if not user.email:
        return

    # Build display name: prefer real name, fall back to username
    display_name = user.first_name or user.username

    # Build the dashboard URL
    try:
        from django.contrib.sites.models import Site
        current_site = Site.objects.get_current()
        protocol = 'https' if not settings.DEBUG else 'http'
        dashboard_url = f"{protocol}://{current_site.domain}/"
    except Exception:
        dashboard_url = "http://127.0.0.1:8000/"

    context = {
        'display_name': display_name,
        'dashboard_url': dashboard_url,
        'year': datetime.now().year,
    }

    subject = f"🎉 Welcome to CEFRPrep, {display_name}!"

    try:
        html_content = render_to_string('emails/welcome.html', context)
        text_content = strip_tags(html_content)

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        email.attach_alternative(html_content, "text/html")
        email.send(fail_silently=True)

        logger.info(f"Welcome email sent to {user.email}")
    except Exception as e:
        logger.error(f"Failed to send welcome email to {user.email}: {e}")


def send_verification_code_email(email, code):
    """
    Send a verification code email for registration.
    Returns True if sent successfully, False otherwise.
    """
    context = {
        'code': code,
        'year': datetime.now().year,
    }

    subject = f"🔐 Your CEFRPrep verification code: {code}"

    try:
        html_content = render_to_string('emails/verification_code.html', context)
        text_content = f"Your CEFRPrep verification code is: {code}\n\nThis code expires in 10 minutes."

        email_msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
        )
        email_msg.attach_alternative(html_content, "text/html")
        email_msg.send(fail_silently=False)

        logger.info(f"Verification code sent to {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send verification code to {email}: {e}")
        return False


def send_password_reset_email(email, code):
    """
    Send a password reset OTP code email.
    Reuses the verification email template.
    Returns True if sent successfully, False otherwise.
    """
    context = {
        'code': code,
        'year': datetime.now().year,
    }

    subject = f"🔑 CEFRPrep password reset code: {code}"

    try:
        html_content = render_to_string('emails/password_reset_code.html', context)
        text_content = f"Your password reset code is: {code}\n\nThis code expires in 10 minutes."

        email_msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
        )
        email_msg.attach_alternative(html_content, "text/html")
        email_msg.send(fail_silently=False)

        logger.info(f"Password reset code sent to {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send password reset code to {email}: {e}")
        return False
