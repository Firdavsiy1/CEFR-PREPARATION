"""
Models for the accounts app.

UserProfile extends Django's built-in User model with:
  - avatar selection (predefined CSS-rendered avatars)
  - language preference (en / ru / uz)

EmailVerification stores OTP codes for email verification during registration.
"""

import random
import string
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class EmailVerification(models.Model):
    """
    Stores a 6-digit verification code tied to pending registration data.
    Codes expire after 10 minutes.
    """
    email = models.EmailField()
    code = models.CharField(max_length=6)
    # Store the full registration form data as JSON so we can recreate the user later
    registration_data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Verification for {self.email} ({self.code})"

    @property
    def is_expired(self):
        """Code expires after 10 minutes."""
        return timezone.now() > self.created_at + timedelta(minutes=10)

    @classmethod
    def generate_code(cls):
        """Generate a random 6-digit numeric code."""
        return ''.join(random.choices(string.digits, k=6))

    @classmethod
    def create_for_email(cls, email, registration_data):
        """
        Create a new verification code for the given email.
        Invalidates all previous codes for this email.
        """
        # Mark all previous codes as used
        cls.objects.filter(email=email, is_used=False).update(is_used=True)
        # Create a new one
        return cls.objects.create(
            email=email,
            code=cls.generate_code(),
            registration_data=registration_data,
        )


class PasswordResetCode(models.Model):
    """
    Stores a 6-digit OTP code for password reset.
    Codes expire after 10 minutes.
    """
    email = models.EmailField()
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Password reset for {self.email} ({self.code})"

    @property
    def is_expired(self):
        """Code expires after 10 minutes."""
        return timezone.now() > self.created_at + timedelta(minutes=10)

    @classmethod
    def create_for_email(cls, email):
        """
        Create a new reset code for the given email.
        Invalidates all previous codes for this email.
        """
        cls.objects.filter(email=email, is_used=False).update(is_used=True)
        code = ''.join(random.choices(string.digits, k=6))
        return cls.objects.create(email=email, code=code)




class UserProfile(models.Model):
    """
    Extended profile for each user.
    Auto-created via a post_save signal on User (see signals.py).
    """

    # --- Avatar choices ---
    AVATAR_CHOICES = [
        ('owl', 'Owl'),
        ('fox', 'Fox'),
        ('cat', 'Cat'),
        ('dog', 'Dog'),
        ('rabbit', 'Rabbit'),
        ('penguin', 'Penguin'),
        ('butterfly', 'Butterfly'),
        ('star', 'Star'),
        ('flame', 'Fire'),
        ('lightning', 'Lightning'),
        ('rocket', 'Rocket'),
        ('planet', 'Planet'),
        ('diamond', 'Diamond'),
        ('crown', 'Crown'),
        ('heart', 'Heart'),
        ('custom', 'Custom'),
    ]

    # --- Language choices ---
    LANGUAGE_CHOICES = [
        ('en', 'English'),
        ('ru', 'Русский'),
        ('uz', 'O\'zbek'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
    )
    avatar = models.CharField(
        max_length=20,
        choices=AVATAR_CHOICES,
        default='owl',
        help_text='Selected avatar identifier.',
    )
    language = models.CharField(
        max_length=5,
        choices=LANGUAGE_CHOICES,
        default='ru',
        help_text='Preferred interface language.',
    )
    custom_avatar = models.ImageField(
        upload_to='avatars/',
        null=True,
        blank=True,
        help_text='User uploaded custom avatar.',
    )

    def __str__(self):
        return f"{self.user.username}'s profile"

    # --- Avatar rendering helpers ---

    AVATAR_META = {
        'owl':       {'icon': 'ph-bird',             'gradient': 'linear-gradient(135deg, #58CC02, #46A302)'},
        'fox':       {'icon': 'ph-paw-print',        'gradient': 'linear-gradient(135deg, #FF6B35, #E85D26)'},
        'custom':    {'icon': 'ph-user',             'gradient': 'linear-gradient(135deg, #2B4D56, #1A2F35)'},
        'cat':       {'icon': 'ph-cat',              'gradient': 'linear-gradient(135deg, #CE82FF, #A855F7)'},
        'dog':       {'icon': 'ph-dog',              'gradient': 'linear-gradient(135deg, #FFC800, #E5B400)'},
        'rabbit':    {'icon': 'ph-rabbit',           'gradient': 'linear-gradient(135deg, #FF9EC6, #F472B6)'},
        'penguin':   {'icon': 'ph-bird',             'gradient': 'linear-gradient(135deg, #1CB0F6, #1899D6)'},
        'butterfly': {'icon': 'ph-butterfly',        'gradient': 'linear-gradient(135deg, #FF6BCB, #D946EF)'},
        'star':      {'icon': 'ph-star',             'gradient': 'linear-gradient(135deg, #FFC800, #F59E0B)'},
        'flame':     {'icon': 'ph-fire',             'gradient': 'linear-gradient(135deg, #FF4B4B, #DC2626)'},
        'lightning': {'icon': 'ph-lightning',         'gradient': 'linear-gradient(135deg, #FACC15, #EAB308)'},
        'rocket':    {'icon': 'ph-rocket-launch',     'gradient': 'linear-gradient(135deg, #1CB0F6, #6366F1)'},
        'planet':    {'icon': 'ph-planet',           'gradient': 'linear-gradient(135deg, #6366F1, #4F46E5)'},
        'diamond':   {'icon': 'ph-diamond',          'gradient': 'linear-gradient(135deg, #22D3EE, #06B6D4)'},
        'crown':     {'icon': 'ph-crown',            'gradient': 'linear-gradient(135deg, #FFC800, #D97706)'},
        'heart':     {'icon': 'ph-heart',            'gradient': 'linear-gradient(135deg, #FF4B4B, #F472B6)'},
    }

    @property
    def avatar_icon(self):
        """Return the Phosphor icon CSS class for the selected avatar."""
        return self.AVATAR_META.get(self.avatar, self.AVATAR_META['owl'])['icon']

    @property
    def avatar_gradient(self):
        """Return the CSS gradient string for the selected avatar."""
        return self.AVATAR_META.get(self.avatar, self.AVATAR_META['owl'])['gradient']
