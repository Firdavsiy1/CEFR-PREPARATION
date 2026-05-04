"""
accounts/tasks.py — Celery tasks for background accounts operations.

Tasks:
    update_session_city      — Geolocate an IP and write city into the Django session.
    send_streak_goal_email   — Send a congratulatory email when a streak goal is hit.
"""

import logging
import time

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def update_session_city(session_key: str, ip_address: str) -> None:
    """Geolocate *ip_address* and patch the 'city' field in the Django session."""
    time.sleep(1)  # Give session middleware time to commit the session row.

    from accounts.signals import _geolocate_ip

    city = _geolocate_ip(ip_address)
    if not city:
        return
    try:
        from django.contrib.sessions.backends.db import SessionStore
        from django import db

        store = SessionStore(session_key=session_key)
        store.load()
        meta = store.get('login_meta') or {}
        if meta.get('city'):
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


@shared_task
def send_streak_goal_email_task(user_id: int, streak_days: int) -> None:
    """Send a streak-goal congratulation email for the given *user_id*."""
    try:
        from django.contrib.auth import get_user_model
        from accounts.emails import send_streak_goal_email

        User = get_user_model()
        user = User.objects.get(pk=user_id)
        send_streak_goal_email(user, streak_days)
    except Exception:
        logger.exception("[send_streak_goal_email_task] user_id=%s streak=%s", user_id, streak_days)
