# Compresseur WebP

Plateforme Streamlit qui compresse et convertit des images en WebP, le format le plus
adapté au web.

- Jusqu'à **100 images** par conversion, **20 Mo** maximum par fichier, **300 Mo** au total.
- Formats acceptés : JPEG, PNG, GIF (animé compris), HEIC/HEIF, AVIF, WebP, TIFF, BMP,
  ICO, TGA, JPEG 2000. Le format est vérifié d'après le contenu du fichier, pas son extension.
- Moins de 5 fichiers envoyés : un bouton de téléchargement par image.
  **5 fichiers ou plus** : toutes les images dans un seul ZIP.

## Ce que fait la conversion

| Étape | Détail |
|---|---|
| Rotation | La rotation EXIF des photos de smartphone est appliquée. |
| Redimensionnement | Plus grand côté limité à 1280, 1920 (défaut) ou 2560 px, ou taille d'origine. Jamais d'agrandissement. |
| Compression | WebP qualité 80 par défaut, réglable de 70 à 95. Objectif : alléger sans dégrader. Si la WebP dépasse le poids d'origine, le sans perte est tenté pour les sources sans perte (PNG, GIF… : logos, captures d'écran), puis la qualité baisse par pas de 5 jusqu'à la plus haute qui tient sous ce poids. Elle ne descend jamais sous 70 : en dessous, la perte de détail devient visible (mesure SSIM sur photos réelles). La qualité réellement utilisée est affichée. Une photo n'est jamais encodée sans perte : elle serait 2 à 6 fois plus lourde. |
| Animations | Les GIF/PNG/WebP animés restent animés. |
| Métadonnées | EXIF et XMP (position GPS, appareil…) supprimés. Le profil couleur RGB est conservé. |
| Noms | `photo.jpg` → `photo.webp` ; les doublons deviennent `photo-2.webp`, `photo-3.webp`… |

Un fichier invalide n'arrête pas le lot : il est signalé, les autres sont convertis.

## Installation

Python 3.10 ou plus récent.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Lancement

```bash
.venv/bin/streamlit run app.py
```

L'application s'ouvre sur http://localhost:8501.

## Développement

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest
```

## Structure

| Fichier | Rôle |
|---|---|
| `converter.py` | Conversion, limites, noms de sortie, ZIP. Aucune dépendance à Streamlit. |
| `app.py` | Interface Streamlit. |
| `.streamlit/config.toml` | Thème clair/sombre et taille maximale d'envoi (`maxUploadSize`, à garder égal à `MAX_FILE_SIZE_MB`). |
| `tests/test_converter.py` | Tests de la conversion. |

## Limites connues

- Les SVG ne sont pas acceptés : un format vectoriel est déjà idéal pour le web.
- Les images CMJN sont converties en RGB sans gestion colorimétrique : les couleurs
  peuvent légèrement varier.
- Une image déjà très compressée (HEIC, AVIF, JPEG de faible qualité) peut rester un peu
  plus lourde en WebP, même à qualité 70. L'application le signale dans les résultats plutôt
  que de dégrader davantage l'image.
