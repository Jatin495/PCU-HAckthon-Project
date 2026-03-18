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

# Import the enhanced camera processing
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from working_camera import process_frame, analyze_emotion_advanced, calculate_engagement_score

logger = logging.getLogger(__name__)


class VideoStream:
    """
    Thread-safe video capture and processing.
    Runs AI detection in a separate thread every 5 seconds.
    Falls back to demo mode if camera is not available.
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
        self.demo_mode = False

    def start(self, source=0, session_id=None):
        """Start video capture from camera/file"""
        if self.is_running:
            logger.warning("VideoStream already running")
            return False

        logger.info(f"🎥 VideoStream.start() called with source={source}, session_id={session_id}")
        
        try:
            logger.info(f"🎥 Starting camera initialization for source {source}")
            
            # Try to open camera with different backends
            camera_backends = [
                cv2.CAP_DSHOW,      # DirectShow (Windows)
                cv2.CAP_MSMF,       # Media Foundation (Windows, has issues)
                cv2.CAP_FFMPEG,     # FFmpeg
                0                   # Auto
            ]
            
            self.cap = None
            self.demo_mode = True  # Start as True - fallback to demo if camera fails
            
            logger.info(f"🎥 Trying {len(camera_backends)} camera backends...")
            
            for backend in camera_backends:
                try:
                    logger.info(f"🎥 Trying backend {backend} with source {source}")
                    if backend == 0:
                        self.cap = cv2.VideoCapture(source)
                    else:
                        self.cap = cv2.VideoCapture(source, backend)
                    
                    if self.cap.isOpened():
                        # Test if we can read from camera
                        ret, test_frame = self.cap.read()
                        if ret and test_frame is not None:
                            logger.info(f"✅ Camera opened successfully with backend {backend}: {test_frame.shape}")
                            self.demo_mode = False  # Only set to False on SUCCESS
                            break
                        else:
                            logger.warning(f"Camera opened with backend {backend} but can't read frames")
                            self.cap.release()
                    else:
                        logger.warning(f"Failed to open camera with backend {backend}")
                        if self.cap:
                            self.cap.release()
                            self.cap = None
                except Exception as e:
                    logger.warning(f"Failed to try backend {backend}: {e}")
                    if self.cap:
                        self.cap.release()
                        self.cap = None
            
            if self.demo_mode:
                logger.warning("🎭 No working camera found, using demo mode")
                self.cap = None
            else:
                # Set resolution for performance
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                self.cap.set(cv2.CAP_PROP_FPS, 30)

            self.session_id = session_id
            self.is_running = True
            self.last_analysis_time = 0

            # Initialize detector; if detector dependencies fail, still stream frames.
            try:
                if self.demo_mode:
                    from engagement.detector import ClassroomDetector
                    self.detector = ClassroomDetector()
                    logger.info("🎭 Using demo mode detector")
                else:
                    from engagement.simple_detector import RealCameraDetector
                    self.detector = RealCameraDetector()
                    logger.info("📹 Using real camera detector")
            except Exception as detector_error:
                logger.error(f"Detector initialization failed, continuing without AI analysis: {detector_error}")
                self.detector = None

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
        logger.info("Stopping video stream...")
        self.is_running = False
        
        # Stop camera capture immediately
        if self.cap:
            try:
                self.cap.release()
            except:
                pass
            self.cap = None

        # Stop detector
        if self.detector:
            try:
                self.detector.close()
            except:
                pass
            self.detector = None

        # Clear current frame
        self.current_frame = None
        
        # Force garbage collection to clean up
        import gc
        gc.collect()
        
        logger.info("✅ VideoStream stopped - Camera released")

    def _capture_loop(self):
        """Main capture and analysis loop"""
        while self.is_running:
            try:
                if self.demo_mode:
                    # Generate demo frame
                    frame = self._generate_demo_frame()
                    time.sleep(0.1)
                else:
                    # Capture from camera
                    ret, frame = self.cap.read()
                    if not ret:
                        logger.warning("Failed to capture frame")
                        time.sleep(0.1)
                        continue
                
                # Update frame count for FPS calculation
                self.frame_count += 1
                if self.frame_count == 1:
                    self._fps_start = time.time()

                # Check if it's time for AI analysis (every 0.5 seconds for continuous detection)
                current_time = time.time()
                should_analyze = (current_time - self.last_analysis_time) >= 0.5  # Analyze every 0.5 seconds

                if should_analyze:
                    # Run analysis on this frame using enhanced face detection
                    try:
                        # Use enhanced face detection from working_camera
                        processed_frame = process_frame(frame.copy())
                        
                        # Create result structure
                        result = {
                            'annotated_frame': processed_frame,
                            'face_regions': [],
                            'students': [],
                            'class_avg_engagement': 70,  # Default
                            'present_count': 0,
                            'emotion_distribution': {}
                        }
                        
                        with self.lock:
                            self.last_analysis_result = result
                            self.annotated_frame = processed_frame
                            self.current_frame = processed_frame
                            self.last_analysis_time = current_time
                        
                        logger.info(f"✅ Frame analyzed successfully")
                        
                    except Exception as e:
                        logger.error(f"Analysis error: {e}")
                        with self.lock:
                            self.annotated_frame = frame.copy()
                            self.current_frame = frame.copy()
                else:
                    # Always update current frame for smooth video display
                    with self.lock:
                        if self.annotated_frame is not None:
                            # Prefer annotated frame if available
                            self.current_frame = self.annotated_frame
                        else:
                            # Show raw frame
                            self.current_frame = frame

                # Calculate FPS
                if self.frame_count >= 30:
                    self.fps = 30 / (time.time() - self._fps_start)
                    self.frame_count = 0
                    self._fps_start = time.time()

            except Exception as e:
                logger.error(f"Capture loop error: {e}")
                time.sleep(0.1)

    def _add_face_recognition(self, result, frame):
        """Add face recognition to identify students in the frame"""
        try:
            from .face_recognition import get_face_recognition_system
            
            face_recognition = get_face_recognition_system()
            face_regions = result.get('face_regions', [])
            
            logger.info(f"🔍 Face regions detected: {len(face_regions)}")
            
            # If no faces detected, do not inject mock students.
            if len(face_regions) == 0:
                logger.info("No faces detected in current frame")
                recognized_students = []
            else:
                # Recognize each detected face
                recognized_students = []
                for i, face_data in enumerate(face_regions):
                    face_roi = face_data.get('face_roi')
                    if face_roi is not None and face_roi.size > 0:
                        # Try to identify the student
                        student_id, student_name, confidence = face_recognition.identify_student(face_roi)
                        
                        if student_id and student_name:
                            # Found a match
                            recognized_students.append({
                                'face_index': i,
                                'student_id': student_id,
                                'name': student_name,
                                'confidence': confidence,
                                'emotion': face_data.get('emotion', 'neutral'),
                                'engagement': face_data.get('engagement', 0),
                                'face_region': face_data
                            })
                            
                            # Annotate the frame with student name
                            bbox = face_data.get('bbox', [0, 0, 0, 0])
                            x, y, w, h = bbox
                            
                            # Draw name label
                            label = f"{student_name} ({confidence:.2f})"
                            cv2.rectangle(frame, (x, y-25), (x + len(label)*8, y-5), (0, 255, 0), -1)
                            cv2.putText(frame, label, (x, y-8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
                            
                        else:
                            # No match found - create a placeholder student for detected face
                            # This ensures we show something for each detected face
                            placeholder_name = f"Detected Student {i+1}"
                            recognized_students.append({
                                'face_index': i,
                                'student_id': f"DETECTED_{i+1}",
                                'name': placeholder_name,
                                'confidence': 0.0,
                                'emotion': face_data.get('emotion', 'neutral'),
                                'engagement': face_data.get('engagement', 0),
                                'face_region': face_data
                            })
                            
                            # Annotate the frame with placeholder
                            bbox = face_data.get('bbox', [0, 0, 0, 0])
                            x, y, w, h = bbox
                            cv2.rectangle(frame, (x, y-25), (x + len(placeholder_name)*8, y-5), (255, 165, 0), -1)
                            cv2.putText(frame, placeholder_name, (x, y-8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
            
            # Update result with recognized students
            result['recognized_students'] = recognized_students
            
            # Update annotated frame
            result['annotated_frame'] = frame
            
            logger.info(f"✅ Final recognized students: {len(recognized_students)}")
            for student in recognized_students:
                logger.info(f"   - {student['name']} (confidence: {student['confidence']})")
            
            return result
            
        except Exception as e:
            logger.error(f"Face recognition error: {e}")
            result['recognized_students'] = []
            return result

    def _generate_demo_frame(self):
        """Generate a synthetic demo frame with simulated classroom scene"""
        import random
        
        # Create a classroom-like background
        # Use a contiguous uint8 buffer so OpenCV drawing functions can write safely.
        frame = np.full((480, 640, 3), (30, 30, 40), dtype=np.uint8)  # Dark blue background
        
        # Add some classroom elements
        cv2.rectangle(frame, (50, 50), (590, 150), (60, 60, 80), -1)  # Board
        cv2.putText(frame, "CS101 - Computer Science", (70, 100), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        
        # Add simulated student faces
        positions = [(100, 200), (250, 200), (400, 200), (550, 200),
                    (100, 320), (250, 320), (400, 320), (550, 320)]
        
        for i, (x, y) in enumerate(positions):
            # Face rectangle
            cv2.rectangle(frame, (x-30, y-40), (x+30, y+40), (100, 150, 200), -1)
            cv2.rectangle(frame, (x-30, y-40), (x+30, y+40), (200, 200, 255), 2)
            
            # Random emotion labels
            emotions = ['Happy', 'Neutral', 'Focused', 'Confused']
            emotion = random.choice(emotions)
            cv2.putText(frame, emotion, (x-25, y+5), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
            
            # Student ID
            cv2.putText(frame, f"STU{i+1:03d}", (x-20, y+55), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.3, (150, 150, 150), 1)
        
        # Add timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, f"Demo Mode - {timestamp}", (10, 460), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 255, 100), 1)
        
        return frame

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
            # Always show annotated frame if available, otherwise current frame
            if self.annotated_frame is not None:
                frame = self.annotated_frame
            elif self.current_frame is not None:
                frame = self.current_frame
            else:
                # Return a placeholder frame with status message
                placeholder = np.full((480, 640, 3), (30, 30, 40), dtype=np.uint8)
                cv2.putText(placeholder, "🎥 Camera Initializing...",
                            (80, 240), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 150, 255), 2)
                cv2.putText(placeholder, "Please wait for video feed to appear",
                            (100, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100, 100, 100), 1)
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
    logger.info(f"🎥 start_stream called with source={source}, session_id={session_id}")
    logger.info(f"🎥 Stream is_running: {stream.is_running}")
    
    if not stream.is_running:
        result = stream.start(source=source, session_id=session_id)
        logger.info(f"🎥 Stream start result: {result}")
        return result
    else:
        logger.warning("🎥 Stream is already running, returning True")
        return True


def stop_stream():
    """Stop video stream with immediate camera release"""
    try:
        stream = get_video_stream()
        if stream.is_running:
            logger.info("🛑 Stopping video stream...")
            stream.stop()
            
            # Wait a moment to ensure cleanup
            import time
            time.sleep(0.2)
            
            # Double-check that stream is stopped
            if stream.is_running:
                logger.warning("⚠️ Stream still running, forcing stop...")
                stream.is_running = False
                if stream.cap:
                    stream.cap.release()
                    stream.cap = None
            
            logger.info("✅ Video stream stopped successfully")
            return True
        else:
            logger.info("ℹ️ Stream was not running")
            return True
    except Exception as e:
        logger.error(f"❌ Error stopping stream: {e}")
        return False


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
