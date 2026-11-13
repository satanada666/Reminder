"""Иконка программы (колокольчик) — рисуется кодом, файлы не нужны."""
from PIL import Image, ImageDraw


def make_icon(size: int = 256) -> Image.Image:
    """Колокольчик на скруглённом синем квадрате."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 4, fill=(79, 107, 255))
    w = (255, 255, 255)
    d.ellipse((s * .30, s * .20, s * .70, s * .60), fill=w)            # купол
    d.rectangle((s * .30, s * .40, s * .70, s * .66), fill=w)          # тело
    d.rounded_rectangle((s * .22, s * .62, s * .78, s * .70), radius=s * .04, fill=w)  # юбка
    d.ellipse((s * .44, s * .70, s * .56, s * .82), fill=w)            # язычок
    d.ellipse((s * .46, s * .13, s * .54, s * .21), fill=w)            # ушко
    return img


def save_all(out_dir):
    """icon.ico / icon.icns / icon.png для сборщика."""
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    img = make_icon(1024)
    img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.save(out / "icon.icns")
    img.resize((256, 256)).save(out / "icon.png")
    return out


if __name__ == "__main__":
    print(save_all("build_assets"))
