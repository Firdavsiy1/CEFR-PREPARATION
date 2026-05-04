import re

with open('exams/mentor_views.py', 'r') as f:
    content = f.read()

speaking_clone_function = """def _clone_speaking_parts_to_individual_tests(test_obj, user):
    \"\"\"Clone each SpeakingPart into its own standalone micro-test.\"\"\"
    from exams.models import SpeakingPart, SpeakingQuestion
    
    for part in test_obj.speaking_parts.prefetch_related('questions').all():
        new_test_name = f"{test_obj.name} - Part {part.part_number}"
        Test.objects.filter(name=new_test_name).delete()

        new_test = Test.objects.create(
            name=new_test_name,
            test_type='speaking',
            is_active=True,
            author=user,
        )

        new_part = SpeakingPart.objects.create(
            test=new_test,
            part_number=part.part_number,
            instructions=part.instructions,
            original_image=part.original_image if part.original_image else None,
            cropped_image=part.cropped_image if hasattr(part, 'cropped_image') and part.cropped_image else None,
            alt_text=part.alt_text,
            debate_data=part.debate_data,
            validation_data=part.validation_data,
            is_validated=part.is_validated,
        )

        for q in part.questions.all():
            SpeakingQuestion.objects.create(
                part=new_part,
                question_number=q.question_number,
                question_text=q.question_text,
                audio_file=q.audio_file if q.audio_file else None,
            )

"""

new_clone_parts = """def _clone_parts_to_individual_tests(test_obj, user):
    \"\"\"Clone each part of a test into its own standalone micro-test. Supports Writing too.\"\"\"
    if test_obj.test_type == 'writing':
        tasks = test_obj.writing_tasks.all()
        # Group tasks by their logical 'part' based on order (1, 2 = Part 1; 3 = Part 2)
        part1_tasks = [t for t in tasks if t.order in (1, 2)]
        part2_tasks = [t for t in tasks if t.order == 3]
        
        for part_num, task_group in [(1, part1_tasks), (2, part2_tasks)]:
            if not task_group: continue
            new_test_name = f"{test_obj.name} - Part {part_num}"
            Test.objects.filter(name=new_test_name).delete()
            new_test = Test.objects.create(
                name=new_test_name, test_type=test_obj.test_type, is_active=True, author=user
            )
            for t in task_group:
                t.id = None
                t.test = new_test
                t.save()
        return
        
    if test_obj.test_type == 'reading':
        return _clone_reading_parts_to_individual_tests(test_obj, user)
        
    if test_obj.test_type == 'speaking':
        return _clone_speaking_parts_to_individual_tests(test_obj, user)

    for part in test_obj.parts.all():
"""

# Apply the replacements
# First, insert speaking clone function before _clone_reading_parts_to_individual_tests
content = content.replace("def _clone_reading_parts_to_individual_tests", speaking_clone_function + "\ndef _clone_reading_parts_to_individual_tests")

# Next replace the first lines of _clone_parts_to_individual_tests 
old_clone_sig = """def _clone_parts_to_individual_tests(test_obj, user):
    \"\"\"Clone each part of a test into its own standalone micro-test. Supports Writing too.\"\"\"
    if test_obj.test_type == 'writing':
        tasks = test_obj.writing_tasks.all()
        # Group tasks by their logical 'part' based on order (1, 2 = Part 1; 3 = Part 2)
        part1_tasks = [t for t in tasks if t.order in (1, 2)]
        part2_tasks = [t for t in tasks if t.order == 3]
        
        for part_num, task_group in [(1, part1_tasks), (2, part2_tasks)]:
            if not task_group: continue
            new_test_name = f"{test_obj.name} - Part {part_num}"
            Test.objects.filter(name=new_test_name).delete()
            new_test = Test.objects.create(
                name=new_test_name, test_type=test_obj.test_type, is_active=True, author=user
            )
            for t in task_group:
                t.id = None
                t.test = new_test
                t.save()
        return

    for part in test_obj.parts.all():"""
content = content.replace(old_clone_sig, new_clone_parts)

with open('exams/mentor_views.py', 'w') as f:
    f.write(content)
