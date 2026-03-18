"""
SmartClass Monitor - Face Recognition System
Identifies registered students during live monitoring
"""

import cv2
import numpy as np
import logging
import json
import ast
from engagement.models import Student

logger = logging.getLogger(__name__)

class FaceRecognitionSystem:
    """
    Face recognition system for identifying registered students
    """
    
    def __init__(self):
        self.student_encodings = {}
        self.load_student_encodings()
        logger.info("✅ FaceRecognitionSystem initialized")
    
    def load_student_encodings(self):
        """Load all active student face encodings from database."""
        try:
            self.student_encodings.clear()

            # Load every active student with a stored encoding.
            students = Student.objects.filter(
                is_active=True, 
                face_encoding__isnull=False
            )
            
            for student in students:
                if student.face_encoding:
                    encoding = None

                    if isinstance(student.face_encoding, str):
                        try:
                            encoding = np.array(json.loads(student.face_encoding), dtype=np.float32)
                        except Exception:
                            # Backward-compatibility for legacy non-JSON stringified lists.
                            encoding = np.array(ast.literal_eval(student.face_encoding), dtype=np.float32)
                    else:
                        encoding = np.array(student.face_encoding, dtype=np.float32)

                    if encoding is None or encoding.size == 0:
                        continue

                    self.student_encodings[student.student_id] = {
                        'encoding': encoding,
                        'name': student.name,
                        'id': student.id,
                        'student_id': student.student_id,
                    }
            
            logger.info(f"✅ Loaded {len(self.student_encodings)} student face encodings")
            
        except Exception as e:
            logger.error(f"❌ Error loading student encodings: {e}")
    
    def identify_student(self, face_roi, confidence_threshold=0.55):
        """
        Identify a student from face ROI with improved accuracy
        Returns: (student_id, student_name, confidence) or (None, None, 0)
        """
        try:
            if not self.student_encodings:
                logger.warning("❌ No student encodings loaded")
                return None, None, 0
            
            # Generate encoding for detected face
            face_encoding = self.generate_face_encoding(face_roi)
            if face_encoding is None:
                logger.warning("❌ Failed to generate face encoding for detected face")
                return None, None, 0
            
            face_encoding = np.array(face_encoding)
            logger.info(f"🔍 Analyzing detected face with encoding length: {len(face_encoding)}")
            
            # Compare with all stored encodings
            best_match = None
            best_confidence = 0
            second_best_confidence = 0
            all_similarities = []
            
            for student_id, student_data in self.student_encodings.items():
                try:
                    stored_encoding = np.array(student_data['encoding'])

                    if stored_encoding.shape != face_encoding.shape:
                        logger.debug(
                            "Skipping %s due to encoding shape mismatch (detected=%s, stored=%s)",
                            student_id,
                            face_encoding.shape,
                            stored_encoding.shape,
                        )
                        continue
                    
                    # Calculate similarity (cosine similarity)
                    similarity = self.calculate_similarity(face_encoding, stored_encoding)
                    all_similarities.append((student_id, student_data['name'], similarity))
                    
                    if similarity > best_confidence:
                        second_best_confidence = best_confidence
                        best_confidence = similarity
                        best_match = student_data
                    elif similarity > second_best_confidence:
                        second_best_confidence = similarity
                        
                except Exception as e:
                    logger.error(f"❌ Error comparing with student {student_id}: {e}")
                    continue
            
            # Log top 3 matches for debugging
            all_similarities.sort(key=lambda x: x[2], reverse=True)
            logger.info(f"🎯 Top 3 matches: {all_similarities[:3]}")
            
            # Accept match only if confidence passes threshold and is sufficiently separated from runner-up.
            confidence_margin = best_confidence - second_best_confidence
            if best_match and best_confidence >= confidence_threshold and confidence_margin >= 0.03:
                return best_match['student_id'], best_match['name'], best_confidence

            logger.info(
                "No reliable match: best=%.3f second=%.3f margin=%.3f threshold=%.2f",
                best_confidence,
                second_best_confidence,
                confidence_margin,
                confidence_threshold,
            )
            return None, None, 0
            
        except Exception as e:
            logger.error(f"❌ Face identification error: {e}")
            return None, None, 0
    
    def generate_face_encoding(self, face_roi):
        """Generate face encoding in the same format used during student registration."""
        try:
            if face_roi is None or face_roi.size == 0:
                logger.warning("❌ Invalid face ROI provided")
                return None

            face_resized = cv2.resize(face_roi, (64, 64))
            face_gray = cv2.cvtColor(face_resized, cv2.COLOR_BGR2GRAY)

            encoding = []
            encoding.extend([face_roi.shape[0], face_roi.shape[1]])

            for channel in range(3):
                hist = cv2.calcHist([face_resized], [channel], None, [16], [0, 256])
                encoding.extend(hist.flatten())

            gray_hist = cv2.calcHist([face_gray], [0], None, [8], [0, 256])
            encoding.extend(gray_hist.flatten())

            encoding = np.array(encoding, dtype=np.float32)
            encoding = encoding / (np.linalg.norm(encoding) + 1e-5)

            logger.info(f"✅ Generated face encoding with {len(encoding)} features")
            return encoding.tolist()
            
        except Exception as e:
            logger.error(f"❌ Face encoding error: {e}")
            return None
    
    def calculate_similarity(self, encoding1, encoding2):
        """Calculate similarity between two face encodings"""
        try:
            # Ensure both encodings are numpy arrays
            enc1 = np.array(encoding1)
            enc2 = np.array(encoding2)

            # Ignore the first 2 metadata features (original ROI dimensions),
            # which are highly distance-dependent and hurt identity matching.
            if enc1.size > 2 and enc2.size > 2:
                enc1 = enc1[2:]
                enc2 = enc2[2:]
            
            # Calculate cosine similarity
            dot_product = np.dot(enc1, enc2)
            norm1 = np.linalg.norm(enc1)
            norm2 = np.linalg.norm(enc2)
            
            if norm1 == 0 or norm2 == 0:
                return 0
            
            similarity = dot_product / (norm1 * norm2)
            
            # Ensure similarity is between 0 and 1
            return max(0, min(1, similarity))
            
        except Exception as e:
            logger.error(f"Similarity calculation error: {e}")
            return 0
    
    def refresh_encodings(self):
        """Refresh student encodings from database"""
        self.student_encodings.clear()
        self.load_student_encodings()
        logger.info("✅ Student encodings refreshed")

# Global instance
_face_recognition_system = None

def get_face_recognition_system():
    """Get global face recognition system instance"""
    global _face_recognition_system
    if _face_recognition_system is None:
        _face_recognition_system = FaceRecognitionSystem()
    return _face_recognition_system
