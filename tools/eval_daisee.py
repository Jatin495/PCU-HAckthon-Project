"""Evaluate DAiSEE checkpoint on labeled frame CSV.

Outputs overall accuracy, macro-F1, weighted-F1, and confusion matrix.
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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Evaluate DAiSEE engagement model")
    p.add_argument("--csv", required=True, help="Path to labels CSV")
    p.add_argument("--image-root", default=".", help="Root directory for frame_path")
    p.add_argument("--checkpoint", required=True, help="Path to trained checkpoint")
    p.add_argument("--input-size", type=int, default=96)
    p.add_argument("--val-split", type=float, default=0.15)
    p.add_argument("--split", choices=["val", "all"], default="val")
    p.add_argument("--max-samples", type=int, default=0, help="Optional cap for quick checks")
    return p


def load_image_tensor(image_path: Path, input_size: int) -> np.ndarray | None:
    image = cv2.imread(str(image_path))
    if image is None:
        return None
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    image = cv2.resize(image, (input_size, input_size), interpolation=cv2.INTER_AREA)
    x = image.astype(np.float32) / 255.0
    x = np.transpose(x, (2, 0, 1))
    return x


def safe_div(a: float, b: float) -> float:
    return (a / b) if b else 0.0


def compute_scores(y_true: np.ndarray, y_pred: np.ndarray, num_classes: int = 4):
    cm = np.zeros((num_classes, num_classes), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        cm[int(t), int(p)] += 1

    total = int(cm.sum())
    accuracy = safe_div(float(np.trace(cm)), float(total))

    per_class = []
    macro_f1_sum = 0.0
    weighted_f1_sum = 0.0

    for c in range(num_classes):
        tp = float(cm[c, c])
        fp = float(cm[:, c].sum() - cm[c, c])
        fn = float(cm[c, :].sum() - cm[c, c])
        support = float(cm[c, :].sum())

        precision = safe_div(tp, tp + fp)
        recall = safe_div(tp, tp + fn)
        f1 = safe_div(2 * precision * recall, precision + recall) if (precision + recall) else 0.0

        per_class.append({
            "class": c,
            "support": int(support),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        })

        macro_f1_sum += f1
        weighted_f1_sum += f1 * support

    macro_f1 = macro_f1_sum / num_classes
    weighted_f1 = safe_div(weighted_f1_sum, float(total))

    return {
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "weighted_f1": weighted_f1,
        "per_class": per_class,
        "confusion_matrix": cm,
        "total": total,
    }


def main() -> None:
    args = build_arg_parser().parse_args()

    import torch

    csv_path = Path(args.csv)
    image_root = Path(args.image_root)
    checkpoint_path = Path(args.checkpoint)

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")

    df = pd.read_csv(csv_path)
    if "frame_path" not in df.columns or "engagement" not in df.columns:
        raise ValueError("CSV must contain frame_path and engagement columns")

    df["label_idx"] = df["engagement"].map(parse_label)
    df = df.dropna(subset=["label_idx"]).copy()
    df["label_idx"] = df["label_idx"].astype(int)

    if args.max_samples > 0:
        df = df.sample(n=min(args.max_samples, len(df)), random_state=42).reset_index(drop=True)

    if len(df) < 10:
        raise ValueError("Not enough samples for evaluation")

    # Use same random split as train script for comparable validation estimates.
    df = df.sample(frac=1.0, random_state=42).reset_index(drop=True)
    val_count = max(1, int(len(df) * args.val_split))
    eval_df = df.iloc[:val_count].copy() if args.split == "val" else df.copy()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    net = DAiSEENet().to(device)
    net.eval()

    checkpoint = torch.load(checkpoint_path, map_location=device)
    if isinstance(checkpoint, dict) and "model_state" in checkpoint:
        net.load_state_dict(checkpoint["model_state"])
        input_size = int(checkpoint.get("input_size", args.input_size))
    else:
        net.load_state_dict(checkpoint)
        input_size = int(args.input_size)

    y_true = []
    y_pred = []
    missing = 0

    with torch.no_grad():
        for _, row in eval_df.iterrows():
            image_path = Path(row["frame_path"])
            if not image_path.is_absolute():
                image_path = image_root / image_path

            x = load_image_tensor(image_path, input_size=input_size)
            if x is None:
                missing += 1
                continue

            xt = torch.from_numpy(x).unsqueeze(0).to(device)
            logits = net(xt)
            pred = int(torch.argmax(logits, dim=1).item())

            y_true.append(int(row["label_idx"]))
            y_pred.append(pred)

    if not y_true:
        raise RuntimeError("No readable images were found for evaluation")

    y_true_np = np.array(y_true, dtype=np.int64)
    y_pred_np = np.array(y_pred, dtype=np.int64)

    metrics = compute_scores(y_true_np, y_pred_np, num_classes=4)

    print(f"Eval split: {args.split}")
    print(f"Samples used: {metrics['total']} | Missing images: {missing}")
    print(f"Accuracy: {metrics['accuracy']:.4f}")
    print(f"Macro-F1: {metrics['macro_f1']:.4f}")
    print(f"Weighted-F1: {metrics['weighted_f1']:.4f}")
    print("Per-class metrics (0=very_low,1=low,2=high,3=very_high):")
    for row in metrics["per_class"]:
        print(
            f"  class {row['class']}: support={row['support']} "
            f"precision={row['precision']:.4f} recall={row['recall']:.4f} f1={row['f1']:.4f}"
        )

    print("Confusion matrix (rows=true, cols=pred):")
    print(metrics["confusion_matrix"])


if __name__ == "__main__":
    main()
