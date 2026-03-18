"""
Quick test for face detection improvements
"""
import cv2
import numpy as np

# Test the merge_overlapping_faces function
def test_merge_overlapping_faces():
    # Simulate overlapping face detections
    faces = [
        (100, 100, 80, 80),   # Face 1
        (105, 105, 75, 75),   # Face 2 (overlaps with Face 1)
        (300, 200, 70, 70),   # Face 3 (separate)
        (310, 210, 65, 65),   # Face 4 (overlaps with Face 3)
    ]
    
    print(f"Original faces: {len(faces)}")
    
    # Import the function from working_camera
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from working_camera import merge_overlapping_faces
    
    merged = merge_overlapping_faces(faces)
    print(f"After merging: {len(merged)}")
    
    for i, (x, y, w, h) in enumerate(merged):
        print(f"Face {i+1}: x={x}, y={y}, w={w}, h={h}")

if __name__ == "__main__":
    test_merge_overlapping_faces()
