"""DAiSEE engagement model training and inference helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np


LABEL_TO_INDEX = {
    "very_low": 0,
    "low": 1,
    "high": 2,
    "very_high": 3,
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
}

INDEX_TO_SCORE = {
    0: 10.0,
    1: 30.0,
    2: 65.0,
    3: 85.0,
}


class DAiSEENet:
    """Small CNN that predicts DAiSEE engagement class logits."""

    def __init__(self):
        import torch
        import torch.nn as nn

        self.model = nn.Sequential(
            nn.Conv2d(3, 16, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d((1, 1)),
            nn.Flatten(),
            nn.Linear(64, 64),
            nn.ReLU(inplace=True),
            nn.Dropout(0.2),
            nn.Linear(64, 4),
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
class DAiSEEPrediction:
    score: float
    confidence: float
    class_index: int


class DAiSEEPredictor:
    """Loads a trained DAiSEE model checkpoint and predicts engagement score."""

    def __init__(self, checkpoint_path: str, input_size: int = 96):
        import torch

        self.torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.net = DAiSEENet().to(self.device)
        self.net.eval()
        self.input_size = input_size

        checkpoint = torch.load(checkpoint_path, map_location=self.device)
        if isinstance(checkpoint, dict) and "model_state" in checkpoint:
            self.net.load_state_dict(checkpoint["model_state"])
            self.input_size = int(checkpoint.get("input_size", input_size))
        else:
            self.net.load_state_dict(checkpoint)

    def _preprocess(self, image_bgr: np.ndarray):
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(image_rgb, (self.input_size, self.input_size), interpolation=cv2.INTER_AREA)
        x = resized.astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))
        x = self.torch.from_numpy(x).unsqueeze(0).to(self.device)
        return x

    def predict(self, face_roi: np.ndarray) -> Optional[DAiSEEPrediction]:
        if face_roi is None or getattr(face_roi, "size", 0) == 0:
            return None

        with self.torch.no_grad():
            x = self._preprocess(face_roi)
            logits = self.net(x)
            probs = self.torch.softmax(logits, dim=1)[0].detach().cpu().numpy()

        class_idx = int(np.argmax(probs))
        confidence = float(probs[class_idx])
        score = float(sum(INDEX_TO_SCORE[i] * float(probs[i]) for i in range(4)))
        return DAiSEEPrediction(score=score, confidence=confidence, class_index=class_idx)


def parse_label(label_value) -> Optional[int]:
    if label_value is None:
        return None
    key = str(label_value).strip().lower()
    return LABEL_TO_INDEX.get(key)
