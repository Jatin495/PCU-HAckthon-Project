"""
SmartClass Monitor - AI Detector Engine
Combines OpenCV, MediaPipe, and FER/DeepFace for:
1. Face Detection (OpenCV Haar Cascade + MediaPipe Face Mesh)
2. Emotion Recognition (FER with FER2013 dataset)
3. Posture Analysis (MediaPipe Pose)
4. Engagement Score Calculation
"""

import cv2
import mediapipe as mp
import numpy as np
import math
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ─── MediaPipe Setup ──────────────────────────────────────────────────────────
mp_face_mesh = mp.solutions.face_mesh
mp_pose = mp.solutions.pose
mp_drawing = mp.solutions.drawing_utils


# ─── Emotion Recognition using FER ───────────────────────────────────────────
try:
    from fer import FER
    _fer_detector = FER(mtcnn=False)  # Use OpenCV backend (faster)
    FER_AVAILABLE = True
    logger.info("✅ FER emotion detector loaded successfully")
except Exception as e:
    FER_AVAILABLE = False
    _fer_detector = None
    logger.warning(f"⚠️ FER not available: {e}. Using fallback emotion detection.")


# ─── OpenCV Haar Cascade Face Detector ───────────────────────────────────────
_face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')


class StudentAnalyzer:
    """
    Analyzes a single student face region.
    Returns emotion, engagement score, and posture data.
    """

    # Emotion → Engagement weight mapping (based on classroom context)
    EMOTION_ENGAGEMENT_WEIGHTS = {
        'happy': 90,
        'focused': 88,
        'neutral': 70,
        'surprise': 65,
        'sad': 35,
        'fear': 30,
        'disgust': 25,
        'angry': 20,
        'confused': 45,  # Confused = still engaging, just not understanding
        'bored': 20,
        'unknown': 50,
    }

    # FER emotion names → our system names
    FER_EMOTION_MAP = {
        'happy': 'happy',
        'neutral': 'neutral',
        'sad': 'sad',
        'angry': 'angry',
        'surprise': 'surprise',
        'fear': 'fear',
        'disgust': 'disgust',
    }

    def __init__(self):
        self.face_mesh = mp_face_mesh.FaceMesh(
            static_image_mode=False,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5
        )
        self.pose = mp_pose.Pose(
            static_image_mode=False,
            model_complexity=0,  # Lightweight
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5
        )

    def detect_emotion(self, face_roi):
        """Detect emotion from face region using FER (FER2013 dataset model)"""
        if not FER_AVAILABLE or _fer_detector is None:
            return self._fallback_emotion(face_roi)

        try:
            # FER expects BGR image
            if face_roi.size == 0:
                return 'unknown', 0.0, {}

            result = _fer_detector.detect_emotions(face_roi)
            if not result:
                return self._fallback_emotion(face_roi)

            emotions = result[0]['emotions']
            # Find dominant emotion
            dominant = max(emotions, key=emotions.get)
            confidence = emotions[dominant]

            # Map to our system's emotion names
            mapped_emotion = self.FER_EMOTION_MAP.get(dominant, dominant)

            # Add extra emotions based on analysis
            all_scores = {self.FER_EMOTION_MAP.get(k, k): v for k, v in emotions.items()}

            return mapped_emotion, float(confidence), all_scores

        except Exception as e:
            logger.debug(f"FER detection error: {e}")
            return self._fallback_emotion(face_roi)

    def _fallback_emotion(self, face_roi):
        """Simple brightness/contrast based emotion fallback"""
        try:
            gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
            mean_brightness = np.mean(gray)
            std_dev = np.std(gray)

            # Very rough heuristic
            if mean_brightness > 140 and std_dev > 40:
                emotion = 'happy'
                confidence = 0.55
            elif mean_brightness < 90:
                emotion = 'sad'
                confidence = 0.45
            else:
                emotion = 'neutral'
                confidence = 0.60

            return emotion, confidence, {emotion: confidence, 'neutral': 0.3}
        except:
            return 'unknown', 0.0, {}

    def analyze_head_pose(self, face_landmarks, image_shape):
        """
        Estimate head pose using facial landmarks.
        Returns: (yaw, pitch, roll, is_looking_forward, attention_score)
        """
        if not face_landmarks:
            return 0, 0, 0, False, 0.0

        h, w = image_shape[:2]
        landmarks = face_landmarks.landmark

        # Key facial points for pose estimation
        # Nose tip (1), Chin (152), Left eye corner (33), Right eye corner (263)
        # Left mouth (61), Right mouth (291)
        nose_tip = np.array([landmarks[1].x * w, landmarks[1].y * h, landmarks[1].z * w])
        chin = np.array([landmarks[152].x * w, landmarks[152].y * h, landmarks[152].z * w])
        left_eye = np.array([landmarks[33].x * w, landmarks[33].y * h, landmarks[33].z * w])
        right_eye = np.array([landmarks[263].x * w, landmarks[263].y * h, landmarks[263].z * w])

        # Calculate angles
        eye_center = (left_eye + right_eye) / 2

        # Yaw (left-right head turn)
        dx = right_eye[0] - left_eye[0]
        dy = right_eye[1] - left_eye[1]
        roll = math.degrees(math.atan2(dy, dx))

        # Pitch (up-down tilt) - vertical nose-chin vs nose-eye
        vertical = chin - nose_tip
        pitch = math.degrees(math.atan2(vertical[1], vertical[2])) - 90

        # Yaw estimation from face symmetry
        face_center_x = (left_eye[0] + right_eye[0]) / 2
        nose_offset = nose_tip[0] - face_center_x
        face_width = abs(right_eye[0] - left_eye[0])
        yaw = (nose_offset / (face_width + 1e-6)) * 90

        # Determine if looking forward
        is_looking_forward = abs(yaw) < 25 and abs(pitch) < 20 and abs(roll) < 15

        # Attention score based on head orientation
        yaw_penalty = min(abs(yaw) / 45, 1.0)
        pitch_penalty = min(abs(pitch) / 35, 1.0)
        roll_penalty = min(abs(roll) / 25, 1.0)
        attention_score = max(0, (1 - (yaw_penalty * 0.5 + pitch_penalty * 0.3 + roll_penalty * 0.2))) * 100

        return float(yaw), float(pitch), float(roll), is_looking_forward, float(attention_score)

    def analyze_eye_openness(self, face_landmarks, image_shape):
        """Estimate eye openness (EAR) to detect drowsiness"""
        if not face_landmarks:
            return 0.3, False

        landmarks = face_landmarks.landmark
        h, w = image_shape[:2]

        # Eye landmarks (MediaPipe Face Mesh indices)
        # Left eye
        left_upper = landmarks[159]
        left_lower = landmarks[145]
        left_left = landmarks[33]
        left_right = landmarks[133]

        # Right eye
        right_upper = landmarks[386]
        right_lower = landmarks[374]
        right_left = landmarks[362]
        right_right = landmarks[263]

        def ear(upper, lower, eye_left, eye_right):
            vertical = abs(upper.y - lower.y)
            horizontal = abs(eye_left.x - eye_right.x) + 1e-6
            return vertical / horizontal

        left_ear = ear(left_upper, left_lower, left_left, left_right)
        right_ear = ear(right_upper, right_lower, right_left, right_right)
        avg_ear = (left_ear + right_ear) / 2

        # EAR < 0.15 typically means closed/drowsy
        is_drowsy = avg_ear < 0.15

        return float(avg_ear), is_drowsy

    def analyze_posture(self, pose_landmarks, image_shape):
        """
        Analyze body posture using MediaPipe Pose.
        Returns: (posture_score, is_slouching, shoulder_angle)
        """
        if not pose_landmarks:
            return 70.0, False, 0.0

        landmarks = pose_landmarks.landmark
        h, w = image_shape[:2]

        try:
            # Shoulder landmarks
            left_shoulder = landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER]
            right_shoulder = landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER]

            # Ear landmarks (head position)
            left_ear = landmarks[mp_pose.PoseLandmark.LEFT_EAR]
            right_ear = landmarks[mp_pose.PoseLandmark.RIGHT_EAR]

            # Shoulder angle (slope)
            shoulder_dx = (right_shoulder.x - left_shoulder.x) * w
            shoulder_dy = (right_shoulder.y - left_shoulder.y) * h
            shoulder_angle = math.degrees(math.atan2(shoulder_dy, shoulder_dx))

            # Head forward position relative to shoulders
            head_x = (left_ear.x + right_ear.x) / 2
            shoulder_x = (left_shoulder.x + right_shoulder.x) / 2
            head_y = (left_ear.y + right_ear.y) / 2
            shoulder_y = (left_shoulder.y + right_shoulder.y) / 2

            # Forward head posture
            head_forward = head_y < shoulder_y  # Head is above shoulders (normal)
            is_slouching = not head_forward or abs(shoulder_angle) > 10

            # Calculate posture score
            angle_penalty = min(abs(shoulder_angle) / 20, 1.0) * 30
            posture_score = max(0, 100 - angle_penalty)
            if is_slouching:
                posture_score = max(0, posture_score - 20)

            return float(posture_score), is_slouching, float(shoulder_angle)
        except Exception as e:
            return 70.0, False, 0.0

    def calculate_engagement_score(self, emotion, emotion_confidence, attention_score,
                                   posture_score, eye_openness, face_detected):
        """
        Calculate overall engagement score (0-100) from multiple signals.
        Inspired by DAiSEE dataset engagement levels.
        """
        if not face_detected:
            return 0.0

        # Base emotion score
        emotion_base = self.EMOTION_ENGAGEMENT_WEIGHTS.get(emotion, 50)

        # Weighted combination of signals
        # Emotion: 35%, Attention (head pose): 30%, Posture: 20%, Eye openness: 15%
        engagement = (
            emotion_base * 0.35 +
            attention_score * 0.30 +
            posture_score * 0.20 +
            (eye_openness * 200) * 0.15  # Scale EAR to 0-100
        )

        # Confidence penalty
        if emotion_confidence < 0.4:
            engagement *= 0.9

        return min(100, max(0, float(engagement)))

    def close(self):
        self.face_mesh.close()
        self.pose.close()


class ClassroomDetector:
    """
    Main classroom monitoring class.
    Processes video frames and returns per-student engagement data.
    """

    def __init__(self):
        self.face_mesh = mp_face_mesh.FaceMesh(
            static_image_mode=False,
            max_num_faces=10,  # Support up to 10 detected faces per frame
            refine_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5
        )
        self.student_analyzer = StudentAnalyzer()
        self.frame_count = 0
        logger.info("✅ ClassroomDetector initialized")

    def detect_faces_opencv(self, frame):
        """Detect faces using OpenCV Haar Cascade (fast, used for ROI extraction)"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray_equalized = cv2.equalizeHist(gray)

        faces = _face_cascade.detectMultiScale(
            gray_equalized,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(50, 50),
            maxSize=(400, 400)
        )
        return faces if len(faces) > 0 else []

    def process_frame(self, frame):
        """
        Process a video frame and return engagement data for all detected students.

        Returns:
            dict: {
                'students': list of per-student engagement data,
                'class_avg_engagement': float,
                'emotion_distribution': dict,
                'present_count': int,
                'annotated_frame': numpy array (BGR)
            }
        """
        self.frame_count += 1
        h, w = frame.shape[:2]
        results = []

        # Detect faces using OpenCV
        faces = self.detect_faces_opencv(frame)

        # Also run MediaPipe Face Mesh on full frame
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        face_mesh_results = self.face_mesh.process(rgb_frame)

        annotated = frame.copy()

        face_landmarks_list = face_mesh_results.multi_face_landmarks if face_mesh_results.multi_face_landmarks else []

        # Process each detected face
        for i, (x, y, fw, fh) in enumerate(faces):
            # Extract face ROI
            face_roi = frame[y:y+fh, x:x+fw]
            if face_roi.size == 0:
                continue

            # Get corresponding landmarks if available
            face_landmarks = face_landmarks_list[i] if i < len(face_landmarks_list) else None

            # Emotion detection (FER/FER2013)
            emotion, emotion_confidence, emotion_scores = self.student_analyzer.detect_emotion(face_roi)

            # Head pose (attention direction)
            yaw, pitch, roll, is_looking, attention_score = self.student_analyzer.analyze_head_pose(
                face_landmarks, frame.shape
            )

            # Eye openness
            eye_ear, is_drowsy = self.student_analyzer.analyze_eye_openness(face_landmarks, frame.shape)

            # Posture (placeholder - would need full body frame)
            posture_score = 75.0 if not is_drowsy else 40.0
            is_slouching = is_drowsy

            # Engagement score
            engagement = self.student_analyzer.calculate_engagement_score(
                emotion, emotion_confidence, attention_score,
                posture_score, eye_ear, True
            )

            student_data = {
                'face_index': i,
                'face_bbox': {'x': int(x), 'y': int(y), 'w': int(fw), 'h': int(fh)},
                'emotion': emotion,
                'emotion_confidence': round(emotion_confidence, 3),
                'emotion_scores': emotion_scores,
                'engagement_score': round(engagement, 1),
                'attention_score': round(attention_score, 1),
                'posture_score': round(posture_score, 1),
                'eye_openness': round(eye_ear, 3),
                'is_looking_forward': bool(is_looking),
                'is_drowsy': bool(is_drowsy),
                'is_slouching': bool(is_slouching),
                'head_yaw': round(yaw, 1),
                'head_pitch': round(pitch, 1),
                'face_detected': True,
            }
            results.append(student_data)

            # ── Draw annotations on frame ──
            # Engagement color
            if engagement >= 80:
                color = (0, 220, 100)   # Green
                level = 'HIGH'
            elif engagement >= 60:
                color = (0, 200, 255)   # Yellow/Amber
                level = 'MEDIUM'
            else:
                color = (0, 60, 255)    # Red
                level = 'LOW'

            # Draw face rectangle
            cv2.rectangle(annotated, (x, y), (x+fw, y+fh), color, 2)

            # Draw emotion + engagement label
            label = f"{emotion.upper()} ({emotion_confidence:.0%})"
            eng_label = f"Eng: {engagement:.0f}%"

            # Background for text
            cv2.rectangle(annotated, (x, y-45), (x+fw, y), (0, 0, 0), -1)
            cv2.putText(annotated, label, (x+3, y-28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
            cv2.putText(annotated, eng_label, (x+3, y-10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)

            # Engagement bar
            bar_x = x
            bar_y = y + fh + 5
            bar_w = int(fw * engagement / 100)
            cv2.rectangle(annotated, (bar_x, bar_y), (bar_x + fw, bar_y + 6), (50, 50, 50), -1)
            cv2.rectangle(annotated, (bar_x, bar_y), (bar_x + bar_w, bar_y + 6), color, -1)

        # Calculate class-level metrics
        if results:
            avg_engagement = sum(r['engagement_score'] for r in results) / len(results)
            emotion_dist = {}
            for r in results:
                e = r['emotion']
                emotion_dist[e] = emotion_dist.get(e, 0) + 1
        else:
            avg_engagement = 0.0
            emotion_dist = {}

        # Draw class info overlay
        cv2.rectangle(annotated, (0, 0), (w, 35), (15, 15, 25), -1)
        cv2.putText(annotated, f"SmartClass Monitor | Faces: {len(results)} | Avg Engagement: {avg_engagement:.1f}%",
                    (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 1, cv2.LINE_AA)

        return {
            'students': results,
            'class_avg_engagement': round(avg_engagement, 1),
            'emotion_distribution': emotion_dist,
            'present_count': len(results),
            'annotated_frame': annotated,
            'timestamp': datetime.now().isoformat(),
        }

    def close(self):
        self.face_mesh.close()
        self.student_analyzer.close()


# ─── Engagement Scorer ────────────────────────────────────────────────────────
class EngagementScorer:
    """
    Calculates engagement scores using the DAiSEE-inspired scoring system.
    Maps raw signals to engagement levels: Not Engaged, Barely, Engaged, Highly Engaged
    """
    LEVELS = {
        (80, 100): ('Highly Engaged', '#22c55e'),
        (60, 80):  ('Engaged', '#3b82f6'),
        (40, 60):  ('Barely Engaged', '#f59e0b'),
        (0, 40):   ('Not Engaged', '#ef4444'),
    }

    @staticmethod
    def score_to_level(score):
        for (low, high), (label, color) in EngagementScorer.LEVELS.items():
            if low <= score < high:
                return label, color
        return 'Not Engaged', '#ef4444'

    @staticmethod
    def detect_confusion_alert(emotion_distribution, total_students):
        """Trigger class-wide alert if >30% students show confusion/boredom"""
        if total_students == 0:
            return False
        confused = emotion_distribution.get('confused', 0) + emotion_distribution.get('bored', 0)
        ratio = confused / total_students
        return ratio > 0.30

    @staticmethod
    def generate_student_alert(student_data):
        """Generate alert if individual student needs attention"""
        alerts = []
        if student_data['engagement_score'] < 40:
            alerts.append({
                'type': 'low_engagement',
                'severity': 'high',
                'message': f"Very low engagement detected",
            })
        if student_data['emotion'] in ['bored', 'sad', 'disgusted']:
            alerts.append({
                'type': 'bored',
                'severity': 'medium',
                'message': f"Student appears {student_data['emotion']}",
            })
        if student_data['is_drowsy']:
            alerts.append({
                'type': 'distracted',
                'severity': 'medium',
                'message': "Student appears drowsy",
            })
        return alerts
