# CEFR English Exam Preparation Platform

A full-featured Django SaaS platform for practicing CEFR (B2) English — covering Listening, Reading, Writing, and Speaking — with AI-powered feedback, mentor dashboards, classrooms, video lessons, and progress tracking.

## Features

### Core Exam Modules
- **Listening** — OCR-ingested audio tests with multiple choice, fill-in-the-blank, map labeling, and speaker matching
- **Reading** — Multi-passage reading tests with auto-grading and highlight-based annotations
- **Writing** — Timed writing tasks with auto-save drafts and mentor manual grading
- **Speaking** — AI-scored speaking tests with debate-style prompts and multilingual JSON feedback

### Platform Features
- **Mentor Panel** — Full SaaS dashboard: create/clone/delete tests, grade submissions, manage classrooms and users
- **Classrooms** — Mentor-created classes with multi-membership, assignments, and student notifications
- **Video Lessons** — Embedded video lessons with topic tags, quiz questions, and live video rooms (host/join)
- **AI Chat** — In-app AI assistant for language learning support
- **Dictionary** — Built-in word lookup with `WordCache` and `DictionaryEntry` models, alternative form support
- **Arcade** — Gamified practice section (quizzes, stories, video)
- **Streak & Goals** — Daily activity tracking, streak history, and user-defined streak goals
- **Role-based Access** — Mentor and student roles with custom `allauth` adapter
- **Rate Limiting** — IP-based rate limiting for auth endpoints
- **Async Tasks** — Celery + Redis for background processing (email, TTS, AI scoring)
- **TTS Service** — Text-to-speech generation for listening content
- **Multilingual UI** — Full i18n in English, Russian, and Uzbek

### Auth & Security
- OTP email verification for registration and password reset
- Google OAuth via `django-allauth`
- Tab-blur event tracking (anti-cheat)
- All submissions wrapped in `transaction.atomic()`

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.13, Django 4.2 |
| Database | PostgreSQL |
| Task Queue | Celery + Redis |
| OCR | tesserocr / pytesseract + Pillow |
| AI / LLM | OpenAI API (feedback, chat) |
| TTS | Custom TTS service |
| Frontend | Django Templates, Tailwind CSS (CDN), Phosphor Icons |
| Auth | Django Auth + `django-allauth` (Google OAuth) |
| Config | `python-dotenv` |

## Quick Start

```bash
# Clone
git clone https://github.com/FirdavsiyT/cefr_preparer.git
cd cefr_preparer

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Fill in PostgreSQL, Gmail, Redis, and OpenAI credentials

# Download Tesseract training data
mkdir -p tessdata
curl -fsSL -o tessdata/eng.traineddata \
  https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata

# Migrate database
python manage.py migrate

# Create admin user
python manage.py createsuperuser

# Start Celery worker (requires Redis)
celery -A cefr_project worker -l info

# Ingest listening materials
python manage.py ingest_materials

# Run development server
python manage.py runserver
```

## Materials Directory Structure

```
materials/
└── Test 1/
    └── Listening/
        ├── Part 1/
        │   ├── audio.mp3
        │   ├── questions.jpg
        │   └── answers.txt      # Format: "1 A\n2 C\n3 B"
        ├── Part 2/
        │   └── ...
        ├── Part 4/
        │   ├── audio.mp3
        │   ├── questions.jpg
        │   ├── map.jpg           # Supplementary map image
        │   └── answers.txt
        └── ...
```

## Listening Scoring System

| Part | Type | Weight (pts/question) |
|------|------|-----------------------|
| 1 | Multiple Choice (A/B/C) | 2.0 |
| 2 | Fill in the Blank | 2.5 |
| 3 | Speaker Matching (A–F) | 3.0 |
| 4 | Map Labeling (A–H) | 3.0 |
| 5 | Multiple Choice (A/B/C) | 3.0 |
| 6 | Fill in the Blank (1 word) | 4.0 |

**Max score per listening test: 100 points**

## Project Status

- [x] Step 1: Data models & architecture
- [x] Step 2: OCR ingestion & admin interface
- [x] Step 3: User authentication (OTP, Password Reset, Google OAuth)
- [x] Step 4: Listening test-taking interface & scoring
- [x] Step 5: Reading module (passages, questions, auto-grading)
- [x] Step 6: Writing module (tasks, submissions, mentor grading)
- [x] Step 7: Speaking module (AI feedback, debate prompts)
- [x] Step 8: Mentor panel & classroom management
- [x] Step 9: Video lessons & live video rooms
- [x] Step 10: AI chat, dictionary, arcade, and streak system
- [x] Step 11: Celery async tasks, TTS, rate limiting, multilingual i18n
- [x] Step 6: Polish & UX (Glassmorphism, i18n, Duolingo aesthetic)

