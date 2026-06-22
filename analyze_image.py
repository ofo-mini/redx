#!/usr/bin/env python3
import sys
from ocr_client import OcrClient

def main():
    client = OcrClient()
    image_path = "/Users/one/Documents/home/redx/screenshots/006_success.png"
    print(f"Running OCR on {image_path}...")
    items = client.recognize_file(image_path)
    for i, item in enumerate(items):
        print(f"[{i:02d}] Text: {repr(item.text)}, Conf: {item.conf:.3f}, Center: {item.center}, Coords: {item.coords}")

if __name__ == "__main__":
    main()
