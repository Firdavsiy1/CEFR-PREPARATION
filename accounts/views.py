"""
Views for the accounts app.

Handles registration, profile display, and profile updates
(username, email, password, avatar, language).
"""

import json

from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .forms import CustomUserCreationForm, ProfileUpdateForm
from .models import UserProfile


def register_view(request):
    """View for user registration with required email field."""
    # Redirect if already logged in
    if request.user.is_authenticated:
        return redirect('exams:dashboard')

    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, f"msg_reg_success*{user.username}")
            return redirect('exams:dashboard')
        else:
            messages.error(request, "msg_reg_fail")
    else:
        form = CustomUserCreationForm()

    return render(request, 'accounts/register.html', {'form': form})


@login_required
def profile_view(request):
    """Display the user profile page with all management forms."""
    # Ensure profile exists
    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=request.user)

    profile_form = ProfileUpdateForm(instance=request.user)
    password_form = PasswordChangeForm(request.user)

    context = {
        'profile': profile,
        'profile_form': profile_form,
        'password_form': password_form,
        'avatar_choices': UserProfile.AVATAR_CHOICES,
        'avatar_meta': UserProfile.AVATAR_META,
        'language_choices': UserProfile.LANGUAGE_CHOICES,
    }
    return render(request, 'accounts/profile.html', context)


@login_required
@require_POST
def update_profile(request):
    """Handle username and email updates."""
    form = ProfileUpdateForm(request.POST, instance=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, 'msg_profile_success')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f'{error}')
    return redirect('accounts:profile')


@login_required
@require_POST
def change_password(request):
    """Handle password change."""
    form = PasswordChangeForm(request.user, request.POST)
    if form.is_valid():
        user = form.save()
        # Keep the user logged in after password change
        update_session_auth_hash(request, user)
        messages.success(request, 'msg_pw_success')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f'{error}')
    return redirect('accounts:profile')


@login_required
@require_POST
def update_avatar(request):
    """Handle avatar selection update AND custom image uploads."""
    profile = request.user.profile
    
    # Check if a custom file was uploaded
    if 'custom_avatar_file' in request.FILES:
        file = request.FILES['custom_avatar_file']
        
        # 1. Size validation (max 8MB)
        if file.size > 8 * 1024 * 1024:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'message': 'File too large (max 8MB)'}, status=400)
            messages.error(request, 'File too large (max 8MB).')
            return redirect('accounts:profile')
            
        # 2. Strict file check using Pillow
        try:
            from PIL import Image
            import io
            # Read into memory to verify without breaking the file pointer for saving
            file_data = file.read()
            img = Image.open(io.BytesIO(file_data))
            img.verify() # Checks if it's a valid image
            
            # Reset file pointer for Django to save it
            file.seek(0)
            
            # Check for allowed formats just to be safe
            if img.format.lower() not in ['jpeg', 'jpg', 'png', 'webp', 'gif', 'mpo']:
                raise ValueError("Unsupported format")
        except Exception:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'message': 'Invalid image file'}, status=400)
            messages.error(request, 'Invalid image file.')
            return redirect('accounts:profile')
            
        profile.custom_avatar = file
        profile.avatar = 'custom'
        profile.save()
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'ok',
                'avatar': 'custom',
                'icon': 'ph-user',
                'gradient': profile.avatar_gradient,
                'custom_url': profile.custom_avatar.url if profile.custom_avatar else None
            })
        messages.success(request, 'msg_avatar_success')
        return redirect('accounts:profile')

    # Standard preset avatar handling
    avatar = request.POST.get('avatar', '')
    valid_avatars = [choice[0] for choice in UserProfile.AVATAR_CHOICES]

    if avatar in valid_avatars:
        profile.avatar = avatar
        profile.save()

        # Return JSON for AJAX requests
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'ok',
                'avatar': avatar,
                'icon': profile.avatar_icon,
                'gradient': profile.avatar_gradient,
                'custom_url': None
            })

        messages.success(request, 'msg_avatar_success')
    else:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': 'Invalid avatar'}, status=400)
        messages.error(request, 'msg_avatar_invalid')

    return redirect('accounts:profile')


@login_required
@require_POST
def set_language(request):
    """Handle language preference change."""
    lang = request.POST.get('language', '')
    valid_langs = [choice[0] for choice in UserProfile.LANGUAGE_CHOICES]

    if lang in valid_langs:
        profile = request.user.profile
        profile.language = lang
        profile.save()

        # Also store in session for immediate effect
        request.session['django_language'] = lang

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'ok', 'language': lang})

        messages.success(request, 'msg_lang_success')
    else:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': 'Invalid language'}, status=400)
        messages.error(request, 'msg_lang_invalid')

    return redirect('accounts:profile')
