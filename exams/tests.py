"""
Tests for the CEFR Exams application — grading logic and model behavior.
"""

from django.test import TestCase

from .models import normalize_answer, check_answer, Test, Part, Question, Choice


class NormalizeAnswerTests(TestCase):
    """Test the normalize_answer() utility function."""

    def test_strips_whitespace(self):
        self.assertEqual(normalize_answer("  tent  "), "tent")

    def test_collapses_internal_spaces(self):
        self.assertEqual(normalize_answer("over  fishing"), "over fishing")

    def test_lowercases(self):
        self.assertEqual(normalize_answer("Tent"), "tent")

    def test_removes_punctuation(self):
        self.assertEqual(normalize_answer("tent."), "tent")
        self.assertEqual(normalize_answer("tent,"), "tent")
        self.assertEqual(normalize_answer("'tent'"), "tent")

    def test_normalizes_unicode(self):
        self.assertEqual(normalize_answer("café"), "cafe")

    def test_empty_string(self):
        self.assertEqual(normalize_answer(""), "")

    def test_combined_normalization(self):
        self.assertEqual(
            normalize_answer("  Over Fishing!!  "),
            "over fishing",
        )


class CheckAnswerTests(TestCase):
    """Test the check_answer() function for different question types."""

    def test_multiple_choice_case_insensitive(self):
        self.assertTrue(check_answer("a", "A", "multiple_choice"))
        self.assertTrue(check_answer("B", "b", "multiple_choice"))

    def test_multiple_choice_wrong(self):
        self.assertFalse(check_answer("A", "B", "multiple_choice"))

    def test_fill_blank_exact(self):
        self.assertTrue(check_answer("tent", "tent", "fill_blank"))

    def test_fill_blank_case_insensitive(self):
        self.assertTrue(check_answer("Tent", "tent", "fill_blank"))

    def test_fill_blank_extra_spaces(self):
        self.assertTrue(check_answer("  tent  ", "tent", "fill_blank"))

    def test_fill_blank_punctuation(self):
        self.assertTrue(check_answer("tent.", "tent", "fill_blank"))

    def test_fill_blank_wrong(self):
        self.assertFalse(check_answer("tents", "tent", "fill_blank"))

    def test_map_label(self):
        self.assertTrue(check_answer("c", "C", "map_label"))
        self.assertFalse(check_answer("A", "C", "map_label"))


class QuestionModelTests(TestCase):
    """Test Question model behavior."""

    @classmethod
    def setUpTestData(cls):
        cls.test = Test.objects.create(name="Test 1", test_type="listening")
        cls.part = Part.objects.create(
            test=cls.test, part_number=2, audio_file="exams/audio/test.mp3",
        )
        cls.question = Question.objects.create(
            part=cls.part,
            question_number=1,
            question_type="fill_blank",
            correct_answer="tent",
        )

    def test_check_answer_correct(self):
        self.assertTrue(self.question.check_answer("tent"))
        self.assertTrue(self.question.check_answer("  Tent  "))

    def test_check_answer_wrong(self):
        self.assertFalse(self.question.check_answer("tents"))


class PartWeightTests(TestCase):
    """Test that Part auto-sets the scoring weight on save."""

    @classmethod
    def setUpTestData(cls):
        cls.test = Test.objects.create(name="Test Weight", test_type="listening")

    def test_weights_auto_set(self):
        expected = {1: 2.0, 2: 2.5, 3: 3.0, 4: 3.0, 5: 3.0, 6: 4.0}
        for part_num, weight in expected.items():
            part = Part.objects.create(
                test=self.test,
                part_number=part_num,
                audio_file="exams/audio/test.mp3",
            )
            self.assertEqual(
                part.points_per_question, weight,
                f"Part {part_num} should have weight {weight}",
            )
