"""Train a lightweight FER-2013 emotion model from folder-structured images.

Expected dataset layout:
- <dataset-root>/train/<emotion_name>/*.jpg
- <dataset-root>/test/<emotion_name>/*.jpg

Supported emotion folder names:
angry, disgust, fear, happy, neutral, sad, surprise
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engagement.fer_emotion_model import FER2013Net, LABEL_TO_INDEX


def build_arg_parser():
    p = argparse.ArgumentParser(description="Train FER-2013 emotion model")
    p.add_argument("--dataset-root", required=True, help="Path to FER-2013 root folder")
    p.add_argument("--output", default="media/models/fer2013_emotion.pt", help="Output checkpoint path")
    p.add_argument("--epochs", type=int, default=6)
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--input-size", type=int, default=48)
    p.add_argument("--max-train", type=int, default=0, help="Optional cap for training images (0 = full)")
    p.add_argument("--max-test", type=int, default=0, help="Optional cap for test images (0 = full)")
    return p


def _collect_split_rows(split_dir: Path):
    rows = []
    if not split_dir.exists():
        return rows

    for emotion_name, idx in LABEL_TO_INDEX.items():
        class_dir = split_dir / emotion_name
        if not class_dir.exists():
            continue

        for img_path in class_dir.rglob("*"):
            if img_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
                continue
            rows.append((img_path, idx))

    random.shuffle(rows)
    return rows


class FERFolderDataset:
    def __init__(self, rows, input_size: int):
        self.rows = rows
        self.input_size = int(input_size)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        image_path, label_idx = self.rows[idx]
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"Cannot read image: {image_path}")

        image = cv2.resize(image, (self.input_size, self.input_size), interpolation=cv2.INTER_AREA)
        x = image.astype(np.float32) / 255.0
        x = x[None, :, :]
        y = int(label_idx)
        return x, y


def collate_batch(batch, torch):
    xs = np.stack([b[0] for b in batch], axis=0)
    ys = np.array([b[1] for b in batch], dtype=np.int64)
    return torch.from_numpy(xs), torch.from_numpy(ys)


def evaluate(net, loader, criterion, torch, device):
    net.eval()
    total_loss = 0.0
    total = 0
    correct = 0

    with torch.no_grad():
        for x, y in loader:
            x = x.to(device)
            y = y.to(device)
            logits = net(x)
            loss = criterion(logits, y)
            total_loss += float(loss.item()) * y.size(0)
            preds = torch.argmax(logits, dim=1)
            correct += int((preds == y).sum().item())
            total += int(y.size(0))

    if total == 0:
        return 0.0, 0.0
    return total_loss / total, correct / total


def main():
    args = build_arg_parser().parse_args()

    import torch
    from torch.utils.data import DataLoader

    dataset_root = Path(args.dataset_root)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    train_rows = _collect_split_rows(dataset_root / "train")
    test_rows = _collect_split_rows(dataset_root / "test")

    if args.max_train > 0:
        train_rows = train_rows[: min(len(train_rows), args.max_train)]
    if args.max_test > 0:
        test_rows = test_rows[: min(len(test_rows), args.max_test)]

    if len(train_rows) < 100:
        raise ValueError("Not enough training images found. Need at least 100.")
    if len(test_rows) < 20:
        raise ValueError("Not enough test images found. Need at least 20.")

    train_ds = FERFolderDataset(train_rows, input_size=args.input_size)
    test_ds = FERFolderDataset(test_rows, input_size=args.input_size)

    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
        collate_fn=lambda batch: collate_batch(batch, torch),
    )
    test_loader = DataLoader(
        test_ds,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=0,
        collate_fn=lambda batch: collate_batch(batch, torch),
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    net = FER2013Net().to(device)

    train_labels = np.array([label for _path, label in train_rows], dtype=np.int64)
    label_counts = np.bincount(train_labels, minlength=7).astype(np.float32)
    label_counts[label_counts == 0] = 1.0
    class_weights = (label_counts.sum() / label_counts)
    class_weights = class_weights / class_weights.mean()

    weight_tensor = torch.tensor(class_weights, dtype=torch.float32, device=device)
    criterion = torch.nn.CrossEntropyLoss(weight=weight_tensor)
    optimizer = torch.optim.Adam(net.parameters(), lr=args.lr)

    print(f"Train samples: {len(train_ds)} | Test samples: {len(test_ds)}")
    print(f"Class counts: {label_counts.tolist()}")

    best_test_acc = -1.0
    for epoch in range(args.epochs):
        net.train()
        running_loss = 0.0
        running_total = 0

        for x, y in train_loader:
            x = x.to(device)
            y = y.to(device)

            optimizer.zero_grad()
            logits = net(x)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()

            running_loss += float(loss.item()) * y.size(0)
            running_total += int(y.size(0))

        train_loss = running_loss / max(1, running_total)
        test_loss, test_acc = evaluate(net, test_loader, criterion, torch, device)
        print(
            f"Epoch {epoch + 1}/{args.epochs} "
            f"train_loss={train_loss:.4f} test_loss={test_loss:.4f} test_acc={test_acc:.4f}"
        )

        if test_acc > best_test_acc:
            best_test_acc = test_acc
            torch.save(
                {
                    "model_state": net.state_dict(),
                    "input_size": int(args.input_size),
                    "best_test_acc": float(best_test_acc),
                    "labels": list(LABEL_TO_INDEX.keys()),
                },
                output_path,
            )
            print(f"Saved checkpoint: {output_path}")

    print(f"Training complete. Best test_acc={best_test_acc:.4f}")


if __name__ == "__main__":
    main()
