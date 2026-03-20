"""Train a lightweight DAiSEE engagement model from extracted face/frame images.

Expected CSV columns:
- frame_path: relative or absolute path to image
- engagement: one of {very_low, low, high, very_high} or {0,1,2,3}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engagement.daisee_model import DAiSEENet, parse_label


def build_arg_parser():
    p = argparse.ArgumentParser(description="Train DAiSEE engagement model")
    p.add_argument("--csv", required=True, help="Path to labels CSV")
    p.add_argument("--image-root", default=".", help="Root directory for frame_path")
    p.add_argument("--output", default="media/models/daisee_engagement.pt", help="Output checkpoint")
    p.add_argument("--epochs", type=int, default=8)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--input-size", type=int, default=96)
    p.add_argument("--val-split", type=float, default=0.15)
    p.add_argument("--max-samples", type=int, default=0, help="Optional cap on rows for quick smoke runs (0 = full dataset)")
    return p


class FrameDataset:
    def __init__(self, rows, image_root: Path, input_size: int):
        self.rows = rows
        self.image_root = image_root
        self.input_size = input_size

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        row = self.rows[idx]
        image_path = Path(row["frame_path"])
        if not image_path.is_absolute():
            image_path = self.image_root / image_path

        image = cv2.imread(str(image_path))
        if image is None:
            raise ValueError(f"Cannot read image: {image_path}")

        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = cv2.resize(image, (self.input_size, self.input_size), interpolation=cv2.INTER_AREA)
        x = image.astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))
        y = int(row["label_idx"])
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
        for batch in loader:
            x, y = batch
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

    csv_path = Path(args.csv)
    image_root = Path(args.image_root)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(csv_path)
    if "frame_path" not in df.columns or "engagement" not in df.columns:
        raise ValueError("CSV must contain frame_path and engagement columns")

    df["label_idx"] = df["engagement"].map(parse_label)
    df = df.dropna(subset=["label_idx"]).copy()
    df["label_idx"] = df["label_idx"].astype(int)

    if args.max_samples > 0:
        df = df.sample(n=min(args.max_samples, len(df)), random_state=42).reset_index(drop=True)

    if len(df) < 50:
        raise ValueError("Not enough labeled rows after parsing. Need at least 50.")

    # Random split
    df = df.sample(frac=1.0, random_state=42).reset_index(drop=True)
    val_count = max(1, int(len(df) * args.val_split))
    val_rows = df.iloc[:val_count].to_dict("records")
    train_rows = df.iloc[val_count:].to_dict("records")

    train_ds = FrameDataset(train_rows, image_root=image_root, input_size=args.input_size)
    val_ds = FrameDataset(val_rows, image_root=image_root, input_size=args.input_size)

    print(f"Using samples: total={len(df)} train={len(train_ds)} val={len(val_ds)}")

    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
        collate_fn=lambda batch: collate_batch(batch, torch),
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=0,
        collate_fn=lambda batch: collate_batch(batch, torch),
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    net = DAiSEENet().to(device)
    label_counts = np.bincount(df["label_idx"].values, minlength=4).astype(np.float32)
    label_counts[label_counts == 0] = 1.0
    class_weights = (label_counts.sum() / label_counts)
    class_weights = class_weights / class_weights.mean()
    weight_tensor = torch.tensor(class_weights, dtype=torch.float32, device=device)
    print(f"Class counts: {label_counts.tolist()}")
    print(f"Class weights: {class_weights.round(3).tolist()}")

    criterion = torch.nn.CrossEntropyLoss(weight=weight_tensor)
    optimizer = torch.optim.Adam(net.parameters(), lr=args.lr)

    best_val_acc = -1.0
    for epoch in range(args.epochs):
        net.train()
        running_loss = 0.0
        running_total = 0

        for batch in train_loader:
            x, y = batch
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
        val_loss, val_acc = evaluate(net, val_loader, criterion, torch, device)
        print(f"Epoch {epoch + 1}/{args.epochs} train_loss={train_loss:.4f} val_loss={val_loss:.4f} val_acc={val_acc:.4f}")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(
                {
                    "model_state": net.state_dict(),
                    "input_size": int(args.input_size),
                    "best_val_acc": float(best_val_acc),
                },
                output_path,
            )
            print(f"Saved checkpoint: {output_path}")

    print(f"Training complete. Best val_acc={best_val_acc:.4f}")


if __name__ == "__main__":
    main()
