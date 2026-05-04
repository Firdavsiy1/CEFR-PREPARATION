"""
Tests for the CEFR Exams application — grading logic, model behavior,
and key view smoke tests.
"""

from django.contrib.auth.models import User, Group
from django.test import TestCase, Client
from django.urls import reverse
from unittest.mock import patch, MagicMock

from .models import normalize_answer, check_answer, Test, Part, Question, Choice, UserAttempt
from .speaking_services import process_speaking_page


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


# ---------------------------------------------------------------------------
# View smoke tests
# ---------------------------------------------------------------------------

class DashboardViewTests(TestCase):
    """Smoke tests for the student dashboard."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='student1', password='pass1234!')
        cls.test = Test.objects.create(name='Listen Test 1', test_type='listening', is_active=True)

    def setUp(self):
        self.client = Client()
        self.client.login(username='student1', password='pass1234!')

    def test_dashboard_redirects_anonymous(self):
        self.client.logout()
        resp = self.client.get(reverse('exams:dashboard'))
        self.assertIn(resp.status_code, [302, 301])

    def test_dashboard_home_ok(self):
        resp = self.client.get(reverse('exams:dashboard'))
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_category_ok(self):
        resp = self.client.get(reverse('exams:dashboard_category', args=['listening']))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Listen Test 1')

    def test_dashboard_search_filters(self):
        Test.objects.create(name='Reading Practice', test_type='reading', is_active=True)
        resp = self.client.get(reverse('exams:dashboard_category', args=['listening']) + '?q=Listen')
        self.assertContains(resp, 'Listen Test 1')
        self.assertNotContains(resp, 'Reading Practice')

    def test_soft_deleted_test_hidden(self):
        t = Test.objects.create(name='Deleted Test', test_type='listening', is_active=False, is_deleted=True)
        resp = self.client.get(reverse('exams:dashboard_category', args=['listening']))
        self.assertNotContains(resp, 'Deleted Test')


class HistoryViewTests(TestCase):
    """Tests for the exam history view."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='student2', password='pass1234!')
        cls.test = Test.objects.create(name='History Test', test_type='listening', is_active=True)

    def setUp(self):
        self.client = Client()
        self.client.login(username='student2', password='pass1234!')

    def test_history_home_ok(self):
        resp = self.client.get(reverse('exams:history'))
        self.assertEqual(resp.status_code, 200)

    def test_history_category_ok(self):
        resp = self.client.get(reverse('exams:history_category', args=['listening']))
        self.assertEqual(resp.status_code, 200)

    def test_history_category_shows_attempts(self):
        from django.utils import timezone
        attempt = UserAttempt.objects.create(
            user=self.user,
            test=self.test,
            completed_at=timezone.now(),
            total_correct=5,
            total_questions=10,
            total_score=10.0,
            max_possible_score=20.0,
        )
        resp = self.client.get(reverse('exams:history_category', args=['listening']))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attempts', resp.context)

    def test_history_pagination_context(self):
        from django.utils import timezone
        for i in range(25):
            UserAttempt.objects.create(
                user=self.user,
                test=self.test,
                completed_at=timezone.now(),
                total_correct=i,
                total_questions=35,
                total_score=float(i),
                max_possible_score=100.0,
            )
        resp = self.client.get(reverse('exams:history_category', args=['listening']))
        self.assertIn('page_obj', resp.context)
        self.assertEqual(resp.context['page_obj'].paginator.per_page, 20)


class AutosaveThrottleTests(TestCase):
    """Tests for the autosave throttle."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='student3', password='pass1234!')
        cls.test = Test.objects.create(name='Autosave Test', test_type='listening', is_active=True)

    def setUp(self):
        self.client = Client()
        self.client.login(username='student3', password='pass1234!')
        from django.core.cache import cache
        cache.clear()

    def test_autosave_ok(self):
        from django.utils import timezone
        attempt = UserAttempt.objects.create(user=self.user, test=self.test)
        url = reverse('exams:api_autosave', args=[attempt.id])
        resp = self.client.post(url, '{"answers": {}}', content_type='application/json')
        self.assertEqual(resp.status_code, 200)

    def test_autosave_throttled_on_rapid_requests(self):
        from django.utils import timezone
        attempt = UserAttempt.objects.create(user=self.user, test=self.test)
        url = reverse('exams:api_autosave', args=[attempt.id])
        self.client.post(url, '{"answers": {}}', content_type='application/json')
        # Second immediate request should be throttled
        resp = self.client.post(url, '{"answers": {}}', content_type='application/json')
        self.assertEqual(resp.status_code, 429)


class SpeakingOcrParsingTests(TestCase):
    """Tests for speaking OCR parsing heuristics (via Gemini mock)."""

    @patch('exams.speaking_services._build_gemini_client')
    def test_extracts_multiple_parts_via_gemini(self, mock_client):
        # Setup mock genai client
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '''```json
{
  "parts": [
    {"part_number": 2, "questions": [{"q_num": 1, "text": "What can you see?"}]}
  ]
}
```'''
        mock_instance.models.generate_content.return_value = mock_response
        mock_client.return_value = mock_instance

        data = process_speaking_page(b'fake-image-bytes')
        parts = data['parts']

        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['part_number'], 2)
        self.assertEqual(parts[0]['questions'][0]['text'], "What can you see?")

    @patch('exams.speaking_services._build_gemini_client')
    def test_handles_gemini_error_gracefully(self, mock_client):
        mock_client.side_effect = RuntimeError("Credentials not found")

        data = process_speaking_page(b'fake-image-bytes')
        
        self.assertEqual(data['status'], 'error')

    @patch('exams.speaking_services._build_gemini_client')
    def test_extracts_debate_table_for_part3(self, mock_client):
        """Part 3 with debate_table should have empty questions and debate data."""
        mock_instance = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '''{
  "parts": [
    {
      "part_number": "1.1",
      "instructions": "Tell me about yourself",
      "questions": [{"q_num": 1, "text": "Tell me about your family"}]
    },
    {
      "part_number": "1.2",
      "instructions": "Describe the pictures",
      "questions": [{"q_num": 1, "text": "What do you see in these pictures?"}]
    },
    {
      "part_number": "2",
      "instructions": "Describe the picture",
      "questions": [
        {"q_num": 1, "text": "Tell me about a time you were late."},
        {"q_num": 2, "text": "What made you late?"}
      ]
    },
    {
      "part_number": "3",
      "instructions": "Discussion / Debate",
      "debate_table": {
        "topic": "The government is responsible for providing health care",
        "for_points": ["Health is a fundamental right", "Good hospitals are governmental responsibility"],
        "against_points": ["Health is the responsibility of the individual"]
      },
      "questions": []
    }
  ]
}'''
        mock_instance.models.generate_content.return_value = mock_response
        mock_client.return_value = mock_instance

        data = process_speaking_page(b'fake-image-bytes')
        parts = data['parts']

        self.assertEqual(len(parts), 4)

        # Part 1.1 — interview questions
        self.assertEqual(parts[0]['part_number'], '1.1')
        self.assertEqual(len(parts[0]['questions']), 1)

        # Part 1.2 — picture questions
        self.assertEqual(parts[1]['part_number'], '1.2')
        self.assertEqual(len(parts[1]['questions']), 1)

        # Part 2 — image + questions
        self.assertEqual(parts[2]['part_number'], '2')
        self.assertEqual(len(parts[2]['questions']), 2)
        self.assertEqual(parts[2]['questions'][0]['text'], "Tell me about a time you were late.")

        # Part 3 — debate table, NO questions
        self.assertEqual(parts[3]['part_number'], '3')
        self.assertEqual(len(parts[3]['questions']), 0)
        self.assertIn('debate_table', parts[3])
        dt = parts[3]['debate_table']
        self.assertEqual(dt['topic'], "The government is responsible for providing health care")
        self.assertEqual(len(dt['for_points']), 2)
        self.assertEqual(len(dt['against_points']), 1)


class SpeakingPartNumberCoercionTests(TestCase):
    """Tests for _coerce_speaking_part_number remapping logic."""

    def test_basic_mapping(self):
        from exams.mentor_views import _coerce_speaking_part_number
        self.assertEqual(_coerce_speaking_part_number('1', 1), 1)
        self.assertEqual(_coerce_speaking_part_number('1.1', 1), 1)
        self.assertEqual(_coerce_speaking_part_number('1.2', 2), 2)

    def test_avoids_collision(self):
        from exams.mentor_views import _coerce_speaking_part_number
        used = {1, 2}
        # OCR label '2' should get remapped to 3 since 1 and 2 are taken
        result = _coerce_speaking_part_number('2', 3, used_numbers=used)
        self.assertEqual(result, 3)

    def test_sequential_allocation(self):
        """Simulates processing 1.1, 1.2, 2, 3 — all should get unique numbers."""
        from exams.mentor_views import _coerce_speaking_part_number
        used = set()
        results = []
        for raw, fb in [('1.1', 1), ('1.2', 2), ('2', 3), ('3', 4)]:
            num = _coerce_speaking_part_number(raw, fb, used)
            used.add(num)
            results.append(num)
        # Should be 1, 2, 3, 4 with no collisions
        self.assertEqual(len(set(results)), 4)
        self.assertTrue(all(1 <= n <= 4 for n in results))

