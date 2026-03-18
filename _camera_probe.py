import cv2

backends = [
    ("CAP_DSHOW", cv2.CAP_DSHOW),
    ("CAP_MSMF", cv2.CAP_MSMF),
    ("CAP_FFMPEG", cv2.CAP_FFMPEG),
    ("AUTO", None),
]

for idx in [0,1,2,3]:
    print(f"\\n=== Camera index {idx} ===")
    any_ok = False
    for name, b in backends:
        try:
            cap = cv2.VideoCapture(idx) if b is None else cv2.VideoCapture(idx, b)
            opened = cap.isOpened()
            ret, frame = (cap.read() if opened else (False, None))
            shape = None if frame is None else frame.shape
            print(f"{name:10s} opened={opened} ret={ret} shape={shape}")
            if opened and ret:
                any_ok = True
            cap.release()
        except Exception as e:
            print(f"{name:10s} error={e}")
    print(f"Result index {idx}: {'WORKING' if any_ok else 'NOT WORKING'}")
