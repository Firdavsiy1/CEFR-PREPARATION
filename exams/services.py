"""
exams/services.py — Service layer for analytics and recommendation logic.
 
Public API:
    get_skill_radar_data(user) -> dict
    get_recommendations(user, limit=5) -> dict | None
"""

from django.db.models import Avg, Case, Count, F, FloatField, Q, Value, When
from django.db.models.functions import Coalesce

from .models import Test, UserAnswer, UserAttempt, WritingSubmission, ReadingUserAnswer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Radar chart labels for each listening part and writing skill.
PART_LABELS = {
    1: "Part 1",
    2: "Part 2",
    3: "Part 3",
    4: "Part 4",
    5: "Part 5",
    6: "Part 6",
}

# CEFR level → numeric score (0–100) for writing radar axis.
CEFR_SCORE_MAP = {
    "A1": 20,
    "A2": 40,
    "B1": 60,
    "B2": 80,
    "C1": 100,
}


# ---------------------------------------------------------------------------
# Task 2.1 — Skill Radar Data
# ---------------------------------------------------------------------------

def get_skill_radar_data(user) -> dict:
    """
    Aggregate the user's average skill scores across all listening parts and
    writing, suitable for a Radar Chart.

    DB cost: 2 queries (1 for per-part listening, 1 for writing average).

    Returns::

        {
            "labels": ["Part 1", "Part 2", ..., "Part 6", "Writing"],
            "data":   [72.5, 40.0, 55.0, 60.0, 33.3, 80.0, 65.0],
        }

    Any skill with no data defaults to 0.
    """
    # ------------------------------------------------------------------
    # Query 1: Per-part listening accuracy across *all* completed attempts.
    # Single SQL JOIN: UserAnswer → Question → Part → Test → UserAttempt
    # annotate() groups rows in the DB; no Python-level loops for maths.
    # ------------------------------------------------------------------
    part_rows = (
        UserAnswer.objects
        .filter(
            attempt__user=user,
            attempt__completed_at__isnull=False,
            attempt__test__test_type="listening",
        )
        .values("question__part__part_number")
        .annotate(
            total=Count("id"),
            correct=Count("id", filter=Q(is_correct=True)),
        )
        .order_by("question__part__part_number")
    )

    # Build a mapping  {part_number: accuracy_pct}  purely from DB aggregates.
    part_scores: dict[int, float] = {}
    for row in part_rows:
        part_num = row["question__part__part_number"]
        part_scores[part_num] = (
            round(row["correct"] / row["total"] * 100, 1)
            if row["total"] > 0
            else 0.0
        )

    # ------------------------------------------------------------------
    # Query 2: Average writing score across all graded submissions.
    # CASE/WHEN translates the CEFR label to a numeric value inside SQL,
    # so Avg() operates on integers, not Python objects.
    # ------------------------------------------------------------------
    writing_result = (
        WritingSubmission.objects
        .filter(
            attempt__user=user,
            attempt__completed_at__isnull=False,
            is_graded=True,
        )
        .aggregate(
            avg_score=Avg(
                Case(
                    *[
                        When(estimated_level=level, then=Value(score))
                        for level, score in CEFR_SCORE_MAP.items()
                    ],
                    default=Value(0),
                    output_field=FloatField(),
                )
            )
        )
    )
    writing_score = round(writing_result["avg_score"] or 0.0, 1)

    # ------------------------------------------------------------------
    # Query 3: Overall reading accuracy across all completed attempts.
    # ------------------------------------------------------------------
    reading_result = (
        ReadingUserAnswer.objects
        .filter(
            attempt__user=user,
            attempt__completed_at__isnull=False,
            attempt__test__test_type="reading",
        )
        .aggregate(
            total=Count("id"),
            correct=Count("id", filter=Q(is_correct=True)),
        )
    )
    reading_total = reading_result["total"] or 0
    reading_correct = reading_result["correct"] or 0
    reading_score = round(reading_correct / reading_total * 100, 1) if reading_total > 0 else 0.0

    # Assemble the final payload: parts 1–6 in order, then writing, then reading.
    labels = [PART_LABELS[p] for p in range(1, 7)] + ["Writing", "Reading"]
    data = [part_scores.get(p, 0.0) for p in range(1, 7)] + [writing_score, reading_score]

    return {"labels": labels, "data": data}


# ---------------------------------------------------------------------------
# Task 2.2 — Recommendation Engine
# ---------------------------------------------------------------------------

def get_recommendations(user, limit: int = 5) -> dict | None:
    """
    Identify the user's weakest skill and return up to *limit* unattempted
    tests that target that skill.

    Algorithm:
      1. Re-run the per-part accuracy query (same as get_skill_radar_data).
      2. Find the part with the lowest accuracy ratio (min correct/total).
         If the user has no listening history at all, fall back to the weakest
         writing score or, as a last resort, any unattempted listening test.
      3. Build a lazy subquery of the user's completed test IDs.
      4. Fetch micro-tests for the weak part first (name contains 'Part N').
         Fall back to full unattempted listening tests if fewer than *limit*
         results are found.

    DB cost:
      • 1 query for per-part accuracy (shared logic with radar)
      • 1 lazy subquery (passed to the DB as a sub-SELECT, not evaluated)
      • 1 query to fetch recommended tests

    Returns::

        {
            "skill": "Part 3",
            "weakest_part": 3,       # int part number, or None for Writing
            "tests": <QuerySet[Test]>,
        }

    Returns None when the user has completed all available tests or there is
    nothing to recommend.
    """
    # ------------------------------------------------------------------
    # Step 1: Per-part accuracy (same DB structure as radar data).
    # ------------------------------------------------------------------
    part_rows = list(
        UserAnswer.objects
        .filter(
            attempt__user=user,
            attempt__completed_at__isnull=False,
            attempt__test__test_type="listening",
        )
        .values("question__part__part_number")
        .annotate(
            total=Count("id"),
            correct=Count("id", filter=Q(is_correct=True)),
        )
        .order_by("question__part__part_number")
    )

    # ------------------------------------------------------------------
    # Step 2: Identify the weakest part.
    # ------------------------------------------------------------------
    weakest_part: int | None = None

    if part_rows:
        weakest_row = min(
            part_rows,
            key=lambda r: (r["correct"] / r["total"]) if r["total"] > 0 else 0.0,
        )
        weakest_part = weakest_row["question__part__part_number"]
    else:
        # No listening history — recommend Part 1 as a sensible starting point.
        weakest_part = 1

    skill_label = PART_LABELS.get(weakest_part, f"Part {weakest_part}")

    # ------------------------------------------------------------------
    # Step 3: Lazy subquery — IDs of tests the user has already completed.
    # This is NOT evaluated in Python; Django passes it as a sub-SELECT
    # to the DB when the outer .exclude() query runs.
    # ------------------------------------------------------------------
    attempted_test_ids = (
        UserAttempt.objects
        .filter(user=user, completed_at__isnull=False)
        .values("test_id")
    )

    # ------------------------------------------------------------------
    # Step 4: Fetch micro-tests targeting the weakest part first.
    # Micro-tests have names like "Test 1 - Part 3 Listening".
    # ------------------------------------------------------------------
    micro_tests = list(
        Test.objects
        .filter(
            is_active=True,
            test_type="listening",
            name__icontains=f"Part {weakest_part}",
        )
        .exclude(id__in=attempted_test_ids)
        .order_by("name")[:limit]
    )

    if len(micro_tests) >= limit:
        recommended = micro_tests
    else:
        # Supplement / fall back with full unattempted listening tests.
        full_tests = list(
            Test.objects
            .filter(
                is_active=True,
                test_type="listening",
            )
            .exclude(name__icontains=" - Part ")  # exclude micro-tests
            .exclude(id__in=attempted_test_ids)
            .order_by("name")[: limit - len(micro_tests)]
        )
        recommended = micro_tests + full_tests

    if not recommended:
        return None

    return {
        "skill": skill_label,
        "weakest_part": weakest_part,
        "tests": recommended,
    }
