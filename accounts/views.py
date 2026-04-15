from django.shortcuts import render, redirect
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login
from django.contrib import messages

def register_view(request):
    """View for user registration."""
    # Redirect if already logged in
    if request.user.is_authenticated:
        return redirect('exams:dashboard')
        
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, f"Registration successful. Welcome, {user.username}!")
            return redirect('exams:dashboard')
        else:
            messages.error(request, "Registration failed. Please correct the errors below.")
    else:
        form = UserCreationForm()
        
    return render(request, 'accounts/register.html', {'form': form})
