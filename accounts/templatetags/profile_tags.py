"""
Custom template tags and filters for the accounts app.

Provides filters to look up avatar metadata (icon, gradient)
from the AVATAR_META dictionary by key.
"""

from django import template

register = template.Library()


@register.filter(name='get_icon')
def get_icon(avatar_meta, key):
    """
    Get the Phosphor icon class for an avatar key.
    Usage: {{ avatar_meta|get_icon:key }}
    """
    if isinstance(avatar_meta, dict) and key in avatar_meta:
        return avatar_meta[key].get('icon', 'ph-user')
    return 'ph-user'


@register.filter(name='get_gradient')
def get_gradient(avatar_meta, key):
    """
    Get the CSS gradient for an avatar key.
    Usage: {{ avatar_meta|get_gradient:key }}
    """
    if isinstance(avatar_meta, dict) and key in avatar_meta:
        return avatar_meta[key].get('gradient', 'linear-gradient(135deg, #58CC02, #46A302)')
    return 'linear-gradient(135deg, #58CC02, #46A302)'
