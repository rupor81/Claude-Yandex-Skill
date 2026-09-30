"""Draw the two extension icons. Run with any Python that has Pillow:

    python3 extensions/make_icons.py

Our own mark -- a white "Я" on Yandex red -- not Yandex's logo, which is a
trademark. The calendar adds a white binding strip so the two are told apart.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

RED = (252, 63, 29)
WHITE = (255, 255, 255)
SIZE = 512
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
HERE = Path(__file__).parent


def icon(calendar: bool) -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=112, fill=RED)
    top = 0
    if calendar:
        d.rectangle((0, 96, SIZE, 150), fill=WHITE)
        for x in (150, SIZE - 150):
            d.rounded_rectangle((x - 18, 60, x + 18, 180), radius=18, fill=WHITE, outline=RED, width=10)
        top = 60
    font = ImageFont.truetype(FONT, 300 if calendar else 360)
    box = d.textbbox((0, 0), "Я", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((SIZE - w) / 2 - box[0], (SIZE - h) / 2 - box[1] + top), "Я", font=font, fill=WHITE)
    return img


for name, cal in (("yandex-calendar", True), ("yandex-mail", False)):
    icon(cal).save(HERE / name / "icon.png")
    print("wrote", HERE / name / "icon.png")
