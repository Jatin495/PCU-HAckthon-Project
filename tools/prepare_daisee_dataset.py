"""Prepare DAiSEE videos for training.

This script:
1. Scans dataset root for video files.
2. Infers engagement label from folder names (very_low, low, high, very_high or 0-3).
3. Extracts sampled frames at a target FPS.
4. Writes a CSV compatible with tools/train_daisee.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import csv

import cv2


VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}
LABEL_ALIASES = {
    "very_low": "very_low",
    "verylow": "very_low",
    "0": "very_low",
    "low": "low",
    "1": "low",
    "high": "high",
    "2": "high",
    "very_high": "very_high",
    "veryhigh": "very_high",
    "3": "very_high",
}


def parse_args():
    p = argparse.ArgumentParser(description="Extract DAiSEE frames and build training CSV")
    p.add_argument("--dataset-root", required=True, help="Root folder that contains DAiSEE videos")
    p.add_argument("--output-root", default="media/daisee_frames", help="Where extracted frames are written")
    p.add_argument("--csv-out", default="media/daisee_labels.csv", help="Output CSV path")
    p.add_argument(
        "--labels-csv",
        default="",
        help="Optional label CSV or folder. If omitted, auto-detects under <dataset-root>/Labels",
    )
    p.add_argument(
        "--label-column",
        default="Engagement",
        help="Column name to use as class label from label CSV files",
    )
    p.add_argument("--sample-fps", type=float, default=3.0, help="Frames per second to sample")
    p.add_argument("--min-frame-size", type=int, default=64, help="Skip tiny frames below this size")
    p.add_argument("--max-videos", type=int, default=0, help="Optional cap for quick dry runs (0 = no cap)")
    return p.parse_args()


def load_label_map(dataset_root: Path, labels_csv_arg: str, label_column: str):
    csv_files = []

    if labels_csv_arg:
        label_path = Path(labels_csv_arg)
        if label_path.is_dir():
            csv_files.extend(sorted(label_path.glob("*.csv")))
        elif label_path.is_file():
            csv_files.append(label_path)
    else:
        default_labels_dir = dataset_root / "Labels"
        if default_labels_dir.exists() and default_labels_dir.is_dir():
            csv_files.extend(sorted(default_labels_dir.glob("*.csv")))

    label_map = {}
    for csv_path in csv_files:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                continue

            # Handle occasional trailing spaces in DAiSEE headers.
            normalized_fields = {name.strip(): name for name in reader.fieldnames}
            clip_col = normalized_fields.get("ClipID")
            value_col = normalized_fields.get(label_column.strip())
            if clip_col is None or value_col is None:
                continue

            for row in reader:
                clip_id = str(row.get(clip_col, "")).strip()
                label_value = str(row.get(value_col, "")).strip()
                if clip_id and label_value:
                    label_map[clip_id] = label_value

    return label_map


def infer_label_from_path(video_path: Path) -> str | None:
    for part in video_path.parts:
        key = part.strip().lower().replace("-", "_").replace(" ", "_")
        if key in LABEL_ALIASES:
            return LABEL_ALIASES[key]

    stem_tokens = video_path.stem.lower().replace("-", "_").split("_")
    for token in stem_tokens:
        if token in LABEL_ALIASES:
            return LABEL_ALIASES[token]

    return None


def iter_video_files(dataset_root: Path):
    for p in dataset_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
            yield p


def safe_name(value: str) -> str:
    allowed = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_"):
            allowed.append(ch)
        else:
            allowed.append("_")
    return "".join(allowed).strip("_") or "video"


def extract_video_frames(video_path: Path, output_dir: Path, sample_fps: float, min_frame_size: int):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []

    source_fps = cap.get(cv2.CAP_PROP_FPS)
    if source_fps is None or source_fps <= 0:
        source_fps = 25.0

    frame_interval = max(1, int(round(source_fps / max(0.1, sample_fps))))
    frame_idx = 0
    saved_idx = 0
    rows = []

    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break

        if frame_idx % frame_interval == 0:
            h, w = frame.shape[:2]
            if h >= min_frame_size and w >= min_frame_size:
                out_name = f"frame_{saved_idx:06d}.jpg"
                out_path = output_dir / out_name
                cv2.imwrite(str(out_path), frame)
                rows.append((out_path, frame_idx, frame_idx / source_fps))
                saved_idx += 1

        frame_idx += 1

    cap.release()
    return rows


def main():
    args = parse_args()

    dataset_root = Path(args.dataset_root).resolve()
    output_root = Path(args.output_root).resolve()
    csv_out = Path(args.csv_out).resolve()

    if not dataset_root.exists():
        raise FileNotFoundError(f"Dataset root does not exist: {dataset_root}")

    output_root.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)

    videos = list(iter_video_files(dataset_root))
    if args.max_videos > 0:
        videos = videos[: args.max_videos]

    if not videos:
        raise ValueError(f"No videos found under: {dataset_root}")

    label_map = load_label_map(dataset_root, args.labels_csv, args.label_column)
    if label_map:
        print(f"Loaded labels: {len(label_map)} clip mappings")
    else:
        print("No label CSV mappings loaded. Falling back to path-based label inference.")

    total_saved = 0
    processed_videos = 0
    skipped_unlabeled = 0

    with csv_out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "frame_path",
                "engagement",
                "source_video",
                "frame_index",
                "timestamp_sec",
            ],
        )
        writer.writeheader()

        for video_path in videos:
            clip_id = video_path.name
            label = label_map.get(clip_id)
            if label is None:
                label = infer_label_from_path(video_path)
            if label is None:
                skipped_unlabeled += 1
                continue

            rel_video = video_path.relative_to(dataset_root)
            video_tag = safe_name(str(rel_video.with_suffix("")))
            target_dir = output_root / label / video_tag
            target_dir.mkdir(parents=True, exist_ok=True)

            rows = extract_video_frames(
                video_path=video_path,
                output_dir=target_dir,
                sample_fps=args.sample_fps,
                min_frame_size=args.min_frame_size,
            )

            for out_path, frame_index, ts in rows:
                writer.writerow(
                    {
                        "frame_path": str(out_path),
                        "engagement": label,
                        "source_video": str(rel_video),
                        "frame_index": int(frame_index),
                        "timestamp_sec": round(float(ts), 3),
                    }
                )

            processed_videos += 1
            total_saved += len(rows)

            if processed_videos % 25 == 0:
                print(f"Processed videos: {processed_videos}, saved frames: {total_saved}")

    print("Preparation complete")
    print(f"Dataset root: {dataset_root}")
    print(f"Output frames: {output_root}")
    print(f"CSV path: {csv_out}")
    print(f"Videos processed: {processed_videos}")
    print(f"Videos skipped (unlabeled): {skipped_unlabeled}")
    print(f"Total frames saved: {total_saved}")


if __name__ == "__main__":
    main()
