"""Interface Streamlit : compression et conversion d'images en WebP pour le web."""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from converter import (
    ACCEPTED_EXTENSIONS,
    DEFAULT_MAX_DIMENSION,
    DEFAULT_QUALITY,
    MAX_FILE_SIZE_MB,
    MAX_FILES,
    MAX_QUALITY,
    MIN_QUALITY,
    ZIP_THRESHOLD,
    BatchResult,
    ConversionResult,
    ConversionSettings,
    ConvertedImage,
    build_zip,
    convert_batch,
)

ZIP_FILE_NAME = "images-webp.zip"
WEBP_MIME = "image/webp"
ZIP_MIME = "application/zip"
SESSION_KEY = "conversion"
PREVIEW_COLUMNS = 5
THUMBNAIL_WIDTH = 120
# segmented_control renvoie None quand rien n'est sélectionné : la taille d'origine
# a donc besoin de sa propre valeur pour ne pas être confondue avec ce cas.
ORIGINAL_SIZE = 0
MAX_DIMENSION_CHOICES = (1280, 1920, 2560, ORIGINAL_SIZE)


@dataclass(frozen=True)
class StoredConversion:
    """Résultat conservé entre deux exécutions du script Streamlit."""

    batch: BatchResult
    input_count: int
    zip_data: bytes | None


def format_size(size_in_bytes: int) -> str:
    size = float(size_in_bytes)
    for unit in ("o", "Ko", "Mo"):
        if size < 1024 or unit == "Mo":
            text = f"{size:.0f}" if unit == "o" else f"{size:.1f}"
            return f"{text.replace('.', ',')} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def format_weight_change(saved_ratio: float) -> str:
    """« −92 % » quand le fichier s'allège, « +4 % » quand il s'alourdit."""
    percent = round(saved_ratio * 100)
    if percent == 0:
        return "0 %"
    return f"−{percent} %" if percent > 0 else f"+{-percent} %"


def dimension_label(max_dimension: int) -> str:
    return "Originale" if max_dimension == ORIGINAL_SIZE else f"{max_dimension} px"


def quality_label(image: ConvertedImage) -> str:
    if image.kept_original:
        return "original conservé"
    if image.quality is None:
        return "sans perte"
    return str(image.quality)


def render_header() -> None:
    st.title("Images prêtes pour le web")
    st.caption(
        f"Compressez et convertissez jusqu'à {MAX_FILES} images en WebP. "
        f"À partir de {ZIP_THRESHOLD} fichiers, tout est regroupé dans un ZIP. "
        "Métadonnées (GPS, appareil) supprimées, rotation des photos corrigée."
    )


def render_form() -> tuple[list, ConversionSettings] | None:
    """Affiche le formulaire ; retourne les fichiers et réglages une fois soumis."""
    with st.form("conversion_form", border=False):
        uploaded_files = st.file_uploader(
            "Images à convertir",
            type=list(ACCEPTED_EXTENSIONS),
            accept_multiple_files=True,
            max_upload_size=MAX_FILE_SIZE_MB,
            help=f"{MAX_FILES} fichiers maximum, {MAX_FILE_SIZE_MB} Mo par fichier. "
            "JPEG, PNG, GIF (animé compris), HEIC, AVIF, WebP, TIFF, BMP, ICO…",
        )

        quality = st.slider(
            "Qualité",
            min_value=MIN_QUALITY,
            max_value=MAX_QUALITY,
            value=DEFAULT_QUALITY,
            help="80 : bon compromis poids / netteté pour le web. Si une image WebP pèse plus "
            "que l'original, la qualité est réduite automatiquement, par pas de 5, sans jamais "
            f"descendre sous {MIN_QUALITY}.",
        )
        max_dimension = st.segmented_control(
            "Plus grand côté",
            options=MAX_DIMENSION_CHOICES,
            default=DEFAULT_MAX_DIMENSION,
            format_func=dimension_label,
            required=True,
            help="Réduit les images trop grandes, sans jamais agrandir les petites. "
            "1920 px convient à la plupart des sites ; « Originale » garde la pleine "
            "définition et donne des fichiers bien plus lourds.",
        )

        submitted = st.form_submit_button("Convertir en WebP", type="primary", icon=":material/bolt:")

    if not submitted:
        return None
    return uploaded_files or [], ConversionSettings(
        quality=quality,
        max_dimension=None if max_dimension == ORIGINAL_SIZE else max_dimension,
    )


def run_conversion(uploaded_files: list, settings: ConversionSettings) -> StoredConversion:
    files = [(uploaded.name, uploaded.getvalue()) for uploaded in uploaded_files]
    progress = st.progress(0.0, text="Préparation…")

    def show_progress(completed: int, name: str) -> None:
        progress.progress(completed / len(files), text=f"{completed}/{len(files)} converties — {name}")

    batch = convert_batch(files, settings, on_progress=show_progress)
    progress.empty()

    # Le seuil porte sur le nombre de fichiers envoyés, comme annoncé à l'utilisateur.
    zip_data = build_zip(batch.results) if len(files) >= ZIP_THRESHOLD and batch.results else None
    return StoredConversion(batch=batch, input_count=len(files), zip_data=zip_data)


def render_summary(stored: StoredConversion) -> None:
    results = stored.batch.results
    original_total = sum(result.original_size for result in results)
    output_total = sum(result.output_size for result in results)
    saved_ratio = 1 - output_total / original_total if original_total else 0.0

    converted, before, after, change = st.columns(4)
    converted.metric("Converties", f"{len(results)}/{stored.input_count}")
    before.metric("Avant", format_size(original_total))
    after.metric("Après", format_size(output_total))
    change.metric("Poids", format_weight_change(saved_ratio))

    heavier_count = sum(1 for result in results if result.output_size > result.original_size)
    if heavier_count:
        st.info(
            f"{heavier_count} image(s) restent plus lourdes que l'original, même à qualité "
            f"{MIN_QUALITY} : la source était déjà très compressée. La qualité n'est pas "
            "descendue plus bas pour préserver le rendu.",
            icon=":material/info:",
        )


def render_failures(stored: StoredConversion) -> None:
    if not stored.batch.failures:
        return
    lines = "\n".join(f"- **{failure.source_name}** : {failure.reason}" for failure in stored.batch.failures)
    st.warning(f"{len(stored.batch.failures)} fichier(s) non converti(s) :\n\n{lines}", icon=":material/warning:")


def render_zip_download(stored: StoredConversion) -> None:
    results = stored.batch.results
    st.download_button(
        f"Télécharger le ZIP ({len(results)} images · {format_size(len(stored.zip_data))})",
        data=stored.zip_data,
        file_name=ZIP_FILE_NAME,
        mime=ZIP_MIME,
        type="primary",
        icon=":material/folder_zip:",
        on_click="ignore",
        width="stretch",
    )

    st.dataframe(
        [
            {
                "Fichier": result.output_name,
                "Original": format_size(result.original_size),
                "WebP": format_size(result.output_size),
                "Dimensions": f"{result.image.width} × {result.image.height}",
                "Qualité": quality_label(result.image),
                "Poids": format_weight_change(result.saved_ratio),
            }
            for result in results
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "Qualité": st.column_config.TextColumn(
                help="Qualité réellement utilisée : elle est réduite si la WebP dépassait le poids d'origine."
            )
        },
    )

    with st.expander("Aperçu des images converties"):
        for start in range(0, len(results), PREVIEW_COLUMNS):
            row = results[start : start + PREVIEW_COLUMNS]
            # La dernière ligne peut compter moins d'images que de colonnes.
            for column, result in zip(st.columns(PREVIEW_COLUMNS), row, strict=False):
                column.image(result.image.data, caption=result.output_name, width="stretch")


def render_individual_downloads(results: list[ConversionResult]) -> None:
    for index, result in enumerate(results):
        with st.container(border=True):
            preview, details, action = st.columns([1, 3, 2], vertical_alignment="center")
            preview.image(result.image.data, width=THUMBNAIL_WIDTH)
            details.markdown(f"**{result.output_name}**")
            details.caption(
                f"{format_size(result.original_size)} → {format_size(result.output_size)} "
                f"({format_weight_change(result.saved_ratio)}) · {result.image.width} × {result.image.height} px "
                f"· qualité {quality_label(result.image)}"
            )
            action.download_button(
                "Télécharger",
                data=result.image.data,
                file_name=result.output_name,
                mime=WEBP_MIME,
                icon=":material/download:",
                on_click="ignore",
                key=f"download_{index}",
                width="stretch",
            )


def render_results(stored: StoredConversion) -> None:
    st.divider()
    render_failures(stored)
    if not stored.batch.results:
        return
    render_summary(stored)
    if stored.zip_data is not None:
        render_zip_download(stored)
    else:
        render_individual_downloads(stored.batch.results)


def main() -> None:
    st.set_page_config(page_title="Compresseur WebP", page_icon=":material/photo_size_select_large:")
    render_header()

    submission = render_form()
    if submission is not None:
        uploaded_files, settings = submission
        # Les résultats précédents ne correspondent plus à la nouvelle demande.
        st.session_state.pop(SESSION_KEY, None)
        if not uploaded_files:
            st.info("Ajoutez au moins une image.", icon=":material/info:")
        elif len(uploaded_files) > MAX_FILES:
            st.error(
                f"{len(uploaded_files)} fichiers sélectionnés : {MAX_FILES} maximum. "
                f"Retirez-en {len(uploaded_files) - MAX_FILES}.",
                icon=":material/error:",
            )
        else:
            st.session_state[SESSION_KEY] = run_conversion(uploaded_files, settings)

    stored: StoredConversion | None = st.session_state.get(SESSION_KEY)
    if stored is not None:
        render_results(stored)


main()
