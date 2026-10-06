"""Compression et conversion d'images vers WebP, indépendante de l'interface."""

from __future__ import annotations

import io
import logging
import math
import os
import re
import zipfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import pillow_heif
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)

MAX_FILES = 30
ZIP_THRESHOLD = 5
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
# Au-delà, une image décodée pèse plusieurs centaines de Mo en mémoire. Pour une
# animation, toutes les frames sont gardées en mémoire : la limite porte sur leur total.
MAX_PIXELS = 100_000_000

# Le format est déterminé par le contenu du fichier, jamais par son extension.
# "JPEG" couvre aussi les MPO (JPEG multi-images), ouverts par le même décodeur.
ACCEPTED_FORMATS = ("JPEG", "PNG", "GIF", "BMP", "DIB", "TIFF", "WEBP", "AVIF", "HEIF", "ICO", "TGA", "JPEG2000")
ACCEPTED_EXTENSIONS = (
    "jpg",
    "jpeg",
    "jfif",
    "png",
    "gif",
    "bmp",
    "dib",
    "tif",
    "tiff",
    "webp",
    "avif",
    "heic",
    "heif",
    "ico",
    "tga",
    "jp2",
    "j2k",
)
# MPO (photos de smartphone) et TIFF multipage ont plusieurs « frames » qui ne
# sont pas une animation : seule la première image doit être conservée.
ANIMATED_FORMATS = frozenset({"GIF", "PNG", "WEBP", "AVIF"})

# Sources sans perte : leurs aplats (logos, captures d'écran) se compressent souvent
# mieux en WebP sans perte. Jamais pour une source déjà compressée avec perte (JPEG,
# HEIC…) : le sans-perte y figerait bruit et artefacts, pour un poids multiplié.
LOSSLESS_SOURCE_FORMATS = frozenset({"PNG", "GIF", "BMP", "DIB", "TIFF", "ICO", "TGA"})

DEFAULT_MAX_DIMENSION = 1920
DEFAULT_QUALITY = 80
# Mesuré (SSIM) sur des photos réelles : sous 70, la perte de détail devient nette.
# Ce plancher vaut pour le réglage comme pour la réduction automatique.
MIN_QUALITY = 70
# Au-delà, le fichier pèse 2 à 3 fois plus pour un gain invisible à l'écran.
MAX_QUALITY = 95
# Pas de la réduction automatique quand la WebP dépasse le poids d'origine.
QUALITY_STEP = 5
# 6 = compression la plus poussée de libwebp ; plus lent mais fichiers plus petits.
WEBP_METHOD = 6
# En mode sans perte, libwebp interprète « quality » comme l'effort de compression.
LOSSLESS_EFFORT = 80
# Facteur entre une valeur 16 bits (0–65535) et 8 bits (0–255).
SIXTEEN_TO_EIGHT_BIT = 257
UNEXPECTED_ERROR_MESSAGE = "Erreur inattendue pendant la conversion."
# Borné : chaque conversion en cours garde une image décodée en mémoire.
CONVERSION_WORKERS = min(4, os.cpu_count() or 1)


class ImageConversionError(Exception):
    """Erreur liée au fichier fourni, présentable telle quelle à l'utilisateur."""


@dataclass(frozen=True)
class ConversionSettings:
    quality: int = DEFAULT_QUALITY
    # Plus grand côté en pixels ; None conserve la taille d'origine.
    max_dimension: int | None = DEFAULT_MAX_DIMENSION

    def __post_init__(self) -> None:
        if not MIN_QUALITY <= self.quality <= MAX_QUALITY:
            raise ValueError(f"La qualité doit être comprise entre {MIN_QUALITY} et {MAX_QUALITY}.")
        if self.max_dimension is not None and self.max_dimension < 1:
            raise ValueError("La dimension maximale doit être positive.")


@dataclass(frozen=True)
class ConvertedImage:
    data: bytes
    width: int
    height: int
    animated: bool
    # Qualité réellement utilisée, qui peut être inférieure à celle demandée ;
    # None si l'image est encodée sans perte ou si le WebP d'origine est conservé.
    quality: int | None
    kept_original: bool = False


@dataclass(frozen=True)
class ConversionResult:
    source_name: str
    output_name: str
    original_size: int
    image: ConvertedImage

    @property
    def output_size(self) -> int:
        return len(self.image.data)

    @property
    def saved_ratio(self) -> float:
        """Part du poids d'origine économisée (négative si le fichier grossit)."""
        if self.original_size == 0:
            return 0.0
        return 1 - self.output_size / self.original_size


@dataclass(frozen=True)
class ConversionFailure:
    source_name: str
    reason: str


@dataclass(frozen=True)
class BatchResult:
    results: list[ConversionResult]
    failures: list[ConversionFailure]


def convert_image(data: bytes, settings: ConversionSettings) -> ConvertedImage:
    """Convertit le contenu d'une image en WebP optimisé pour le web."""
    if len(data) > MAX_FILE_SIZE_BYTES:
        raise ImageConversionError(f"Fichier trop lourd (maximum {MAX_FILE_SIZE_MB} Mo).")

    try:
        with Image.open(io.BytesIO(data), formats=ACCEPTED_FORMATS) as source:
            animated = source.format in ANIMATED_FORMATS and getattr(source, "n_frames", 1) > 1
            frame_count = source.n_frames if animated else 1
            if source.width * source.height * frame_count > MAX_PIXELS:
                raise ImageConversionError(
                    f"Image ou animation trop grande (plus de {MAX_PIXELS // 1_000_000} mégapixels au total)."
                )
            prepare = _prepare_animation if animated else _prepare_still
            prepared = prepare(source, settings.max_dimension)
            encoded, quality = _lightest_encoding(
                prepared,
                requested_quality=settings.quality,
                original_size=len(data),
                lossless_allowed=source.format in LOSSLESS_SOURCE_FORMATS,
            )
            kept_original = _should_keep_original_webp(source, data, encoded, prepared.frame.size)
    except UnidentifiedImageError as error:
        raise ImageConversionError("Format non reconnu ou fichier qui n'est pas une image.") from error
    except Image.DecompressionBombError as error:
        raise ImageConversionError("Image trop grande pour être traitée.") from error
    except OSError as error:
        raise ImageConversionError("Fichier image corrompu ou incomplet.") from error
    except ValueError as error:
        # Pillow lève ValueError pour les modes de couleur qu'il ne sait pas convertir (LAB, HSV…).
        raise ImageConversionError("Mode de couleur non pris en charge.") from error

    width, height = prepared.frame.size
    if kept_original:
        return ConvertedImage(data, width, height, animated, quality=None, kept_original=True)
    return ConvertedImage(encoded, width, height, animated, quality=quality)


def convert_batch(
    files: Sequence[tuple[str, bytes]],
    settings: ConversionSettings,
    on_progress: Callable[[int, str], None] | None = None,
) -> BatchResult:
    """Convertit une série de fichiers ; un fichier invalide n'interrompt pas les autres.

    `on_progress(nombre_terminés, nom)` est appelé, depuis le thread appelant, à la fin
    de chaque fichier. Les résultats gardent l'ordre des fichiers fournis.
    """
    if len(files) > MAX_FILES:
        raise ValueError(f"{MAX_FILES} fichiers maximum par conversion.")

    output_names = unique_names([webp_name(name) for name, _ in files])
    outcomes: list[ConversionResult | ConversionFailure | None] = [None] * len(files)

    # Pillow libère le GIL pendant le décodage et l'encodage : les threads
    # parallélisent réellement le travail.
    with ThreadPoolExecutor(max_workers=CONVERSION_WORKERS) as executor:
        futures = {
            executor.submit(_convert_one, source_name, data, output_name, settings): index
            for index, ((source_name, data), output_name) in enumerate(zip(files, output_names, strict=True))
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            outcomes[index] = future.result()
            if on_progress is not None:
                on_progress(completed, files[index][0])

    return BatchResult(
        results=[outcome for outcome in outcomes if isinstance(outcome, ConversionResult)],
        failures=[outcome for outcome in outcomes if isinstance(outcome, ConversionFailure)],
    )


def _convert_one(
    source_name: str, data: bytes, output_name: str, settings: ConversionSettings
) -> ConversionResult | ConversionFailure:
    try:
        image = convert_image(data, settings)
    except ImageConversionError as error:
        return ConversionFailure(source_name=source_name, reason=str(error))
    except Exception:
        # Un bug sur un fichier ne doit pas faire perdre les autres conversions du lot.
        logger.exception("Échec inattendu de conversion pour %r", source_name)
        return ConversionFailure(source_name=source_name, reason=UNEXPECTED_ERROR_MESSAGE)
    return ConversionResult(
        source_name=source_name,
        output_name=output_name,
        original_size=len(data),
        image=image,
    )


def build_zip(results: Sequence[ConversionResult]) -> bytes:
    """Regroupe les images converties dans une archive ZIP."""
    buffer = io.BytesIO()
    # WebP est déjà compressé : le dégonflage ZIP coûterait du temps sans rien gagner.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for result in results:
            archive.writestr(result.output_name, result.image.data)
    return buffer.getvalue()


def webp_name(original_name: str) -> str:
    """Nom de sortie sûr : sans chemin, sans caractère de contrôle, extension .webp."""
    base_name = PurePosixPath(original_name.replace("\\", "/")).name
    stem = base_name.rsplit(".", 1)[0] if "." in base_name else base_name
    stem = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "", stem).strip(" .")
    return f"{stem or 'image'}.webp"


def unique_names(names: Sequence[str]) -> list[str]:
    """Suffixe les doublons (`photo.webp`, `photo-2.webp`…) pour éviter les écrasements dans le ZIP."""
    taken: set[str] = set()
    unique: list[str] = []
    for name in names:
        stem, extension = name.rsplit(".", 1)
        candidate = name
        counter = 2
        while candidate.lower() in taken:
            candidate = f"{stem}-{counter}.{extension}"
            counter += 1
        taken.add(candidate.lower())
        unique.append(candidate)
    return unique


@dataclass(frozen=True)
class _PreparedImage:
    """Image tournée, redimensionnée et en RGB(A), prête à être encodée plusieurs fois."""

    frame: Image.Image
    # Options d'enregistrement propres à l'image : profil ICC, frames suivantes, durées…
    save_options: dict[str, object] = field(default_factory=dict)

    def encode(self, quality: int | None) -> bytes:
        """Encode en WebP ; `quality=None` encode sans perte."""
        buffer = io.BytesIO()
        # Sans `exif` ni `xmp`, les métadonnées (GPS, appareil…) ne sont pas recopiées.
        self.frame.save(
            buffer,
            format="WEBP",
            method=WEBP_METHOD,
            lossless=quality is None,
            quality=LOSSLESS_EFFORT if quality is None else quality,
            **self.save_options,
        )
        return buffer.getvalue()


def _lightest_encoding(
    prepared: _PreparedImage, requested_quality: int, original_size: int, lossless_allowed: bool
) -> tuple[bytes, int | None]:
    """Encode à la qualité demandée ; si le fichier dépasse le poids d'origine, essaie
    des variantes plus légères, de la plus fidèle à la moins fidèle, et s'arrête à la
    première qui ne dépasse plus. Sinon garde la plus légère, jamais sous MIN_QUALITY.
    Retourne les octets et la qualité utilisée (None : sans perte).
    """
    best_data, best_quality = prepared.encode(requested_quality), requested_quality
    fallbacks: list[int | None] = [None] if lossless_allowed else []
    fallbacks += _lower_qualities(requested_quality)

    for quality in fallbacks:
        if len(best_data) <= original_size:
            break
        candidate = prepared.encode(quality)
        if len(candidate) < len(best_data):
            best_data, best_quality = candidate, quality
    return best_data, best_quality


def _lower_qualities(requested_quality: int) -> list[int]:
    """Qualités inférieures à essayer, par pas de QUALITY_STEP, jusqu'à MIN_QUALITY inclus.

    80 → [75, 70] ; 72 → [70] ; 70 → [].
    """
    qualities = list(range(requested_quality - QUALITY_STEP, MIN_QUALITY, -QUALITY_STEP))
    if requested_quality > MIN_QUALITY:
        qualities.append(MIN_QUALITY)
    return qualities


def _prepare_still(source: Image.Image, max_dimension: int | None) -> _PreparedImage:
    if max_dimension is not None and source.format in ("JPEG", "MPO"):
        # Le décodeur JPEG sait réduire l'image pendant le décodage (1/2, 1/4, 1/8) :
        # plus rapide et bien moins gourmand en mémoire. draft() ne réduit que si les
        # deux côtés restent ≥ à la cible, d'où une cible aux proportions de l'image.
        # Le redimensionnement exact reste fait par _resize.
        scale = max_dimension / max(source.size)
        if scale < 1:
            target = (math.ceil(source.width * scale), math.ceil(source.height * scale))
            source.draft(source.mode, target)
    # Applique la rotation EXIF des photos avant de supprimer les métadonnées,
    # sinon les photos prises en portrait sortiraient couchées. Sur place pour ne pas
    # dupliquer en mémoire une image potentiellement énorme.
    ImageOps.exif_transpose(source, in_place=True)
    icc_profile = _rgb_icc_profile(source)
    frame = _resize(_to_webp_mode(source), max_dimension)
    return _PreparedImage(frame, {"icc_profile": icc_profile})


def _prepare_animation(source: Image.Image, max_dimension: int | None) -> _PreparedImage:
    # Lu avant de parcourir les frames : `info` change à chaque déplacement.
    loop = source.info.get("loop", 0)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(source):
        durations.append(frame.info.get("duration", source.info.get("duration", 100)))
        frames.append(_resize(_to_webp_mode(frame.copy()), max_dimension))

    return _PreparedImage(
        frames[0],
        {
            "save_all": True,
            "append_images": frames[1:],
            "duration": durations,
            "loop": loop,
            # Laisse l'encodeur choisir lossy ou sans-perte pour chaque frame : les GIF
            # à aplats grossissent nettement sinon.
            "allow_mixed": True,
            "minimize_size": True,
        },
    )


def _should_keep_original_webp(source: Image.Image, data: bytes, encoded: bytes, size: tuple[int, int]) -> bool:
    """Un WebP déjà optimisé peut grossir en étant ré-encodé : on garde alors l'original.

    Seulement s'il n'a pas été redimensionné et ne contient pas de métadonnées,
    pour que la promesse « métadonnées supprimées » tienne.
    """
    return (
        source.format == "WEBP"
        and len(encoded) >= len(data)
        and size == source.size
        and "exif" not in source.info
        and "xmp" not in source.info
    )


def _to_webp_mode(frame: Image.Image) -> Image.Image:
    """Ramène l'image en RGB ou RGBA, seuls modes acceptés par l'encodeur WebP."""
    if frame.mode in ("RGB", "RGBA"):
        return frame
    if frame.mode.startswith("I"):
        # La conversion directe d'une image 16 bits sature tout en blanc : on la ramène
        # d'abord sur 8 bits.
        frame = frame.convert("I").point(lambda value: value / SIXTEEN_TO_EIGHT_BIT).convert("L")
    return frame.convert("RGBA" if frame.has_transparency_data else "RGB")


def _rgb_icc_profile(frame: Image.Image) -> bytes | None:
    # Un profil CMJN ou niveaux de gris décrirait mal les pixels une fois passés en RGB.
    if frame.mode not in ("RGB", "RGBA"):
        return None
    return frame.info.get("icc_profile")


def _resize(frame: Image.Image, max_dimension: int | None) -> Image.Image:
    # Jamais d'agrandissement : il alourdirait le fichier sans ajouter de détail.
    if max_dimension is None or max(frame.size) <= max_dimension:
        return frame
    return ImageOps.contain(frame, (max_dimension, max_dimension), Image.Resampling.LANCZOS)
