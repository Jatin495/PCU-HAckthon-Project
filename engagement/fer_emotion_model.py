"""FER-2013 emotion model training and inference helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


FER2013_LABELS = [
    "angry",
    "disgust",
    "fear",
    "happy",
    "neutral",
    "sad",
    "surprise",
]
LABEL_TO_INDEX = {label: idx for idx, label in enumerate(FER2013_LABELS)}
INDEX_TO_LABEL = {idx: label for idx, label in enumerate(FER2013_LABELS)}


class FER2013Net:
    """Small CNN for FER-2013 7-class emotion classification."""

    def __init__(self):
        import torch
        import torch.nn as nn

        self.model = nn.Sequential(
            nn.Conv2d(1, 32, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d((1, 1)),
            nn.Flatten(),
            nn.Linear(128, 64),
            nn.ReLU(inplace=True),
            nn.Dropout(0.2),
            nn.Linear(64, 7),
        )
        self.torch = torch

    def to(self, device):
        self.model.to(device)
        return self

    def parameters(self):
        return self.model.parameters()

    def train(self):
        self.model.train()

    def eval(self):
        self.model.eval()

    def __call__(self, x):
        return self.model(x)

    def state_dict(self):
        return self.model.state_dict()

    def load_state_dict(self, state):
        return self.model.load_state_dict(state)


@dataclass
class FER2013Prediction:
    label: str
    confidence: float
    class_index: int
    probabilities: dict


class FER2013Predictor:
    """Loads a trained FER-2013 checkpoint and predicts one emotion label."""

    def __init__(self, checkpoint_path: str, input_size: int = 48):
        import torch

        self.torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.net = FER2013Net().to(self.device)
        self.net.eval()
        self.input_size = int(input_size)

        checkpoint = torch.load(checkpoint_path, map_location=self.device)
        if isinstance(checkpoint, dict) and "model_state" in checkpoint:
            self.net.load_state_dict(checkpoint["model_state"])
            self.input_size = int(checkpoint.get("input_size", input_size))
        else:
            self.net.load_state_dict(checkpoint)

    def _preprocess(self, image_bgr: np.ndarray):
        if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
            return None
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (self.input_size, self.input_size), interpolation=cv2.INTER_AREA)
        x = resized.astype(np.float32) / 255.0
        x = x[None, :, :]
        x = self.torch.from_numpy(x).unsqueeze(0).to(self.device)
        return x

    def predict(self, face_roi: np.ndarray) -> Optional[FER2013Prediction]:
        x = self._preprocess(face_roi)
        if x is None:
            return None

        with self.torch.no_grad():
            logits = self.net(x)
            probs = self.torch.softmax(logits, dim=1)[0].detach().cpu().numpy()

        class_idx = int(np.argmax(probs))
        label = INDEX_TO_LABEL.get(class_idx, "neutral")
        confidence = float(probs[class_idx])
        probabilities = {INDEX_TO_LABEL[i]: float(probs[i]) for i in range(len(FER2013_LABELS))}

        return FER2013Prediction(
            label=label,
            confidence=confidence,
            class_index=class_idx,
            probabilities=probabilities,
        )