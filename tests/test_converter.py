import io
import zipfile

import pytest
from PIL import Image, ImageCms, ImageDraw

from converter import (
    MAX_FILES,
    ConversionSettings,
    ImageConversionError,
    build_zip,
    convert_batch,
    convert_image,
    unique_names,
    webp_name,
)

DEFAULT = ConversionSettings()
EXIF_ORIENTATION_TAG = 0x0112
ROTATED_90_CLOCKWISE = 6


def encode(image: Image.Image, image_format: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, image_format, **options)
    return buffer.getvalue()


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def photo(size=(800, 600)) -> Image.Image:
    # Dégradé plutôt qu'aplat : un aplat se compresse trop bien pour être représentatif.
    return Image.linear_gradient("L").resize(size).convert("RGB")


@pytest.mark.parametrize(
    ("image_format", "mode"),
    [
        ("JPEG", "RGB"),
        ("PNG", "RGB"),
        ("BMP", "RGB"),
        ("TIFF", "RGB"),
        ("GIF", "P"),
        ("AVIF", "RGB"),
        ("HEIF", "RGB"),
        ("ICO", "RGBA"),
        ("TGA", "RGB"),
        ("JPEG2000", "RGB"),
        ("WEBP", "RGB"),
    ],
)
def test_converts_every_supported_format_to_webp(image_format, mode):
    source = photo((64, 64)).convert(mode)

    result = convert_image(encode(source, image_format), DEFAULT)

    assert decode(result.data).format == "WEBP"
    assert (result.width, result.height) == (64, 64)


def test_reduces_jpeg_weight():
    data = encode(photo((1600, 1200)), "JPEG", quality=95)

    result = convert_image(data, DEFAULT)

    assert len(result.data) < len(data)


def test_line_art_falls_back_to_lossless_when_lossy_grows():
    # Traits fins sur fond uni : le lossy produit un fichier bien plus lourd que le PNG.
    drawing = Image.new("RGB", (600, 200), "white")
    pen = ImageDraw.Draw(drawing)
    for x in range(0, 600, 7):
        pen.line([(x, 0), (600 - x, 200)], fill="black")
    data = encode(drawing.convert("P", palette=Image.Palette.ADAPTIVE, colors=2), "PNG", optimize=True)

    result = convert_image(data, DEFAULT)

    assert len(result.data) < len(data)
    assert decode(result.data).convert("RGB").getpixel((0, 199)) == (255, 255, 255)


def test_flat_color_animation_is_lighter_than_gif():
    frames = [Image.new("RGB", (200, 200), color) for color in ("red", "green", "blue", "yellow")]
    for frame in frames:
        ImageDraw.Draw(frame).rectangle((50, 50, 150, 150), fill="white")
    data = encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)

    result = convert_image(data, DEFAULT)

    assert result.animated
    assert len(result.data) < len(data)


def test_downscales_to_max_dimension_keeping_ratio():
    data = encode(photo((4000, 2000)), "PNG")

    result = convert_image(data, ConversionSettings(max_dimension=1920))

    assert (result.width, result.height) == (1920, 960)
    assert decode(result.data).size == (1920, 960)


def test_downscales_large_jpeg_to_exact_size():
    data = encode(photo((4032, 3024)), "JPEG")

    result = convert_image(data, ConversionSettings(max_dimension=1920))

    assert (result.width, result.height) == (1920, 1440)


def test_downscales_large_rotated_jpeg_in_portrait():
    exif = Image.Exif()
    exif[EXIF_ORIENTATION_TAG] = ROTATED_90_CLOCKWISE
    data = encode(photo((4032, 3024)), "JPEG", exif=exif)

    result = convert_image(data, ConversionSettings(max_dimension=1920))

    assert (result.width, result.height) == (1440, 1920)


def test_never_upscales_small_images():
    result = convert_image(encode(photo((300, 200)), "PNG"), ConversionSettings(max_dimension=1920))

    assert (result.width, result.height) == (300, 200)


def test_original_dimension_kept_when_no_max():
    result = convert_image(encode(photo((3000, 1000)), "PNG"), ConversionSettings(max_dimension=None))

    assert (result.width, result.height) == (3000, 1000)


def test_keeps_png_transparency():
    source = Image.new("RGBA", (50, 50), (255, 0, 0, 0))
    source.paste((0, 0, 255, 255), (10, 10, 40, 40))

    output = decode(convert_image(encode(source, "PNG"), DEFAULT).data)

    assert output.mode == "RGBA"
    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((25, 25))[3] == 255


def test_keeps_palette_transparency():
    source = Image.new("P", (20, 20), 0)
    source.putpalette([255, 255, 255, 0, 0, 0] + [0] * 762)
    source.paste(1, (5, 5, 15, 15))

    output = decode(convert_image(encode(source, "GIF", transparency=0), DEFAULT).data)

    assert output.mode == "RGBA"
    assert output.getpixel((0, 0))[3] == 0


def test_animated_gif_stays_animated():
    frames = [Image.new("RGB", (40, 40), color) for color in ("red", "green", "blue")]
    data = encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=120, loop=0)

    result = convert_image(data, DEFAULT)

    output = Image.open(io.BytesIO(result.data))
    assert result.animated
    assert output.format == "WEBP"
    assert output.n_frames == 3


def test_multi_picture_jpeg_is_not_treated_as_animation():
    first, second = photo((80, 60)), photo((40, 30))
    data = encode(first, "MPO", save_all=True, append_images=[second])
    assert Image.open(io.BytesIO(data)).format == "MPO"

    result = convert_image(data, DEFAULT)

    assert not result.animated
    assert getattr(decode(result.data), "n_frames", 1) == 1


def test_applies_exif_rotation_then_strips_metadata():
    exif = Image.Exif()
    exif[EXIF_ORIENTATION_TAG] = ROTATED_90_CLOCKWISE
    exif[0x8825] = {2: (48.0, 51.0, 24.0)}  # GPSInfo : ne doit jamais ressortir
    data = encode(photo((200, 100)), "JPEG", exif=exif)

    output = decode(convert_image(data, DEFAULT).data)

    assert output.size == (100, 200)
    assert "exif" not in output.info


def test_keeps_rgb_color_profile():
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    data = encode(photo((50, 50)), "JPEG", icc_profile=profile)

    output = decode(convert_image(data, DEFAULT).data)

    assert output.info.get("icc_profile") == profile


def test_cmyk_converted_to_rgb_without_cmyk_profile():
    data = encode(Image.new("CMYK", (30, 30), (0, 255, 255, 0)), "JPEG")

    output = decode(convert_image(data, DEFAULT).data)

    assert output.mode == "RGB"
    assert "icc_profile" not in output.info
    red, green, blue = output.getpixel((15, 15))
    assert red > 200 and green < 60 and blue < 60


def test_sixteen_bit_grayscale_is_not_saturated():
    source = Image.new("I;16", (10, 1))
    source.putdata([value * 6553 for value in range(10)])

    output = decode(convert_image(encode(source, "PNG"), DEFAULT).data).convert("L")

    assert output.getpixel((0, 0)) < 10
    assert 100 < output.getpixel((5, 0)) < 150
    assert output.getpixel((9, 0)) > 220


def test_lossless_mode():
    source = Image.new("RGB", (20, 20), (12, 34, 56))

    output = decode(convert_image(encode(source, "PNG"), ConversionSettings(lossless=True)).data)

    assert output.getpixel((5, 5)) == (12, 34, 56)


def test_keeps_original_webp_when_reencoding_grows_it():
    data = encode(photo((300, 300)), "WEBP", quality=10)

    result = convert_image(data, ConversionSettings(quality=100))

    assert result.data == data


def test_rejects_non_image_content_even_with_image_extension():
    with pytest.raises(ImageConversionError, match="Format non reconnu"):
        convert_image(b"<?php echo 'pas une image'; ?>", DEFAULT)


def test_rejects_unsupported_format():
    with pytest.raises(ImageConversionError, match="Format non reconnu"):
        convert_image(encode(photo((10, 10)), "PDF"), DEFAULT)


def test_rejects_truncated_image():
    data = encode(photo((400, 400)), "PNG")

    with pytest.raises(ImageConversionError, match="corrompu"):
        convert_image(data[: len(data) // 2], DEFAULT)


def test_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr("converter.MAX_FILE_SIZE_BYTES", 100)

    with pytest.raises(ImageConversionError, match="trop lourd"):
        convert_image(encode(photo((100, 100)), "PNG"), DEFAULT)


def test_rejects_too_many_pixels(monkeypatch):
    monkeypatch.setattr("converter.MAX_PIXELS", 99)

    with pytest.raises(ImageConversionError, match="trop grande"):
        convert_image(encode(photo((10, 10)), "PNG"), DEFAULT)


def test_rejects_animation_with_too_many_pixels_in_total(monkeypatch):
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "green", "blue")]
    data = encode(frames[0], "GIF", save_all=True, append_images=frames[1:])
    monkeypatch.setattr("converter.MAX_PIXELS", 250)

    with pytest.raises(ImageConversionError, match="trop grande"):
        convert_image(data, DEFAULT)


@pytest.mark.parametrize("quality", [0, 101])
def test_rejects_invalid_quality(quality):
    with pytest.raises(ValueError):
        ConversionSettings(quality=quality)


def test_batch_continues_after_invalid_file():
    files = [
        ("a.jpg", encode(photo((50, 50)), "JPEG")),
        ("faux.png", b"rien"),
        ("b.png", encode(photo((50, 50)), "PNG")),
    ]
    progress: list[tuple[int, str]] = []

    batch = convert_batch(files, DEFAULT, on_progress=lambda index, name: progress.append((index, name)))

    assert [result.output_name for result in batch.results] == ["a.webp", "b.webp"]
    assert [failure.source_name for failure in batch.failures] == ["faux.png"]
    assert sorted(count for count, _ in progress) == [1, 2, 3]
    assert sorted(name for _, name in progress) == ["a.jpg", "b.png", "faux.png"]


def test_batch_reports_unexpected_error_without_losing_other_files(monkeypatch):
    def broken(data, settings):
        if data == b"boom":
            raise RuntimeError("bug")
        return real_convert(data, settings)

    import converter

    real_convert = converter.convert_image
    monkeypatch.setattr(converter, "convert_image", broken)

    batch = convert_batch([("a.png", encode(photo((10, 10)), "PNG")), ("b.png", b"boom")], DEFAULT)

    assert len(batch.results) == 1
    assert batch.failures[0].reason == converter.UNEXPECTED_ERROR_MESSAGE


def test_batch_keeps_input_order():
    sizes = [(900, 900), (10, 10), (500, 500), (30, 30)]
    files = [(f"{index}.png", encode(photo(size), "PNG")) for index, size in enumerate(sizes)]

    batch = convert_batch(files, DEFAULT)

    assert [result.source_name for result in batch.results] == ["0.png", "1.png", "2.png", "3.png"]
    assert [(result.image.width, result.image.height) for result in batch.results] == sizes


def test_batch_refuses_more_than_max_files():
    files = [(f"{index}.png", b"") for index in range(MAX_FILES + 1)]

    with pytest.raises(ValueError):
        convert_batch(files, DEFAULT)


def test_batch_gives_unique_names_to_duplicates():
    data = encode(photo((10, 10)), "PNG")
    files = [("photo.png", data), ("photo.jpg", data), ("PHOTO.gif", encode(photo((10, 10)), "GIF"))]

    batch = convert_batch(files, DEFAULT)

    assert [result.output_name for result in batch.results] == ["photo.webp", "photo-2.webp", "PHOTO-3.webp"]


def test_saved_ratio():
    batch = convert_batch([("a.jpg", encode(photo((1200, 900)), "JPEG", quality=95))], DEFAULT)

    result = batch.results[0]
    assert result.saved_ratio == pytest.approx(1 - result.output_size / result.original_size)
    assert 0 < result.saved_ratio < 1


def test_zip_contains_every_converted_image():
    files = [(f"img{index}.png", encode(photo((20, 20)), "PNG")) for index in range(6)]
    batch = convert_batch(files, DEFAULT)

    with zipfile.ZipFile(io.BytesIO(build_zip(batch.results))) as archive:
        assert archive.namelist() == [f"img{index}.webp" for index in range(6)]
        assert archive.read("img3.webp") == batch.results[3].image.data


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("photo.jpg", "photo.webp"),
        ("archive.tar.png", "archive.tar.webp"),
        ("sans_extension", "sans_extension.webp"),
        ("../../etc/passwd.png", "passwd.webp"),
        ("C:\\Users\\moi\\vacances.heic", "vacances.webp"),
        ('mauvais<>:"|?*nom.png', "mauvaisnom.webp"),
        (".png", "image.webp"),
        ("", "image.webp"),
    ],
)
def test_webp_name(original, expected):
    assert webp_name(original) == expected


def test_unique_names_is_case_insensitive():
    assert unique_names(["a.webp", "A.webp", "a-2.webp"]) == ["a.webp", "A-2.webp", "a-2-2.webp"]
