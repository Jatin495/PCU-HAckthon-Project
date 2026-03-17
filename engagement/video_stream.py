"""
SmartClass Monitor - Video Stream Service
Handles real-time video capture, processing, and MJPEG streaming.
"""

import cv2
import threading
import time
import base64
import numpy as np
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class VideoStream:
    """
    Thread-safe video capture and processing.
    Runs AI detection in a separate thread every 5 seconds.
    """

    def __init__(self):
        self.cap = None
        self.current_frame = None
        self.annotated_frame = None
        self.is_running = False
        self.lock = threading.Lock()
        self.detector = None
        self.last_analysis_result = None
        self.analysis_interval = 5  # Analyze every 5 seconds (configurable)
        self.last_analysis_time = 0
        self._capture_thread = None
        self._analysis_thread = None
        self.session_id = None
        self.student_map = {}  # face_index -> student_id mapping
        self.frame_count = 0
        self.fps = 0
        self._fps_start = time.time()

    def start(self, source=0, session_id=None):
        """Start video capture from camera/file"""
        if self.is_running:
            logger.warning("VideoStream already running")
            return False

        try:
            # Try to open camera
            self.cap = cv2.VideoCapture(source)
            if not self.cap.isOpened():
                # Try default camera index 0
                self.cap = cv2.VideoCapture(0)
                if not self.cap.isOpened():
                    logger.error("Cannot open camera")
                    return False

            # Set resolution for performance
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.cap.set(cv2.CAP_PROP_FPS, 30)

            self.session_id = session_id
            self.is_running = True
            self.last_analysis_time = 0

            # Initialize detector
            from engagement.detector import ClassroomDetector
            self.detector = ClassroomDetector()

            # Start capture thread
            self._capture_thread = threading.Thread(
                target=self._capture_loop, daemon=True, name='CaptureThread'
            )
            self._capture_thread.start()

            logger.info(f"✅ VideoStream started (source={source}, session={session_id})")
            return True

        except Exception as e:
            logger.error(f"Failed to start VideoStream: {e}")
            self.is_running = False
            return False

    def stop(self):
        """Stop video capture and release resources"""
        self.is_running = False
        time.sleep(0.5)  # Allow threads to finish

        if self.cap:
            self.cap.release()
            self.cap = None

        if self.detector:
            self.detector.close()
            self.detector = None

        logger.info("VideoStream stopped")

    def _capture_loop(self):
        """Main capture and analysis loop"""
        while self.is_running:
            try:
                ret, frame = self.cap.read()
                if not ret:
                    logger.warning("Failed to capture frame")
                    time.sleep(0.1)
                    continue

                self.frame_count += 1

                # Calculate FPS
                elapsed = time.time() - self._fps_start
                if elapsed >= 1.0:
                    self.fps = self.frame_count / elapsed
                    self.frame_count = 0
                    self._fps_start = time.time()

                # Check if it's time for AI analysis (every 5 seconds)
                current_time = time.time()
                should_analyze = (current_time - self.last_analysis_time) >= self.analysis_interval

                if should_analyze and self.detector:
                    # Run analysis on this frame
                    try:
                        result = self.detector.process_frame(frame)
                        self.last_analysis_time = current_time

                        with self.lock:
                            self.last_analysis_result = result
                            self.annotated_frame = result['annotated_frame']

                        # Save to database asynchronously
                        threading.Thread(
                            target=self._save_to_database,
                            args=(result,),
                            daemon=True
                        ).start()

                    except Exception as e:
                        logger.error(f"Analysis error: {e}")
                        with self.lock:
                            self.annotated_frame = frame.copy()
                else:
                    # Just update current frame with last annotation overlaid
                    with self.lock:
                        if self.annotated_frame is not None:
                            self.current_frame = frame
                        else:
                            self.current_frame = frame

                time.sleep(0.033)  # ~30 FPS

            except Exception as e:
                logger.error(f"Capture loop error: {e}")
                time.sleep(0.5)

    def _save_to_database(self, analysis_result):
        """Save analysis results to SQLite database"""
        try:
            import django
            from engagement.models import EngagementRecord, ClassEngagementSnapshot, Alert, ClassSession, Student
            from django.utils import timezone

            if not self.session_id:
                return

            session = ClassSession.objects.filter(id=self.session_id, status='active').first()
            if not session:
                return

            # Save class snapshot
            emotion_dist_json = __import__('json').dumps(analysis_result['emotion_distribution'])
            confusion_alert = analysis_result.get('confusion_alert', False)

            ClassEngagementSnapshot.objects.create(
                session=session,
                avg_engagement=analysis_result['class_avg_engagement'],
                avg_attention=analysis_result['class_avg_engagement'],  # Simplified
                present_count=analysis_result['present_count'],
                emotion_distribution=emotion_dist_json,
                confusion_alert=confusion_alert,
                low_engagement_alert=analysis_result['class_avg_engagement'] < 40,
            )

            # Save per-student records
            students = Student.objects.filter(is_active=True)[:analysis_result['present_count']]
            for i, student_data in enumerate(analysis_result['students']):
                if i < len(students):
                    import json
                    EngagementRecord.objects.create(
                        student=students[i],
                        session=session,
                        engagement_score=student_data['engagement_score'],
                        attention_score=student_data['attention_score'],
                        emotion=student_data['emotion'],
                        emotion_confidence=student_data['emotion_confidence'],
                        emotion_scores=json.dumps(student_data.get('emotion_scores', {})),
                        posture_score=student_data['posture_score'],
                        is_slouching=student_data.get('is_slouching', False),
                        face_detected=True,
                        face_confidence=student_data.get('emotion_confidence', 0.8),
                        head_angle=student_data.get('head_yaw', 0),
                        eye_contact=student_data.get('is_looking_forward', False),
                    )

            # Generate alerts for confusion > 30%
            if confusion_alert:
                Alert.objects.create(
                    session=session,
                    student=None,
                    alert_type='class_confusion',
                    severity='high',
                    message=f"⚠️ Over 30% of students appear confused or bored!"
                )

        except Exception as e:
            logger.error(f"Database save error: {e}")

    def get_jpeg_frame(self):
        """Get current annotated frame as JPEG bytes for MJPEG streaming"""
        with self.lock:
            frame = self.annotated_frame if self.annotated_frame is not None else self.current_frame

        if frame is None:
            # Return a placeholder frame
            placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(placeholder, "Waiting for camera...",
                        (120, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (100, 100, 100), 2)
            frame = placeholder

        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return jpeg.tobytes()

    def get_frame_base64(self):
        """Get current frame as base64 encoded JPEG string"""
        jpeg_bytes = self.get_jpeg_frame()
        return base64.b64encode(jpeg_bytes).decode('utf-8')

    def get_latest_analysis(self):
        """Thread-safe access to latest analysis result"""
        with self.lock:
            return self.last_analysis_result

    def get_status(self):
        return {
            'is_running': self.is_running,
            'fps': round(self.fps, 1),
            'session_id': self.session_id,
            'has_frame': self.current_frame is not None,
            'analysis_available': self.last_analysis_result is not None,
        }


# Global singleton instance
_video_stream_instance = None
_stream_lock = threading.Lock()


def get_video_stream():
    """Get or create the global VideoStream singleton"""
    global _video_stream_instance
    with _stream_lock:
        if _video_stream_instance is None:
            _video_stream_instance = VideoStream()
    return _video_stream_instance


def start_stream(source=0, session_id=None):
    stream = get_video_stream()
    if not stream.is_running:
        return stream.start(source=source, session_id=session_id)
    return True


def stop_stream():
    stream = get_video_stream()
    stream.stop()


def generate_mjpeg_frames():
    """Generator for MJPEG streaming"""
    stream = get_video_stream()
    while stream.is_running:
        frame = stream.get_jpeg_frame()
        yield (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
        )
        time.sleep(0.033)  # ~30 FPS
