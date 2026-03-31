import os
import sys
import json
os.environ['DJANGO_SETTINGS_MODULE'] = 'smartclass_backend.settings'
import django
django.setup()

from engagement.models import Student
from engagement.face_recognition import get_face_recognition_system

# Check how many students have encodings
students = Student.objects.filter(is_active=True)
print("=== REGISTERED STUDENTS ===\n")

for student in students:
    try:
        encodings = json.loads(student.face_encoding or '[]') if student.face_encoding else []
        encoding_count = len(encodings)
    except:
        encoding_count = 0
    
    print(f"{student.student_id}: {student.name}")
    print(f"  Face encodings: {encoding_count}")
    if encoding_count == 0:
        print(f"  WARNING: No encodings!")
    print()

# Check face recognition system
print("\n=== FACE RECOGNITION SYSTEM ===\n")
fr_system = get_face_recognition_system()
print(f"Total students loaded: {len(fr_system.student_encodings)}")
for sid, encodings in fr_system.student_encodings.items():
    student_name = sid
    try:
        s = Student.objects.get(student_id=sid)
        student_name = s.name
    except:
        pass
    print(f"  {sid} ({student_name}): {len(encodings)} encodings")

print("\n=== RECOGNITION THRESHOLDS ===")
print(f"Confidence threshold: 0.86")
print(f"Min liveness score: 45")
print(f"Min stable frames: 8")
print(f"Min signature variation: 0.02")
print(f"Require blink: Yes")
print(f"Min blinks: 1")
