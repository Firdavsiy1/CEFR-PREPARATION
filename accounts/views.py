"""
Views for the accounts app.

Handles registration, profile display, and profile updates
(username, email, password, avatar, language).
"""

import json
import re
from datetime import datetime

from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.sessions.models import Session
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from allauth.socialaccount.models import SocialAccount

from .forms import CustomUserCreationForm, ProfileUpdateForm
from .models import UserProfile, EmailVerification, PasswordResetCode
from .emails import send_verification_code_email, send_password_reset_email
from .ratelimit import check_rate_limit


@require_http_methods(["GET", "POST"])
def logout_view(request):
    """Log out the current user and redirect to login.

    Supports GET to avoid CSRF token mismatch issues in navbar logout flows.
    """
    logout(request)
    return redirect('accounts:login')


def _parse_user_agent(user_agent):
    """Parse User-Agent string and return device info dict with label and icon."""
    ua = (user_agent or '').strip()
    if not ua:
        return {'label': 'Unknown device', 'icon': 'ph-desktop'}

    lower = ua.lower()

    # ── OS / Platform ────────────────────────────────────────────────
    if 'ipad' in lower:
        m = re.search(r'cpu os ([\d_]+)', lower)
        ver = m.group(1).replace('_', '.')[:4] if m else ''
        platform = f'iPadOS {ver}'.strip()
        icon = 'ph-device-tablet'
    elif 'iphone' in lower:
        m = re.search(r'cpu iphone os ([\d_]+)', lower)
        ver = m.group(1).replace('_', '.')[:4] if m else ''
        platform = f'iPhone iOS {ver}'.strip()
        icon = 'ph-device-mobile'
    elif 'android' in lower:
        m = re.search(r'android ([\d.]+)', lower)
        ver = m.group(1)[:3] if m else ''
        platform = f'Android {ver}'.strip()
        icon = 'ph-device-tablet' if 'tablet' in lower else 'ph-device-mobile'
    elif 'windows nt' in lower:
        nt_map = {'10.0': '10/11', '6.3': '8.1', '6.2': '8', '6.1': '7', '6.0': 'Vista'}
        m = re.search(r'windows nt ([\d.]+)', lower)
        nt = m.group(1) if m else ''
        platform = f"Windows {nt_map.get(nt, nt)}".strip()
        icon = 'ph-desktop'
    elif 'mac os x' in lower or 'macintosh' in lower:
        m = re.search(r'mac os x ([\d_]+)', lower)
        ver = m.group(1).replace('_', '.')[:5] if m else ''
        platform = f'macOS {ver}'.strip()
        icon = 'ph-desktop'
    elif 'cros' in lower:
        platform = 'ChromeOS'
        icon = 'ph-desktop'
    elif 'x11' in lower or 'linux' in lower:
        platform = 'Linux'
        icon = 'ph-desktop'
    else:
        platform = 'Unknown OS'
        icon = 'ph-desktop'

    # ── Browser (order matters: most specific first) ──────────────────
    if 'edg/' in lower or 'edghtml/' in lower:
        m = re.search(r'edg[e/]+(\d+)', lower)
        ver = m.group(1) if m else ''
        browser = f'Edge {ver}'.strip()
    elif 'opr/' in lower:
        m = re.search(r'opr/(\d+)', lower)
        ver = m.group(1) if m else ''
        browser = f'Opera {ver}'.strip()
    elif 'yabrowser/' in lower:
        m = re.search(r'yabrowser/(\d+)', lower)
        ver = m.group(1) if m else ''
        browser = f'Яндекс {ver}'.strip()
    elif 'samsungbrowser/' in lower:
        m = re.search(r'samsungbrowser/(\d+)', lower)
        ver = m.group(1) if m else ''
        browser = f'Samsung Browser {ver}'.strip()
    elif 'chrome/' in lower:
        m = re.search(r'chrome/(\d+)', lower)
        ver = m.group(1) if m else ''
        browser = f'Chrome {ver}'.strip()
    elif 'firefox/' in lower:
        m = re.search(r'firefox/(\d+)', lower)
        ver = m.group(1) if m else ''
        browser = f'Firefox {ver}'.strip()
    elif 'safari/' in lower:
        m = re.search(r'version/(\d+)', lower)
        ver = m.group(1) if m else ''
        browser = f'Safari {ver}'.strip()
    else:
        browser = 'Unknown browser'

    return {'label': f'{platform} · {browser}', 'icon': icon}


def _get_user_active_sessions(user, current_session_key):
    """Collect active django sessions for the given user."""
    sessions = []
    for session in Session.objects.filter(expire_date__gte=timezone.now()).order_by('-expire_date'):
        try:
            decoded = session.get_decoded()
        except Exception:
            continue

        if str(decoded.get('_auth_user_id', '')) != str(user.id):
            continue

        login_meta = decoded.get('login_meta') or {}
        user_agent = login_meta.get('user_agent', '')
        ip_address = login_meta.get('ip_address', '')
        city = login_meta.get('city', '')
        login_at_str = login_meta.get('login_at', '')

        login_at = None
        if login_at_str:
            try:
                login_at = datetime.fromisoformat(login_at_str)
            except (ValueError, TypeError):
                pass

        device_info = _parse_user_agent(user_agent)

        sessions.append({
            'session_key': session.session_key,
            'is_current': session.session_key == current_session_key,
            'expires_at': session.expire_date,
            'device_label': device_info['label'],
            'device_icon': device_info['icon'],
            'ip_address': ip_address or '-',
            'city': city,
            'login_at': login_at,
        })

    return sessions


# =============================================
# PASSWORD RESET FLOW (Forgot Password)
# =============================================

def forgot_password_view(request):
    """
    Step 1: User enters their email to receive a reset code.
    """
    if request.user.is_authenticated:
        return redirect('exams:dashboard')

    if request.method == 'POST':
        if check_rate_limit(request, 'forgot_password', limit=5, window=900):
            messages.error(request, 'msg_too_many_attempts')
            return render(request, 'accounts/forgot_password.html')

        email = request.POST.get('email', '').strip()

        if not email:
            messages.error(request, 'msg_reset_email_required')
            return render(request, 'accounts/forgot_password.html')

        # Check if user exists with this email
        from django.contrib.auth.models import User
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Don't reveal whether the email exists — still redirect
            messages.success(request, 'msg_reset_code_sent')
            return render(request, 'accounts/forgot_password.html')

        # Create and send OTP
        reset_code = PasswordResetCode.create_for_email(email)
        sent = send_password_reset_email(email, reset_code.code)

        if sent:
            request.session['reset_code_id'] = reset_code.id
            request.session['reset_email'] = email
            return redirect('accounts:reset_verify_code')
        else:
            messages.error(request, 'msg_email_send_fail')

    return render(request, 'accounts/forgot_password.html')


def reset_verify_code_view(request):
    """
    Step 2: User enters the 6-digit OTP code.
    """
    if request.user.is_authenticated:
        return redirect('exams:dashboard')

    reset_code_id = request.session.get('reset_code_id')
    reset_email = request.session.get('reset_email')

    if not reset_code_id or not reset_email:
        messages.error(request, 'msg_no_pending_reset')
        return redirect('accounts:forgot_password')

    try:
        reset_obj = PasswordResetCode.objects.get(id=reset_code_id, is_used=False)
    except PasswordResetCode.DoesNotExist:
        messages.error(request, 'msg_reset_expired')
        return redirect('accounts:forgot_password')

    if reset_obj.is_expired:
        messages.error(request, 'msg_code_expired')
        return redirect('accounts:forgot_password')

    if request.method == 'POST':
        if check_rate_limit(request, f'reset_verify:{reset_code_id}', limit=10, window=900):
            messages.error(request, 'msg_too_many_attempts')
            return render(request, 'accounts/reset_verify_code.html', {'email': reset_email})

        entered_code = ''
        for i in range(1, 7):
            digit = request.POST.get(f'digit{i}', '')
            entered_code += digit

        if entered_code == reset_obj.code:
            # Mark OTP as verified (but not used yet — used after new password set)
            request.session['reset_code_verified'] = True
            return redirect('accounts:reset_set_password')
        else:
            messages.error(request, 'msg_wrong_code')

    return render(request, 'accounts/reset_verify_code.html', {
        'email': reset_email,
    })


def resend_reset_code_view(request):
    """Resend a new reset code for the pending password reset."""
    if request.user.is_authenticated:
        return redirect('exams:dashboard')

    reset_email = request.session.get('reset_email')
    if not reset_email:
        return redirect('accounts:forgot_password')

    if check_rate_limit(request, 'resend_reset', limit=3, window=300):
        messages.error(request, 'msg_too_many_attempts')
        return redirect('accounts:reset_verify_code')

    new_reset = PasswordResetCode.create_for_email(reset_email)
    sent = send_password_reset_email(reset_email, new_reset.code)

    if sent:
        request.session['reset_code_id'] = new_reset.id
        messages.success(request, 'msg_code_resent')
    else:
        messages.error(request, 'msg_email_send_fail')

    return redirect('accounts:reset_verify_code')


def reset_set_password_view(request):
    """
    Step 3: User sets a new password after OTP verification.
    """
    if request.user.is_authenticated:
        return redirect('exams:dashboard')

    reset_code_id = request.session.get('reset_code_id')
    reset_email = request.session.get('reset_email')
    code_verified = request.session.get('reset_code_verified')

    if not reset_code_id or not reset_email or not code_verified:
        messages.error(request, 'msg_no_pending_reset')
        return redirect('accounts:forgot_password')

    if request.method == 'POST':
        password1 = request.POST.get('new_password1', '')
        password2 = request.POST.get('new_password2', '')

        if not password1 or len(password1) < 8:
            messages.error(request, 'msg_reset_pw_too_short')
            return render(request, 'accounts/reset_set_password.html', {'email': reset_email})

        if password1 != password2:
            messages.error(request, 'msg_reset_pw_mismatch')
            return render(request, 'accounts/reset_set_password.html', {'email': reset_email})

        if password1.isdigit():
            messages.error(request, 'msg_reset_pw_numeric')
            return render(request, 'accounts/reset_set_password.html', {'email': reset_email})

        # Set the new password
        from django.contrib.auth.models import User
        try:
            user = User.objects.get(email=reset_email)
        except User.DoesNotExist:
            messages.error(request, 'msg_reset_expired')
            return redirect('accounts:forgot_password')

        # Delete reset code after successful use
        try:
            reset_obj = PasswordResetCode.objects.get(id=reset_code_id, is_used=False)
            reset_obj.delete()
        except PasswordResetCode.DoesNotExist:
            pass

        user.set_password(password1)
        user.save()

        # Clean up session
        for key in ['reset_code_id', 'reset_email', 'reset_code_verified']:
            request.session.pop(key, None)

        messages.success(request, 'msg_reset_pw_success')
        return redirect('accounts:login')

    return render(request, 'accounts/reset_set_password.html', {'email': reset_email})


# =============================================
# REGISTRATION FLOW
# =============================================
def register_view(request):
    """
    Step 1: Validate the registration form and send a verification code.
    User data is NOT saved yet — stored in EmailVerification model.
    """
    # Redirect if already logged in
    if request.user.is_authenticated:
        return redirect('exams:dashboard')

    if request.method == 'POST':
        if check_rate_limit(request, 'register', limit=10, window=900):
            messages.error(request, 'msg_too_many_attempts')
            return render(request, 'accounts/register.html', {'form': CustomUserCreationForm()})

        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']

            # Store form data in the verification model
            registration_data = {
                'username': form.cleaned_data['username'],
                'email': email,
                'password': form.cleaned_data['password1'],
                'role': form.cleaned_data.get('role', 'student'),
            }

            verification = EmailVerification.create_for_email(email, registration_data)

            # Send the code
            sent = send_verification_code_email(email, verification.code)
            if sent:
                # Store verification ID in session
                request.session['pending_verification_id'] = verification.id
                return redirect('accounts:verify_email')
            else:
                messages.error(request, "msg_email_send_fail")
        else:
            messages.error(request, "msg_reg_fail")
    else:
        form = CustomUserCreationForm()

    return render(request, 'accounts/register.html', {'form': form})


def verify_email_view(request):
    """
    Step 2: User enters the 6-digit code sent to their email.
    On success, create the user account and log them in.
    """
    if request.user.is_authenticated:
        return redirect('exams:dashboard')

    verification_id = request.session.get('pending_verification_id')
    if not verification_id:
        messages.error(request, "msg_no_pending_verification")
        return redirect('accounts:register')

    try:
        verification = EmailVerification.objects.get(id=verification_id, is_used=False)
    except EmailVerification.DoesNotExist:
        messages.error(request, "msg_verification_expired")
        return redirect('accounts:register')

    if verification.is_expired:
        messages.error(request, "msg_code_expired")
        return redirect('accounts:register')

    email = verification.email

    if request.method == 'POST':
        if check_rate_limit(request, f'verify_email:{verification_id}', limit=10, window=900):
            messages.error(request, 'msg_too_many_attempts')
            return render(request, 'accounts/verify_email.html', {
                'email': email, 'verification_id': verification_id,
            })

        # Collect digits from individual inputs
        entered_code = ''
        for i in range(1, 7):
            digit = request.POST.get(f'digit{i}', '')
            entered_code += digit

        if entered_code == verification.code:
            # Mark as used
            verification.is_used = True
            verification.save()

            # Create the user from stored data
            from django.contrib.auth.models import User
            from django.contrib.auth.models import Group
            data = verification.registration_data
            user = User.objects.create_user(
                username=data['username'],
                email=data['email'],
                password=data['password'],
            )

            # Set user role from registration choice
            role = data.get('role', 'student')
            try:
                profile = user.profile
                profile.role = role
                profile.save(update_fields=['role'])
            except Exception:
                pass

            # Add mentor to 'Mentors' group for backward compatibility
            if role == 'mentor':
                mentors_group, _ = Group.objects.get_or_create(name='Mentors')
                user.groups.add(mentors_group)

            login(request, user, backend='django.contrib.auth.backends.ModelBackend')

            # Clean up session
            del request.session['pending_verification_id']

            messages.success(request, f"msg_reg_success*{user.username}")
            return redirect('exams:dashboard')
        else:
            messages.error(request, "msg_wrong_code")

    return render(request, 'accounts/verify_email.html', {
        'email': email,
        'verification_id': verification_id,
    })


def resend_code_view(request):
    """Resend a new verification code for the pending registration."""
    if request.user.is_authenticated:
        return redirect('exams:dashboard')

    verification_id = request.session.get('pending_verification_id')
    if not verification_id:
        return redirect('accounts:register')

    if check_rate_limit(request, 'resend_verify', limit=3, window=300):
        messages.error(request, 'msg_too_many_attempts')
        return redirect('accounts:verify_email')

    try:
        old_verification = EmailVerification.objects.get(id=verification_id, is_used=False)
    except EmailVerification.DoesNotExist:
        return redirect('accounts:register')

    # Create a new code with the same registration data
    new_verification = EmailVerification.create_for_email(
        old_verification.email,
        old_verification.registration_data,
    )

    sent = send_verification_code_email(new_verification.email, new_verification.code)
    if sent:
        request.session['pending_verification_id'] = new_verification.id
        messages.success(request, "msg_code_resent")
    else:
        messages.error(request, "msg_email_send_fail")

    return redirect('accounts:verify_email')


@login_required
def profile_view(request):
    """Display the user profile page with all management forms."""
    # Ensure profile exists
    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=request.user)

    profile_form = ProfileUpdateForm(instance=request.user)
    if request.user.has_usable_password():
        password_form = PasswordChangeForm(request.user)
    else:
        password_form = SetPasswordForm(request.user)
    google_social_account = request.user.socialaccount_set.filter(provider='google').first()

    context = {
        'profile': profile,
        'profile_form': profile_form,
        'password_form': password_form,
        'active_sessions': _get_user_active_sessions(request.user, request.session.session_key),
        'avatar_choices': UserProfile.AVATAR_CHOICES,
        'avatar_meta': UserProfile.AVATAR_META,
        'language_choices': UserProfile.LANGUAGE_CHOICES,
        'google_social_account': google_social_account,
    }
    return render(request, 'accounts/profile.html', context)


@login_required
@require_POST
def terminate_session(request):
    """Terminate a specific session that belongs to the current user."""
    session_key = request.POST.get('session_key', '').strip()
    current_session_key = request.session.session_key

    if not session_key:
        messages.error(request, 'msg_session_invalid')
        return redirect('accounts:profile')

    if session_key == current_session_key:
        messages.error(request, 'msg_session_current_block')
        return redirect('accounts:profile')

    target_session = Session.objects.filter(session_key=session_key, expire_date__gte=timezone.now()).first()
    if not target_session:
        messages.error(request, 'msg_session_not_found')
        return redirect('accounts:profile')

    try:
        decoded = target_session.get_decoded()
    except Exception:
        messages.error(request, 'msg_session_not_found')
        return redirect('accounts:profile')

    if str(decoded.get('_auth_user_id', '')) != str(request.user.id):
        messages.error(request, 'msg_session_access_denied')
        return redirect('accounts:profile')

    target_session.delete()
    messages.success(request, 'msg_session_closed')
    return redirect('accounts:profile')


@login_required
@require_POST
def terminate_other_sessions(request):
    """Terminate all active sessions for the current user except the current one."""
    current_session_key = request.session.session_key
    closed_count = 0

    for session in Session.objects.filter(expire_date__gte=timezone.now()):
        if session.session_key == current_session_key:
            continue

        try:
            decoded = session.get_decoded()
        except Exception:
            continue

        if str(decoded.get('_auth_user_id', '')) != str(request.user.id):
            continue

        session.delete()
        closed_count += 1

    messages.success(request, f'msg_other_sessions_ended*{closed_count}')
    return redirect('accounts:profile')


@login_required
@require_POST
def update_profile(request):
    """Handle username, email, and streak goal updates."""
    form = ProfileUpdateForm(request.POST, instance=request.user)
    streak_goal = request.POST.get('streak_goal')

    if form.is_valid():
        form.save()
        
        if streak_goal and streak_goal.isdigit():
            val = int(streak_goal)
            if val > 0:
                request.user.profile.streak_goal = val
                request.user.profile.save(update_fields=['streak_goal'])
                
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
    if request.user.has_usable_password():
        form = PasswordChangeForm(request.user, request.POST)
    else:
        form = SetPasswordForm(request.user, request.POST)

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
def disconnect_google(request):
    """Detach Google social account from the current user."""
    google_account = request.user.socialaccount_set.filter(provider='google').first()
    if not google_account:
        messages.error(request, 'msg_google_not_linked')
        return redirect('accounts:profile')

    has_other_social = request.user.socialaccount_set.exclude(provider='google').exists()
    if not request.user.has_usable_password() and not has_other_social:
        messages.error(request, 'msg_set_password_before_disconnect')
        return redirect('accounts:profile')

    google_account.delete()
    messages.success(request, 'msg_google_disconnected')
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
                return JsonResponse({'status': 'error', 'message': 'msg_avatar_file_too_large'}, status=400)
            messages.error(request, 'msg_avatar_file_too_large')
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
                return JsonResponse({'status': 'error', 'message': 'msg_avatar_invalid_image'}, status=400)
            messages.error(request, 'msg_avatar_invalid_image')
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
            return JsonResponse({'status': 'error', 'message': 'msg_avatar_invalid'}, status=400)
        messages.error(request, 'msg_avatar_invalid')

    return redirect('accounts:profile')


@login_required
@require_POST
def delete_account(request):
    """Handle account deletion with password confirmation."""
    user = request.user

    # For users with a usable password (manual registration), verify it
    if user.has_usable_password():
        password = request.POST.get('password', '')
        if not user.check_password(password):
            messages.error(request, 'msg_delete_wrong_pw')
            return redirect('accounts:profile')

    # Log out first, then delete
    logout(request)
    user.delete()

    return redirect('accounts:login')


@require_POST
def set_language(request):
    """Handle language preference change for both guests and authenticated users."""
    lang = request.POST.get('language', '')
    valid_langs = [choice[0] for choice in UserProfile.LANGUAGE_CHOICES]

    if lang in valid_langs:
        if request.user.is_authenticated:
            profile = request.user.profile
            profile.language = lang
            profile.save()

        # Store in session for immediate effect (for both guests and users)
        request.session['django_language'] = lang

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'ok', 'language': lang})

        messages.success(request, 'msg_lang_success')
    else:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'message': 'Invalid language'}, status=400)
        messages.error(request, 'msg_lang_invalid')

    return redirect('accounts:profile')
