"""
Tests for the Mentor Panel — access control, ownership enforcement, CRUD APIs,
toggle/publish correctness, speaking concurrency safeguards, soft-delete protection,
and task status polling.
"""
import json

from django.contrib.auth.models import User, Group
from django.test import TestCase, Client
from django.urls import reverse

from .models import (
    Test, Part, Question, Choice,
    ReadingTest, ReadingPart,
    SpeakingPart, SpeakingQuestion,
    IngestionTask,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mentor(username='mentor1', password='pass1234!'):
    """Create a user in the Mentors group."""
    user = User.objects.create_user(
        username=username,
        email=f'{username}@test.example',
        password=password,
    )
    group, _ = Group.objects.get_or_create(name='Mentors')
    user.groups.add(group)
    return user


def _make_superuser(username='admin1', password='pass1234!'):
    return User.objects.create_superuser(
        username=username,
        email=f'{username}@test.example',
        password=password,
    )


def _make_student(username='student_x', password='pass1234!'):
    return User.objects.create_user(
        username=username,
        email=f'{username}@test.example',
        password=password,
    )


# ---------------------------------------------------------------------------
# Access control — mentor_required decorator
# ---------------------------------------------------------------------------

class MentorRequiredDecoratorTests(TestCase):
    """Anonymous → 302 to login; student → 403; mentor → 200; superuser → 200."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('req_m')
        cls.student = _make_student('req_s')

    def test_anon_redirected_to_login(self):
        resp = self.client.get(reverse('exams:mentor_dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_student_gets_403(self):
        self.client.force_login(self.student)
        resp = self.client.get(reverse('exams:mentor_dashboard'))
        self.assertEqual(resp.status_code, 403)

    def test_mentor_gets_200(self):
        self.client.force_login(self.mentor)
        resp = self.client.get(reverse('exams:mentor_dashboard'))
        self.assertEqual(resp.status_code, 200)

    def test_superuser_gets_200(self):
        su = _make_superuser('req_su')
        self.client.force_login(su)
        resp = self.client.get(reverse('exams:mentor_dashboard'))
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Ownership helper
# ---------------------------------------------------------------------------

class CanManageTestTests(TestCase):
    """can_manage_test: mentor owns own tests only; superuser owns all."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor_a = _make_mentor('own_mA')
        cls.mentor_b = _make_mentor('own_mB')
        cls.su = _make_superuser('own_su')
        cls.test_a = Test.objects.create(name='A', test_type='listening', author=cls.mentor_a)
        cls.test_b = Test.objects.create(name='B', test_type='listening', author=cls.mentor_b)

    def test_mentor_can_manage_own_test(self):
        from exams.mentor_views import can_manage_test
        self.assertTrue(can_manage_test(self.mentor_a, self.test_a))

    def test_mentor_cannot_manage_others_test(self):
        from exams.mentor_views import can_manage_test
        self.assertFalse(can_manage_test(self.mentor_a, self.test_b))

    def test_superuser_can_manage_any_test(self):
        from exams.mentor_views import can_manage_test
        self.assertTrue(can_manage_test(self.su, self.test_b))


# ---------------------------------------------------------------------------
# Dashboard visibility
# ---------------------------------------------------------------------------

class MentorDashboardTests(TestCase):
    """Dashboard: own tests visible; other mentor's tests hidden; deleted tests hidden."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('dash_m')
        cls.other = _make_mentor('dash_o')
        cls.su = _make_superuser('dash_su')
        cls.own_test = Test.objects.create(
            name='My Test', test_type='listening', author=cls.mentor, is_active=True
        )
        cls.other_test = Test.objects.create(
            name='Other Test', test_type='listening', author=cls.other, is_active=True
        )
        cls.deleted_test = Test.objects.create(
            name='Dead Test', test_type='listening', author=cls.mentor, is_deleted=True
        )

    def test_mentor_sees_own_test(self):
        self.client.force_login(self.mentor)
        resp = self.client.get(reverse('exams:mentor_dashboard'))
        self.assertContains(resp, 'My Test')

    def test_mentor_does_not_see_other_test(self):
        self.client.force_login(self.mentor)
        resp = self.client.get(reverse('exams:mentor_dashboard'))
        self.assertNotContains(resp, 'Other Test')

    def test_mentor_does_not_see_deleted_test(self):
        self.client.force_login(self.mentor)
        resp = self.client.get(reverse('exams:mentor_dashboard'))
        self.assertNotContains(resp, 'Dead Test')

    def test_superuser_sees_all_non_deleted(self):
        self.client.force_login(self.su)
        resp = self.client.get(reverse('exams:mentor_dashboard'))
        self.assertContains(resp, 'My Test')
        self.assertContains(resp, 'Other Test')
        self.assertNotContains(resp, 'Dead Test')


# ---------------------------------------------------------------------------
# Builder views — soft-delete protection
# ---------------------------------------------------------------------------

class BuilderSoftDeleteTests(TestCase):
    """All builder views return 404 for soft-deleted tests."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('bld_m')
        cls.dead_l = Test.objects.create(
            name='Dead L', test_type='listening', author=cls.mentor, is_deleted=True
        )
        cls.dead_r = Test.objects.create(
            name='Dead R', test_type='reading', author=cls.mentor, is_deleted=True
        )
        cls.dead_s = Test.objects.create(
            name='Dead S', test_type='speaking', author=cls.mentor, is_deleted=True
        )

    def setUp(self):
        self.client.force_login(self.mentor)

    def test_listening_builder_404_on_deleted(self):
        resp = self.client.get(reverse('exams:mentor_test_builder', args=[self.dead_l.id]))
        self.assertEqual(resp.status_code, 404)

    def test_reading_builder_404_on_deleted(self):
        resp = self.client.get(reverse('exams:mentor_reading_builder', args=[self.dead_r.id]))
        self.assertEqual(resp.status_code, 404)

    def test_speaking_builder_404_on_deleted(self):
        resp = self.client.get(reverse('exams:mentor_speaking_builder', args=[self.dead_s.id]))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Builder views — ownership
# ---------------------------------------------------------------------------

class BuilderOwnershipTests(TestCase):
    """Mentor A cannot open Mentor B's builder pages."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor_a = _make_mentor('bown_mA')
        cls.mentor_b = _make_mentor('bown_mB')
        cls.test = Test.objects.create(name='B Test', test_type='listening', author=cls.mentor_b)

    def setUp(self):
        self.client.force_login(self.mentor_a)

    def test_cannot_open_others_builder(self):
        resp = self.client.get(reverse('exams:mentor_test_builder', args=[self.test.id]))
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Builder routing — speaking and reading redirect via mentor_test_builder
# ---------------------------------------------------------------------------

class BuilderRoutingTests(TestCase):
    """mentor_test_builder redirects reading→reading_builder, speaking→speaking_builder."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('rout_m')
        cls.reading_test = Test.objects.create(name='R', test_type='reading', author=cls.mentor)
        cls.speaking_test = Test.objects.create(name='S', test_type='speaking', author=cls.mentor)

    def setUp(self):
        self.client.force_login(self.mentor)

    def test_reading_redirects_to_reading_builder(self):
        resp = self.client.get(reverse('exams:mentor_test_builder', args=[self.reading_test.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('reading-builder', resp['Location'])

    def test_speaking_redirects_to_speaking_builder(self):
        resp = self.client.get(reverse('exams:mentor_test_builder', args=[self.speaking_test.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('speaking', resp['Location'])


# ---------------------------------------------------------------------------
# api_toggle_test_active
# ---------------------------------------------------------------------------

class ToggleTestActiveTests(TestCase):
    """Toggle: flips is_active; returns JSON with is_active; 403 for non-owner; 405 for GET."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('tog_m')
        cls.other = _make_mentor('tog_o')
        cls.test = Test.objects.create(
            name='Toggle Me', test_type='listening', author=cls.mentor, is_active=False
        )

    def _post(self, user, test_id):
        self.client.force_login(user)
        return self.client.post(
            reverse('exams:api_toggle_test_active', args=[test_id]),
            content_type='application/json',
        )

    def test_toggle_false_to_true(self):
        self.test.is_active = False
        self.test.save()
        resp = self._post(self.mentor, self.test.id)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('is_active', data)
        self.assertTrue(data['is_active'])
        self.test.refresh_from_db()
        self.assertTrue(self.test.is_active)

    def test_toggle_true_to_false(self):
        self.test.is_active = True
        self.test.save()
        resp = self._post(self.mentor, self.test.id)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data['is_active'])
        self.test.refresh_from_db()
        self.assertFalse(self.test.is_active)

    def test_toggle_is_idempotent_if_called_twice(self):
        """Calling toggle twice should return to original state."""
        original = self.test.is_active
        self._post(self.mentor, self.test.id)
        self._post(self.mentor, self.test.id)
        self.test.refresh_from_db()
        self.assertEqual(self.test.is_active, original)

    def test_forbidden_for_non_owner(self):
        resp = self._post(self.other, self.test.id)
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertIn('error', data)

    def test_get_returns_405(self):
        self.client.force_login(self.mentor)
        resp = self.client.get(reverse('exams:api_toggle_test_active', args=[self.test.id]))
        self.assertEqual(resp.status_code, 405)

    def test_response_has_no_error_key_on_success(self):
        resp = self._post(self.mentor, self.test.id)
        data = resp.json()
        self.assertNotIn('error', data)


# ---------------------------------------------------------------------------
# api_publish_test
# ---------------------------------------------------------------------------

class PublishTestTests(TestCase):
    """Publish: sets is_active=True; response has success+is_active; 403 for non-owner."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('pub_m')
        cls.other = _make_mentor('pub_o')
        cls.test = Test.objects.create(
            name='Publish Me', test_type='listening', author=cls.mentor, is_active=False
        )

    def _post(self, user, test_id, payload=None):
        self.client.force_login(user)
        return self.client.post(
            reverse('exams:api_publish_test', args=[test_id]),
            data=json.dumps(payload or {}),
            content_type='application/json',
        )

    def test_publish_activates_test(self):
        self.test.is_active = False
        self.test.save()
        resp = self._post(self.mentor, self.test.id)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get('success'))
        self.assertTrue(data.get('is_active'))
        self.test.refresh_from_db()
        self.assertTrue(self.test.is_active)

    def test_publish_forbidden_for_non_owner(self):
        resp = self._post(self.other, self.test.id)
        self.assertEqual(resp.status_code, 403)

    def test_publish_without_split_parts(self):
        resp = self._post(self.mentor, self.test.id, {'split_parts': False})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get('success'))


# ---------------------------------------------------------------------------
# api_test_data
# ---------------------------------------------------------------------------

class ApiTestDataTests(TestCase):
    """api_test_data: returns full structure for owner; 403 for non-owner."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('data_m')
        cls.other = _make_mentor('data_o')
        cls.test = Test.objects.create(name='Data Test', test_type='listening', author=cls.mentor)
        cls.part = Part.objects.create(test=cls.test, part_number=1, audio_file='x.mp3')
        cls.q = Question.objects.create(
            part=cls.part, question_number=1,
            question_type='fill_blank', correct_answer='x',
        )

    def test_owner_gets_full_structure(self):
        self.client.force_login(self.mentor)
        resp = self.client.get(reverse('exams:api_test_data', args=[self.test.id]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['id'], self.test.id)
        self.assertEqual(len(data['parts']), 1)
        self.assertEqual(len(data['parts'][0]['questions']), 1)

    def test_non_owner_forbidden(self):
        self.client.force_login(self.other)
        resp = self.client.get(reverse('exams:api_test_data', args=[self.test.id]))
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Part CRUD
# ---------------------------------------------------------------------------

class PartCrudTests(TestCase):
    """Part create/delete with ownership enforcement."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('pcrud_m')
        cls.other = _make_mentor('pcrud_o')
        cls.test = Test.objects.create(name='CRUD Test', test_type='listening', author=cls.mentor)

    def test_create_part_owner(self):
        self.client.force_login(self.mentor)
        resp = self.client.post(
            reverse('exams:api_create_part', args=[self.test.id]),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id', resp.json())
        self.assertEqual(Part.objects.filter(test=self.test).count(), 1)

    def test_create_part_forbidden_non_owner(self):
        self.client.force_login(self.other)
        resp = self.client.post(
            reverse('exams:api_create_part', args=[self.test.id]),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)

    def test_delete_part_owner(self):
        part = Part.objects.create(test=self.test, part_number=1, audio_file='x.mp3')
        self.client.force_login(self.mentor)
        resp = self.client.post(
            reverse('exams:api_delete_part', args=[part.id]),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Part.objects.filter(pk=part.id).exists())

    def test_delete_part_forbidden_non_owner(self):
        part = Part.objects.create(test=self.test, part_number=2, audio_file='x.mp3')
        self.client.force_login(self.other)
        resp = self.client.post(
            reverse('exams:api_delete_part', args=[part.id]),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Part.objects.filter(pk=part.id).exists())


# ---------------------------------------------------------------------------
# Speaking part creation
# ---------------------------------------------------------------------------

class SpeakingPartCreateTests(TestCase):
    """api_speaking_create_part: auto-numbers; max 4 enforced; ownership checked."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('sp_m')
        cls.other = _make_mentor('sp_o')
        cls.test = Test.objects.create(name='Spk Test', test_type='speaking', author=cls.mentor)

    def _create_part(self, user=None):
        self.client.force_login(user or self.mentor)
        return self.client.post(
            reverse('exams:api_speaking_create_part', args=[self.test.id]),
            content_type='application/json',
        )

    def test_first_part_gets_number_1(self):
        SpeakingPart.objects.filter(test=self.test).delete()
        resp = self._create_part()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['part_number'], 1)

    def test_auto_increments_numbers(self):
        SpeakingPart.objects.filter(test=self.test).delete()
        self._create_part()
        resp = self._create_part()
        self.assertEqual(resp.json()['part_number'], 2)

    def test_max_4_parts_enforced(self):
        SpeakingPart.objects.filter(test=self.test).delete()
        for i in range(1, 5):
            SpeakingPart.objects.create(test=self.test, part_number=i)
        resp = self._create_part()
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.json())

    def test_forbidden_for_non_owner(self):
        resp = self._create_part(self.other)
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# _coerce_speaking_part_number — ValueError when all slots full
# ---------------------------------------------------------------------------

class CoerceSpeakingPartEdgeCaseTests(TestCase):
    """_coerce_speaking_part_number raises ValueError when all 1-4 occupied."""

    def test_raises_when_all_slots_full(self):
        from exams.mentor_views import _coerce_speaking_part_number
        with self.assertRaises(ValueError):
            _coerce_speaking_part_number('2', fallback_number=2, used_numbers={1, 2, 3, 4})

    def test_raises_for_all_raw_values(self):
        from exams.mentor_views import _coerce_speaking_part_number
        for raw in ('1', '2', '3', '4', '1.1', '1.2'):
            with self.assertRaises(ValueError):
                _coerce_speaking_part_number(raw, fallback_number=1, used_numbers={1, 2, 3, 4})

    def test_no_raise_when_slot_available(self):
        from exams.mentor_views import _coerce_speaking_part_number
        # Should not raise; 4 is free
        result = _coerce_speaking_part_number('2', fallback_number=2, used_numbers={1, 2, 3})
        self.assertEqual(result, 4)


# ---------------------------------------------------------------------------
# Delete test (soft-delete)
# ---------------------------------------------------------------------------

class DeleteTestTests(TestCase):
    """Soft-delete: test is_deleted=True; non-owner gets 403; GET shows confirm page."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('del_m')
        cls.other = _make_mentor('del_o')

    def test_post_soft_deletes_own_test(self):
        test = Test.objects.create(name='Bye', test_type='listening', author=self.mentor)
        self.client.force_login(self.mentor)
        resp = self.client.post(reverse('exams:mentor_delete_test', args=[test.id]))
        self.assertEqual(resp.status_code, 302)
        test.refresh_from_db()
        self.assertTrue(test.is_deleted)
        self.assertFalse(test.is_active)

    def test_post_forbidden_for_non_owner(self):
        test = Test.objects.create(name='Keep', test_type='listening', author=self.mentor)
        self.client.force_login(self.other)
        resp = self.client.post(reverse('exams:mentor_delete_test', args=[test.id]))
        self.assertEqual(resp.status_code, 403)
        test.refresh_from_db()
        self.assertFalse(test.is_deleted)

    def test_get_returns_confirm_page(self):
        test = Test.objects.create(name='Confirm?', test_type='listening', author=self.mentor)
        self.client.force_login(self.mentor)
        resp = self.client.get(reverse('exams:mentor_delete_test', args=[test.id]))
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# api_task_status
# ---------------------------------------------------------------------------

class ApiTaskStatusTests(TestCase):
    """Task status: owner can poll; non-owner 403; test_type resolved correctly."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('ts_m')
        cls.other = _make_mentor('ts_o')

    def _make_task(self, name, result_test_id=None):
        return IngestionTask.objects.create(
            user=self.mentor, test_name=name,
            status='completed', result_test_id=result_test_id,
        )

    def test_owner_can_poll(self):
        task = self._make_task('My Reading Test')
        self.client.force_login(self.mentor)
        resp = self.client.get(reverse('exams:api_task_status', args=[task.id]))
        self.assertEqual(resp.status_code, 200)

    def test_non_owner_forbidden(self):
        task = self._make_task('Secure Task')
        self.client.force_login(self.other)
        resp = self.client.get(reverse('exams:api_task_status', args=[task.id]))
        self.assertEqual(resp.status_code, 403)

    def test_infers_reading_from_name(self):
        task = self._make_task('Reading Comprehension Test')
        self.client.force_login(self.mentor)
        data = self.client.get(reverse('exams:api_task_status', args=[task.id])).json()
        self.assertEqual(data['result_test_type'], 'reading')

    def test_infers_speaking_from_name(self):
        task = self._make_task('IELTS Speaking Practice')
        self.client.force_login(self.mentor)
        data = self.client.get(reverse('exams:api_task_status', args=[task.id])).json()
        self.assertEqual(data['result_test_type'], 'speaking')

    def test_uses_result_test_id_over_name_heuristic(self):
        # Name says "reading" but actual test is writing — ID must win
        test_obj = Test.objects.create(
            name='Actual Writing Test', test_type='writing', author=self.mentor
        )
        task = self._make_task('This is reading something', result_test_id=test_obj.id)
        self.client.force_login(self.mentor)
        data = self.client.get(reverse('exams:api_task_status', args=[task.id])).json()
        self.assertEqual(data['result_test_type'], 'writing')


# ---------------------------------------------------------------------------
# Create empty test
# ---------------------------------------------------------------------------

class CreateEmptyTestTests(TestCase):
    """mentor_create_empty_test: creates test, redirects to correct builder per type."""

    @classmethod
    def setUpTestData(cls):
        cls.mentor = _make_mentor('cre_m')

    def setUp(self):
        self.client.force_login(self.mentor)

    def test_listening_redirects_to_builder(self):
        resp = self.client.post(
            reverse('exams:mentor_create_test'),
            data={'test_type': 'listening'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn('builder', resp['Location'])
        self.assertTrue(
            Test.objects.filter(author=self.mentor, test_type='listening').exists()
        )

    def test_reading_redirects_to_reading_builder(self):
        resp = self.client.post(
            reverse('exams:mentor_create_test'),
            data={'test_type': 'reading'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn('reading-builder', resp['Location'])

    def test_speaking_redirects_to_speaking_builder(self):
        resp = self.client.post(
            reverse('exams:mentor_create_test'),
            data={'test_type': 'speaking'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn('speaking', resp['Location'])

    def test_get_redirects_to_dashboard(self):
        resp = self.client.get(reverse('exams:mentor_create_test'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('mentor', resp['Location'])
