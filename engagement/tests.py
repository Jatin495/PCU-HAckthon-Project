import numpy as np
from django.test import SimpleTestCase

from .camera import CameraProcessor


class _StubFaceRecognition:
	def identify_student(self, face_roi, confidence_threshold=0.45):
		return None, None, 0.0


class _StubFerDetector:
	def __init__(self, results):
		self._results = results

	def detect_emotions(self, _rgb_frame):
		return self._results


class _StubHaarCascade:
	def __init__(self, faces):
		self._faces = faces

	def detectMultiScale(self, *_args, **_kwargs):
		return self._faces


class CameraProcessorTests(SimpleTestCase):
	def test_get_analysis_includes_expected_fields_and_bbox_is_sanitized(self):
		processor = CameraProcessor()
		processor._daisee_init_attempted = True
		processor.students_data = [
			{
				"student_id": "S1",
				"face_bbox": np.array([10.9, 20.1, 30.7, 40.2]),
			}
		]
		processor.recognized_students = [{"student_id": "S1"}]
		processor.current_emotions = {"happy": 1}
		processor.faces_detected = 1
		processor.avg_engagement = 81.2

		analysis = processor.get_analysis()

		self.assertIn("faces_detected", analysis)
		self.assertIn("emotions", analysis)
		self.assertIn("students", analysis)
		self.assertIn("fusion_weights", analysis)
		self.assertEqual(analysis["students"][0]["face_bbox"], [10, 20, 30, 40])

	def test_analyze_frame_uses_fer_results_and_updates_state(self):
		processor = CameraProcessor()
		processor._daisee_init_attempted = True
		processor._fer_init_attempted = True
		processor._mp_init_attempted = True
		processor.face_recognition_system = _StubFaceRecognition()
		processor._fer_detector = _StubFerDetector(
			[{"box": [10, 15, 30, 35], "emotions": {"happy": 0.8}}]
		)

		frame = np.zeros((120, 160, 3), dtype=np.uint8)
		out_frame = processor._analyze_frame(frame)

		self.assertEqual(out_frame.shape, frame.shape)
		self.assertEqual(processor.faces_detected, 1)
		self.assertEqual(processor.current_emotions, {"happy": 1})
		self.assertEqual(len(processor.students_data), 1)
		self.assertEqual(processor.students_data[0]["engagement_source"], "fer+daisee-fusion")

	def test_analyze_frame_with_no_detections_clears_state(self):
		processor = CameraProcessor()
		processor._daisee_init_attempted = True
		processor._fer_init_attempted = True
		processor._mp_init_attempted = True
		processor.face_recognition_system = _StubFaceRecognition()
		processor._fer_detector = _StubFerDetector([])
		processor._mp_face_detection = None
		processor._haar_face_cascade = _StubHaarCascade([])

		processor.students_data = [{"student_id": "old"}]
		processor.recognized_students = [{"student_id": "old"}]
		processor.current_emotions = {"neutral": 2}
		processor.faces_detected = 2
		processor.avg_engagement = 75

		frame = np.zeros((120, 160, 3), dtype=np.uint8)
		_ = processor._analyze_frame(frame)

		self.assertEqual(processor.students_data, [])
		self.assertEqual(processor.recognized_students, [])
		self.assertEqual(processor.current_emotions, {})
		self.assertEqual(processor.faces_detected, 0)
		self.assertEqual(processor.avg_engagement, 0)
