# CEFR English Exam Preparation Platform

A Django web application for practicing CEFR (B2) English listening exams with automated scoring, OCR-powered question ingestion, and progress tracking.

## Features

- **Automated Ingestion** — OCR-based management command that parses local exam materials (audio, answer keys, question screenshots) and populates the database
- **Smart Grading** — Weighted scoring per part (2.0–4.0 pts/question), text normalization for fill-in-the-blank answers (case-insensitive, punctuation-tolerant)
- **Multi-format Questions** — Multiple choice, fill-in-the-blank, map labeling, and speaker matching
- **Django Admin** — Side-by-side image preview + OCR text for manual correction
- **Database Integrity** — All test submissions wrapped in `transaction.atomic()`

## Tech Stack

- **Backend:** Python 3.13, Django 4.2
- **Database:** PostgreSQL (with `python-dotenv` for configuration)
- **OCR:** tesserocr / pytesseract + Pillow
- **Frontend:** Django Templates, Tailwind CSS (CDN), Phosphor Icons
- **Auth:** Standard Django Auth + `django-allauth` (Google OAuth)

## Quick Start

```bash
# Clone
git clone https://github.com/FirdavsiyT/cefr_preparer.git
cd cefr_preparer

# Install dependencies (including python-dotenv)
pip install -r requirements.txt
# Alternatively: pip install Pillow tesserocr psycopg2-binary python-dotenv django-allauth

# Configure Environment Variables
# Copy the example file and fill in your PostgreSQL and Gmail credentials
cp .env.example .env

# Download Tesseract training data
mkdir -p tessdata
curl -fsSL -o tessdata/eng.traineddata \
  https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata

# Migrate database
python manage.py migrate

# Create admin user
python manage.py createsuperuser

# Place exam materials in materials/Test N/Listening/Part N/
# Then ingest:
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

## Scoring System

| Part | Type | Weight (pts/question) |
|------|------|-----------------------|
| 1 | Multiple Choice (A/B/C) | 2.0 |
| 2 | Fill in the Blank | 2.5 |
| 3 | Speaker Matching (A–F) | 3.0 |
| 4 | Map Labeling (A–H) | 3.0 |
| 5 | Multiple Choice (A/B/C) | 3.0 |
| 6 | Fill in the Blank (1 word) | 4.0 |

**Max score per test: 100 points**

## Features Highlight
- **OTP Verification:** Custom multi-step email verification for registration and "Forgot Password" scenarios.
- **Social Login:** Secure Google OAuth integration.
- **Dynamic Localization:** Fully customized i18n support in EN, RU, and UZ.
- **Mentor Panel:** Custom SaaS dashboard for automated test-generation and manual test administration.

## Project Status

- [x] Step 1: Data models & architecture
- [x] Step 2: OCR ingestion & admin interface
- [x] Step 3: User authentication (OTP Verification, Password Reset, Google Auth)
- [x] Step 4: Test-taking interface
- [x] Step 5: Scoring & results
- [x] Step 6: Polish & UX (Glassmorphism, i18n, Duolingo aesthetic)

## License

MIT
