#!/usr/bin/env python3
"""
Sync two recorded videos using their timestamp JSON files and create
a side-by-side video with frame numbers and timestamps overlaid.

Usage:
    python3 sync_videos.py p1/ p2/ [--output synced.mp4] [--fps 30]
"""

import json
import sys
import os
import subprocess
import argparse
from pathlib import Path


def find_files(folder):
    """Find video and timestamp JSON in a folder."""
    folder = Path(folder)
    video = None
    timestamps = None
    for f in folder.iterdir():
        if f.suffix in ('.mp4', '.webm', '.mkv') and not f.name.startswith('.'):
            video = f
        if f.suffix == '.json' and 'timestamps' in f.name:
            timestamps = f
    if not video:
        raise FileNotFoundError(f"No video file found in {folder}")
    if not timestamps:
        raise FileNotFoundError(f"No timestamps JSON found in {folder}")
    return video, timestamps


def load_timestamps(json_path):
    """Load and parse timestamps JSON."""
    with open(json_path) as f:
        return json.load(f)


def compute_sync(p1_data, p2_data):
    """
    Compute sync parameters using epoch timestamps.

    Both phones applied clockOffset to their epochs, but in opposite directions.
    To normalize to a common reference (P1's raw clock):
      P1 common = epoch - clockOffset_p1
      P2 common = epoch_p2  (since P2's offset is -clockOffset_p1, it cancels out)
    """
    offset = p1_data['clockOffset']

    # Convert all frame epochs to common timeline
    p1_frames = [(f['frame'], f['epoch'] - offset) for f in p1_data['frames']]
    p2_frames = [(f['frame'], f['epoch']) for f in p2_data['frames']]

    if not p1_frames or not p2_frames:
        raise ValueError("No frame data found")

    p1_start_common = p1_frames[0][1]
    p1_end_common = p1_frames[-1][1]
    p2_start_common = p2_frames[0][1]
    p2_end_common = p2_frames[-1][1]

    # Overlap region
    overlap_start = max(p1_start_common, p2_start_common)
    overlap_end = min(p1_end_common, p2_end_common)
    overlap_duration = (overlap_end - overlap_start) / 1000.0

    if overlap_duration <= 0:
        raise ValueError(f"No overlap between recordings! "
                         f"P1: {p1_start_common:.0f}-{p1_end_common:.0f}, "
                         f"P2: {p2_start_common:.0f}-{p2_end_common:.0f}")

    # Video internal time starts at recordStartEpoch
    p1_record_start_common = p1_data['recordStartEpoch'] - offset
    p2_record_start_common = p2_data['recordStartEpoch']

    # Seek positions: how far into each video the overlap begins
    p1_seek = (overlap_start - p1_record_start_common) / 1000.0
    p2_seek = (overlap_start - p2_record_start_common) / 1000.0

    # Ensure non-negative
    p1_seek = max(0, p1_seek)
    p2_seek = max(0, p2_seek)

    # Frame matching: for each output frame, find nearest frame in each video
    # Build frame lookup by common epoch
    p1_by_epoch = sorted(p1_frames, key=lambda x: x[1])
    p2_by_epoch = sorted(p2_frames, key=lambda x: x[1])

    return {
        'overlap_start': overlap_start,
        'overlap_end': overlap_end,
        'overlap_duration': overlap_duration,
        'p1_seek': p1_seek,
        'p2_seek': p2_seek,
        'p1_record_start_common': p1_record_start_common,
        'p2_record_start_common': p2_record_start_common,
        'p1_frames': p1_by_epoch,
        'p2_frames': p2_by_epoch,
        'clock_offset': offset,
    }


def build_ffmpeg_command(p1_video, p2_video, sync, output_path, target_fps=30):
    """Build ffmpeg command for synced side-by-side video with overlays."""

    # Calculate relative timestamp start for drawtext
    # We'll show time relative to overlap start (0.00s, 0.03s, etc.)
    cmd = [
        'ffmpeg', '-y',
        # Input 1: P1 video, seeked to overlap start
        '-ss', f'{sync["p1_seek"]:.3f}',
        '-i', str(p1_video),
        # Input 2: P2 video, seeked to overlap start
        '-ss', f'{sync["p2_seek"]:.3f}',
        '-i', str(p2_video),
        # Filter complex
        '-filter_complex',
        # Scale both to same height, add padding, overlay text, stack horizontally
        f"""
        [0:v]scale=640:480:force_original_aspect_ratio=decrease,
             pad=640:480:(ow-iw)/2:(oh-ih)/2:black,
             fps={target_fps},
             drawtext=text='P1':fontsize=28:fontcolor=white:x=10:y=10:
                      borderw=2:bordercolor=black,
             drawtext=text='Frame\\: %{{frame_num}}':fontsize=20:fontcolor=yellow:x=10:y=450:
                      borderw=2:bordercolor=black:start_number=0,
             drawtext=text='%{{pts\\:hms}}':fontsize=20:fontcolor=cyan:x=10:y=425:
                      borderw=2:bordercolor=black
        [v0];

        [1:v]scale=640:480:force_original_aspect_ratio=decrease,
             pad=640:480:(ow-iw)/2:(oh-ih)/2:black,
             fps={target_fps},
             drawtext=text='P2':fontsize=28:fontcolor=white:x=10:y=10:
                      borderw=2:bordercolor=black,
             drawtext=text='Frame\\: %{{frame_num}}':fontsize=20:fontcolor=yellow:x=10:y=450:
                      borderw=2:bordercolor=black:start_number=0,
             drawtext=text='%{{pts\\:hms}}':fontsize=20:fontcolor=cyan:x=10:y=425:
                      borderw=2:bordercolor=black
        [v1];

        [v0][v1]hstack=inputs=2[outv]
        """.replace('\n', '').strip(),
        '-map', '[outv]',
        # Take audio from P1 (or skip if not needed)
        '-an',
        # Duration limited to overlap
        '-t', f'{sync["overlap_duration"]:.3f}',
        # Output settings
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '23',
        '-pix_fmt', 'yuv420p',
        str(output_path),
    ]
    return cmd


def main():
    parser = argparse.ArgumentParser(description='Sync two recorded videos side-by-side')
    parser.add_argument('p1_folder', help='Folder containing P1 video + timestamps JSON')
    parser.add_argument('p2_folder', help='Folder containing P2 video + timestamps JSON')
    parser.add_argument('--output', '-o', default='synced_output.mp4', help='Output filename')
    parser.add_argument('--fps', type=int, default=30, help='Output FPS (default: 30)')
    args = parser.parse_args()

    print("=== Shuttle Recorder - Video Sync ===\n")

    # Find files
    print("Finding files...")
    p1_video, p1_json = find_files(args.p1_folder)
    p2_video, p2_json = find_files(args.p2_folder)
    print(f"  P1 video: {p1_video.name}")
    print(f"  P1 timestamps: {p1_json.name}")
    print(f"  P2 video: {p2_video.name}")
    print(f"  P2 timestamps: {p2_json.name}")

    # Load timestamps
    print("\nLoading timestamps...")
    p1_data = load_timestamps(p1_json)
    p2_data = load_timestamps(p2_json)
    print(f"  P1: {p1_data['totalFrames']} frames, ~{p1_data['fps']} fps")
    print(f"  P2: {p2_data['totalFrames']} frames, ~{p2_data['fps']} fps")
    print(f"  Clock offset: {p1_data['clockOffset']:.1f} ms")

    # Compute sync
    print("\nComputing sync...")
    sync = compute_sync(p1_data, p2_data)
    print(f"  Overlap duration: {sync['overlap_duration']:.1f}s")
    print(f"  P1 seek: {sync['p1_seek']:.3f}s")
    print(f"  P2 seek: {sync['p2_seek']:.3f}s")

    # Generate frame alignment report
    print("\nFrame alignment (first 10 output frames):")
    print(f"  {'Out#':<6} {'P1 Frame':<10} {'P2 Frame':<10} {'Epoch Diff (ms)':<16}")
    print(f"  {'----':<6} {'--------':<10} {'--------':<10} {'---------------':<16}")

    target_fps = args.fps
    frame_interval = 1000.0 / target_fps  # ms per frame

    p1_epochs = [e for _, e in sync['p1_frames']]
    p2_epochs = [e for _, e in sync['p2_frames']]

    import bisect
    for i in range(min(10, int(sync['overlap_duration'] * target_fps))):
        t = sync['overlap_start'] + i * frame_interval

        # Find nearest P1 frame
        idx1 = bisect.bisect_left(p1_epochs, t)
        if idx1 >= len(p1_epochs):
            idx1 = len(p1_epochs) - 1
        elif idx1 > 0 and abs(p1_epochs[idx1 - 1] - t) < abs(p1_epochs[idx1] - t):
            idx1 -= 1
        p1_frame = sync['p1_frames'][idx1][0]
        p1_diff = p1_epochs[idx1] - t

        # Find nearest P2 frame
        idx2 = bisect.bisect_left(p2_epochs, t)
        if idx2 >= len(p2_epochs):
            idx2 = len(p2_epochs) - 1
        elif idx2 > 0 and abs(p2_epochs[idx2 - 1] - t) < abs(p2_epochs[idx2] - t):
            idx2 -= 1
        p2_frame = sync['p2_frames'][idx2][0]
        p2_diff = p2_epochs[idx2] - t

        epoch_diff = abs(p1_diff - p2_diff)
        print(f"  {i:<6} {p1_frame:<10} {p2_frame:<10} {epoch_diff:<16.1f}")

    # Build and run ffmpeg
    output_path = Path(args.p1_folder).parent / args.output
    print(f"\nGenerating synced video: {output_path}")
    print(f"  Resolution: 1280x480 (640x480 per side)")
    print(f"  FPS: {target_fps}")

    cmd = build_ffmpeg_command(p1_video, p2_video, sync, output_path, target_fps)

    print("\nRunning ffmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg error:\n{result.stderr[-1000:]}")
        sys.exit(1)

    file_size = output_path.stat().st_size / (1024 * 1024)
    print(f"\nDone! Output: {output_path} ({file_size:.1f} MB)")


if __name__ == '__main__':
    main()
