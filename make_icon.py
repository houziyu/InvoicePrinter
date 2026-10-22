from PIL import Image, ImageDraw

sizes = [16, 24, 32, 48, 64, 128, 256]
images = []
for size in sizes:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pad = size * 0.12
    d.rounded_rectangle((pad, size * 0.25, size - pad, size * 0.78), radius=size * 0.10, fill="#ef4444")
    d.rounded_rectangle((size * 0.25, size * 0.08, size * 0.75, size * 0.42), radius=size * 0.04, fill="#ffffff", outline="#ef4444", width=max(1, size // 32))
    d.rounded_rectangle((size * 0.23, size * 0.62, size * 0.77, size * 0.94), radius=size * 0.03, fill="#ffffff", outline="#ef4444", width=max(1, size // 32))
    for y in (0.72, 0.81):
        d.line((size * 0.32, size * y, size * 0.68, size * y), fill="#ef4444", width=max(1, size // 28))
    d.ellipse((size * 0.73, size * 0.34, size * 0.80, size * 0.41), fill="#ffffff")
    images.append(im)
images[-1].save("app.ico", format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])
