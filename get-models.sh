#!/bin/sh
# Downloads the two MediaPipe models from Google into models/ (not shipped with Gesture Cam, so
# their license terms stay Google's: https://developers.google.com/edge/mediapipe/solutions/guide).
# Checks each file's SHA-256, so a changed or broken download is never used.
set -e
cd "$(dirname "$(readlink -f "$0")")"
mkdir -p models

base=https://storage.googleapis.com/mediapipe-models
fetch() {  # fetch <file> <url> <sha256>
    [ -f "models/$1" ] && echo "$3  models/$1" | sha256sum -c --status && return
    echo "Downloading $1..."
    curl -fL --progress-bar -o "models/$1.part" "$2"
    if ! echo "$3  models/$1.part" | sha256sum -c --status; then
        rm -f "models/$1.part"
        echo "models/$1: the download doesn't match the expected file." >&2
        exit 1
    fi
    mv "models/$1.part" "models/$1"
}

fetch gesture_recognizer.task "$base/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task" \
    97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482
fetch selfie_segmenter.tflite "$base/image_segmenter/selfie_segmenter/float16/1/selfie_segmenter.tflite" \
    191ac9529ae506ee0beefa6b2c945a172dab9d07d1e802a290a4e4038226658b
