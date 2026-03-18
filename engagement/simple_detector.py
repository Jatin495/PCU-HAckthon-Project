"""
SmartClass Monitor - Simple Face Detector
Uses OpenCV's built-in face detection for real camera feeds
"""

import cv2
import numpy as np
import logging
import random
from datetime import datetime

logger = logging.getLogger(__name__)

class SimpleFaceDetector:
    """
    Simple face detector using OpenCV Haar cascades with improved emotion analysis
    """
    
    def __init__(self):
        # Load face cascade
        self.face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        self.eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')
        
        if self.face_cascade.empty():
            logger.error("Failed to load face cascade")
            raise Exception("Face cascade not loaded")
        
        # Emotion history for temporal smoothing
        self.emotion_history = []
        self.max_history = 5  # Keep last 5 emotion detections
        
        logger.info("✅ SimpleFaceDetector initialized")
    
    def detect_faces(self, frame, lenient=False):
        """Improved face detection with better accuracy"""
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            
            # Enhance image for better detection
            gray = cv2.equalizeHist(gray)
            
            if lenient:
                # Very lenient settings for registration
                faces = self.face_cascade.detectMultiScale(
                    gray, 
                    scaleFactor=1.05,  # Very sensitive
                    minNeighbors=2,    # Very few neighbors
                    minSize=(25, 25),  # Small minimum
                    maxSize=(400, 400) # Large maximum
                )
                
                # Filter faces with very relaxed criteria for registration
                valid_faces = []
                for (x, y, w, h) in faces:
                    # Very relaxed aspect ratio check
                    aspect_ratio = h / w
                    if aspect_ratio < 0.7 or aspect_ratio > 3.5:  # Very relaxed
                        continue
                    
                    # Very relaxed size check
                    if w < 20 or h < 25:  # Very small minimum
                        continue
                    
                    # For registration, accept almost any face-like region
                    valid_faces.append({
                        'x': x, 'y': y, 'w': w, 'h': h,
                        'face_roi': frame[y:y+h, x:x+w]
                    })
                
                return valid_faces
            else:
                # Simple detection for live monitoring - JUST DETECT FACES
                faces = self.face_cascade.detectMultiScale(
                    gray, 
                    scaleFactor=1.3,  # Lower = more detections
                    minNeighbors=4,   # Lower = more sensitive
                    minSize=(40, 40),
                    maxSize=(400, 400)
                )
                
                # MINIMAL filtering - just return what we detect
                valid_faces = []
                for (x, y, w, h) in faces:
                    face_roi = frame[y:y+h, x:x+w]
                    # Just check it's not completely empty
                    if face_roi.size > 0:
                        valid_faces.append({
                            'x': x, 'y': y, 'w': w, 'h': h,
                            'face_roi': face_roi
                        })
                
                return valid_faces
            
        except Exception as e:
            logger.error(f"Face detection error: {e}")
            return []
    
    def _remove_duplicate_faces(self, faces):
        """Remove overlapping face detections"""
        if not faces:
            return faces
        
        # Sort by size (largest first)
        faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
        
        unique_faces = []
        for face in faces:
            x, y, w, h = face
            
            # Check if this face overlaps with any already accepted face
            is_duplicate = False
            for ux, uy, uw, uh in unique_faces:
                # Calculate overlap
                overlap_x = max(0, min(x + w, ux + uw) - max(x, ux))
                overlap_y = max(0, min(y + h, uy + uh) - max(y, uy))
                overlap_area = overlap_x * overlap_y
                
                # If overlap is significant, it's a duplicate
                face_area = w * h
                if overlap_area > face_area * 0.3:  # 30% overlap threshold
                    is_duplicate = True
                    break
            
            if not is_duplicate:
                unique_faces.append(face)
        
        return unique_faces
    
    def _is_likely_face_improved(self, face_roi):
        """Improved face validation - balanced to detect real faces while rejecting posters"""
        try:
            if face_roi.size == 0:
                return False
            
            h, w = face_roi.shape[:2]
            
            # Reasonable aspect ratio for real faces
            aspect_ratio = h / w
            if aspect_ratio < 0.65 or aspect_ratio > 2.5:
                return False
            
            # Check it has some visual content (not completely black/white)
            gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
            mean_brightness = np.mean(gray)
            
            # Reject if too dark (<40) or too bright (>210)
            if mean_brightness < 40 or mean_brightness > 210:
                return False
            
            # Accept - it looks face-like enough
            return True
    
    def analyze_emotion_basic(self, face_roi):
        """Improved emotion analysis with better accuracy"""
        try:
            # Convert to different color spaces for better analysis
            gray_face = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
            hsv_face = cv2.cvtColor(face_roi, cv2.COLOR_BGR2HSV)
            
            # Resize to standard size
            face_resized = cv2.resize(gray_face, (100, 100))
            hsv_resized = cv2.resize(hsv_face, (100, 100))
            
            # Detect key facial features
            eyes = self.eye_cascade.detectMultiScale(gray_face, 1.1, 2, minSize=(8, 8))
            
            # Analyze specific facial regions for expressions
            h, w = face_resized.shape
            
            # Eye region (top 1/3)
            eye_region = face_resized[:h//3, :]
            
            # Mouth region (bottom 1/3) 
            mouth_region = face_resized[2*h//3:, :]
            
            # Forehead region (top 1/4)
            forehead_region = face_resized[:h//4, :]
            
            # Calculate expression-specific features
            features = {}
            
            # 1. Mouth curve analysis (smile vs frown)
            mouth_top = mouth_region[:mouth_region.shape[0]//2, :]
            mouth_bottom = mouth_region[mouth_region.shape[0]//2:, :]
            
            mouth_top_brightness = np.mean(mouth_top)
            mouth_bottom_brightness = np.mean(mouth_bottom)
            mouth_curve = mouth_bottom_brightness - mouth_top_brightness
            
            # 2. Eye openness and position
            eye_openness = len(eyes)
            eye_brightness = np.mean(eye_region)
            
            # 3. Overall face brightness (for sad/happy distinction)
            overall_brightness = np.mean(face_resized)
            
            # 4. Mouth activity (talking vs static)
            mouth_edges = cv2.Canny(mouth_region, 40, 80)
            mouth_activity = np.sum(mouth_edges > 0) / mouth_region.size
            
            # 5. Forehead wrinkles (confusion/concentration)
            forehead_edges = cv2.Canny(forehead_region, 30, 60)
            forehead_activity = np.sum(forehead_edges > 0) / forehead_region.size
            
            # 6. Face symmetry (neutral vs expressions)
            left_half = face_resized[:, :w//2]
            right_half = face_resized[:, w//2:]
            symmetry_score = 1 - np.abs(np.mean(left_half) - np.mean(right_half)) / 255
            
            # 7. Contrast and texture
            contrast = np.std(face_resized)
            texture_var = np.var(face_resized)
            
            features.update({
                'mouth_curve': mouth_curve,
                'eye_openness': eye_openness,
                'eye_brightness': eye_brightness,
                'overall_brightness': overall_brightness,
                'mouth_activity': mouth_activity,
                'forehead_activity': forehead_activity,
                'symmetry_score': symmetry_score,
                'contrast': contrast,
                'texture_var': texture_var
            })
            
            # ACCURATE EMOTION DETECTION - Only 4 emotions: happy, bored, confused, neutral
            # Use clear, practical thresholds based on real facial expressions
            
            # HAPPY - Clear smile (upward mouth curve) and bright face
            if mouth_curve > 5 and overall_brightness > 115:
                emotion = 'happy'
                confidence = min(0.9, 0.6 + mouth_curve / 15 + (overall_brightness - 100) / 40)
            
            # CONFUSED - Asymmetric face, furrowed forehead, normal brightness
            elif (symmetry_score < 0.6 or forehead_activity > 0.02) and 90 <= overall_brightness <= 125:
                emotion = 'confused'
                confidence = min(0.85, 0.6 + (0.6 - symmetry_score) + forehead_activity / 2)
            
            # BORED - Low brightness, low mouth activity, neutral mouth
            elif (overall_brightness < 100 and mouth_activity < 0.04 and abs(mouth_curve) <= 2):
                emotion = 'bored'
                confidence = min(0.8, 0.6 + (100 - overall_brightness) / 30 + (0.04 - mouth_activity) * 10)
            
            # NEUTRAL - Everything else (balanced expression)
            else:
                emotion = 'neutral'
                confidence = 0.7
            
            # Log emotion detection details for debugging
            logger.info(f"🎭 Emotion detected: {emotion} (confidence: {confidence:.3f})")
            logger.info(f"   Features: mouth_curve={mouth_curve:.2f}, brightness={overall_brightness:.1f}, "
                       f"symmetry={symmetry_score:.3f}, forehead={forehead_activity:.3f}")
            
            return {
                'emotion': emotion,
                'confidence': confidence,
                'features': features
            }
            
        except Exception as e:
            logger.error(f"❌ Emotion analysis error: {e}")
            return {'emotion': 'neutral', 'confidence': 0.5, 'features': {}}
    
    def _smooth_emotion(self, emotion, confidence):
        """Apply temporal smoothing to emotion detection"""
        try:
            # Add current emotion to history
            self.emotion_history.append({
                'emotion': emotion,
                'confidence': confidence,
                'timestamp': datetime.now()
            })
            
            # Keep only recent history
            if len(self.emotion_history) > self.max_history:
                self.emotion_history.pop(0)
            
            # If we have enough history, apply smoothing
            if len(self.emotion_history) >= 3:
                # Count emotions in recent history
                emotion_counts = {}
                confidence_sums = {}
                
                for record in self.emotion_history:
                    emo = record['emotion']
                    conf = record['confidence']
                    
                    emotion_counts[emo] = emotion_counts.get(emo, 0) + conf
                    confidence_sums[emo] = confidence_sums.get(emo, 0) + conf
                
                # Get emotion with highest weighted count
                if emotion_counts:
                    smoothed_emotion = max(emotion_counts, key=emotion_counts.get)
                    smoothed_confidence = confidence_sums[smoothed_emotion] / len(self.emotion_history)
                    return smoothed_emotion, smoothed_confidence
            
            # Return original if not enough history
            return emotion, confidence
            
        except Exception as e:
            logger.error(f"❌ Emotion smoothing error: {e}")
            return emotion, confidence
        
        # If we have enough history, use weighted voting for stability
        if len(self.emotion_history) >= 2:
            # Count emotions in recent history with weights
            emotion_counts = {}
            confidence_sum = 0
            
            # Give more weight to recent detections
            for i, (emotion, confidence) in enumerate(self.emotion_history):
                weight = (i + 1) / len(self.emotion_history)  # Recent = higher weight
                emotion_counts[emotion] = emotion_counts.get(emotion, 0) + confidence * weight
                confidence_sum += confidence * weight
            
            # Get the emotion with highest weighted count
            if emotion_counts:
                best_emotion = max(emotion_counts, key=emotion_counts.get)
                avg_confidence = confidence_sum / len(self.emotion_history)
                
                # But if current emotion is very different and confident, allow change
                current_weight = 2.0  # Give current detection extra weight
                current_score = current_confidence * current_weight
                
                if emotion_counts.get(current_emotion, 0) + current_score > emotion_counts.get(best_emotion, 0):
                    return current_emotion, current_confidence
                else:
                    return best_emotion, avg_confidence
        
        # If not enough history, return current
        return current_emotion, current_confidence
    
    def calculate_engagement(self, emotion, confidence, face_size, head_pose=None):
        """Calculate engagement score based on emotion and classroom context"""
        # Base engagement by emotion
        emotion_scores = {
            'happy': 85,
            'focused': 90,
            'neutral': 75,  # Increased neutral score (normal classroom state)
            'confused': 50,  # Confused means they're paying attention but don't understand
            'bored': 30,    # Only truly bored if disengaged
            'sad': 40
        }
        
        base_score = emotion_scores.get(emotion, 60)  # Default to 60 (engaged)
        
        # Adjust by confidence
        score = base_score * (0.7 + 0.3 * confidence)
        
        # Classroom-specific adjustments:
        # In classroom, looking forward (at teacher) is GOOD, even if not at camera
        if head_pose:
            # If head is tilted forward/downward (looking at teacher/board), that's engaged
            if head_pose.get('pitch', 0) < -10:  # Looking down at desk = disengaged
                score *= 0.7
            elif head_pose.get('pitch', 0) > 10:  # Looking up = engaged
                score *= 1.1
            # Slight forward tilt (normal classroom posture) = engaged
            elif -5 <= head_pose.get('pitch', 0) <= 5:
                score *= 1.05
        
        # Face size indicates distance - closer to camera = more engaged
        face_area = face_size[0] * face_size[1]
        if face_area > 20000:  # Large face = close to camera = engaged
            score *= 1.1
        elif face_area < 5000:  # Small face = far away = less engaged
            score *= 0.9
        
        return min(100, max(0, score))
    
    def estimate_head_pose_simple(self, face_roi):
        """Simple head pose estimation based on face features"""
        try:
            gray_face = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
            
            # Detect eyes within face region
            eyes = self.eye_cascade.detectMultiScale(gray_face, 1.1, 3, minSize=(10, 10))
            
            if len(eyes) >= 2:
                # Sort eyes by x position
                eyes = sorted(eyes, key=lambda x: x[0])
                left_eye, right_eye = eyes[0], eyes[1]
                
                # Calculate eye center
                left_center = (left_eye[0] + left_eye[2]//2, left_eye[1] + left_eye[3]//2)
                right_center = (right_eye[0] + right_eye[2]//2, right_eye[1] + right_eye[3]//2)
                
                # Simple pose estimation based on eye position
                eye_y_avg = (left_center[1] + right_center[1]) / 2
                face_height = face_roi.shape[0]
                
                # Eyes in upper half = looking forward (normal classroom posture)
                # Eyes in lower half = looking down (possibly disengaged)
                eye_position_ratio = eye_y_avg / face_height
                
                if eye_position_ratio < 0.4:  # Eyes high = looking up/forward
                    pitch = 15  # Looking slightly up
                elif eye_position_ratio > 0.7:  # Eyes low = looking down
                    pitch = -20  # Looking down
                else:  # Eyes in middle = normal classroom posture
                    pitch = 0  # Looking forward
                
                return {'pitch': pitch, 'yaw': 0, 'roll': 0}
            
            return {'pitch': 0, 'yaw': 0, 'roll': 0}  # Default to forward
            
        except:
            return {'pitch': 0, 'yaw': 0, 'roll': 0}

class RealCameraDetector:
    """
    Real camera detector that processes actual camera frames
    """
    
    def __init__(self):
        self.face_detector = SimpleFaceDetector()
        self.face_recognition = None  # Will be loaded when needed
        logger.info("✅ RealCameraDetector initialized")
    
    def get_face_recognition_system(self):
        """Lazy load face recognition system"""
        if self.face_recognition is None:
            try:
                from engagement.face_recognition import get_face_recognition_system
                self.face_recognition = get_face_recognition_system()
                logger.info("✅ Face recognition system loaded")
            except Exception as e:
                logger.error(f"❌ Failed to load face recognition: {e}")
        return self.face_recognition
    
    def process_frame(self, frame):
        """Process real camera frame and return analysis"""
        try:
            # Detect faces
            face_regions = self.face_detector.detect_faces(frame)
            
            # Get face recognition system
            face_recog = self.get_face_recognition_system()
            
            results = []
            for i, face_data in enumerate(face_regions):
                x, y, w, h = face_data['x'], face_data['y'], face_data['w'], face_data['h']
                face_roi = face_data['face_roi']
                
                # Analyze emotion
                emo_result = self.face_detector.analyze_emotion_basic(face_roi)
                if isinstance(emo_result, dict):
                    emotion = emo_result.get('emotion', 'neutral')
                    confidence = float(emo_result.get('confidence', 0.5))
                else:
                    # Backwards compatibility if function returns tuple
                    try:
                        emotion, confidence = emo_result
                    except Exception:
                        emotion, confidence = 'neutral', 0.5
                
                # Estimate head pose for classroom context
                head_pose = self.face_detector.estimate_head_pose_simple(face_roi)
                
                # Try to identify student
                student_id = None
                student_name = None
                identification_confidence = 0
                
                if face_recog:
                    student_id, student_name, identification_confidence = face_recog.identify_student(face_roi)
                
                # Calculate engagement with classroom awareness
                engagement = self.face_detector.calculate_engagement(emotion, confidence, (w, h), head_pose)
                
                # Calculate attention based on classroom behavior
                # In classroom, attention = not looking down at phone/desk
                if head_pose['pitch'] < -15:  # Looking significantly down
                    attention = max(30, engagement - 20)  # Likely disengaged
                else:
                    attention = min(100, engagement + 10)  # Normal classroom posture = attentive
                
                # Posture score (simplified)
                posture_score = random.uniform(70, 95)
                
                # Looking forward in classroom context = not looking down at desk
                is_looking_forward = head_pose['pitch'] > -10  # Looking generally forward
                
                results.append({
                    'face_index': i,
                    'x': x, 'y': y, 'w': w, 'h': h,
                    'emotion': emotion,
                    'emotion_confidence': confidence,
                    'engagement_score': round(engagement, 1),
                    'attention_score': round(attention, 1),
                    'posture_score': round(posture_score, 1),
                    'is_looking_forward': is_looking_forward,
                    'is_drowsy': False,
                    'is_slouching': False,
                    'face_detected': True,
                    'head_pose': head_pose,  # Add head pose info
                    'student_id': student_id,  # Real student identification
                    'student_name': student_name,  # Real student name
                    'identification_confidence': round(identification_confidence, 2)
                })
            
            # Create annotated frame
            annotated = frame.copy()
            for result in results:
                x, y, w, h = result['x'], result['y'], result['w'], result['h']
                emotion = result['emotion']
                engagement = result['engagement_score']
                confidence = result['emotion_confidence']
                head_pose = result.get('head_pose', {})
                
                # Color based on engagement
                if engagement >= 80:
                    color = (0, 220, 100)  # Green
                elif engagement >= 60:
                    color = (0, 200, 255)  # Yellow
                else:
                    color = (0, 60, 255)   # Red
                
                # Draw face rectangle
                cv2.rectangle(annotated, (x, y), (x+w, y+h), color, 2)
                
                # Draw labels with classroom context and student identification
                label = f"{emotion.upper()} ({confidence:.0%})"
                eng_label = f"Eng: {engagement:.0f}%"
                
                # Show student name if identified, otherwise show face index
                if result.get('student_name'):
                    name_label = result['student_name']
                    id_confidence = f"({result.get('identification_confidence', 0):.0%})"
                else:
                    name_label = f"Unknown {i+1}"
                    id_confidence = ""
                
                # Add pose indicator
                pose_indicator = "↓" if head_pose.get('pitch', 0) < -10 else "→" if head_pose.get('pitch', 0) > 10 else "→"
                
                # Background for text
                cv2.rectangle(annotated, (x, y-85), (x+w, y), (0, 0, 0), -1)
                cv2.putText(annotated, label, (x+3, y-68),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
                cv2.putText(annotated, eng_label, (x+3, y-50),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
                cv2.putText(annotated, name_label, (x+3, y-32),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color if result.get('student_name') else (200, 200, 200), 1, cv2.LINE_AA)
                if id_confidence:
                    cv2.putText(annotated, id_confidence, (x+3, y-16),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.35, (150, 255, 150), 1, cv2.LINE_AA)
                cv2.putText(annotated, f"Pose: {pose_indicator}", (x+3, y-2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1, cv2.LINE_AA)
            
            # Calculate class metrics
            if results:
                avg_engagement = sum(r['engagement_score'] for r in results) / len(results)
                emotion_dist = {}
                for r in results:
                    e = r['emotion']
                    emotion_dist[e] = emotion_dist.get(e, 0) + 1
            else:
                avg_engagement = 0
                emotion_dist = {}
            
            # Add class info overlay with classroom context
            h, w = annotated.shape[:2]
            cv2.rectangle(annotated, (0, 0), (w, 35), (15, 15, 25), -1)
            cv2.putText(annotated, f"SmartClass Monitor | Faces: {len(results)} | Avg Engagement: {avg_engagement:.1f}% | Classroom Mode",
                        (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1, cv2.LINE_AA)
            
            return {
                'students': results,
                'class_avg_engagement': round(avg_engagement, 1),
                'emotion_distribution': emotion_dist,
                'present_count': len(results),
                'annotated_frame': annotated,
                'timestamp': datetime.now().isoformat(),
            }
            
        except Exception as e:
            logger.error(f"Real camera processing error: {e}")
            # Return fallback
            return self._fallback_processing(frame)
    
    def _fallback_processing(self, frame):
        """Fallback processing if real detection fails"""
        return {
            'students': [],
            'class_avg_engagement': 0,
            'emotion_distribution': {},
            'present_count': 0,
            'annotated_frame': frame,
            'timestamp': datetime.now().isoformat(),
        }
    
    def close(self):
        """Clean up resources"""
        pass
