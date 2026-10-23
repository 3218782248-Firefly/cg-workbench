"""Self-authored 2D fixture; exercises the pipeline without a DCC installation."""
import argparse
import math
from PIL import Image, ImageDraw

p = argparse.ArgumentParser()
p.add_argument("--frame", type=int, required=True)
p.add_argument("--output", required=True)
a = p.parse_args()
image = Image.new("RGB", (192, 192), (22, 25, 33))
draw = ImageDraw.Draw(image)
x = 96 + int(math.sin(a.frame / 12 * math.tau) * 24)
draw.ellipse((x-32, 56, x+32, 120), fill=(40, 140, 230))
draw.ellipse((x-13, 75, x-6, 84), fill="white")
draw.ellipse((x+6, 75, x+13, 84), fill="white")
draw.arc((x-10, 84, x+10, 101), 0, 180, fill="white", width=2)
image.save(a.output)
