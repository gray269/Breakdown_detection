from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, Toplevel, filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageTk


APP_NAME = "SuiviClaquage"
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}
MAX_ANALYSIS_SIDE = 800
ANALYSIS_CACHE: dict[tuple, tuple[bool, str, str]] = {}
ANALYSIS_CACHE_LOCK = threading.Lock()


def data_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    folder = base / APP_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


DATA_DIR = data_directory()
CONFIG_FILE = DATA_DIR / "parametres.json"
DATABASE_FILE = DATA_DIR / "historique.sqlite3"


DEFAULT_CONFIG = {
    "folder_gray": "",
    "folder_green": "",
    "filename_pattern": "picture_2",
    "analysis_date": date.today().isoformat(),
    "size": "16 mm",
    "interval_seconds": 30,
    "min_spot_diameter_percent": 1.8,
    "contrast_ratio": 0.20,
    "monitoring_enabled": True,
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            config.update(saved)
    except (OSError, json.JSONDecodeError):
        pass
    return config


def write_config(config: dict) -> None:
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def _connected_components(binary: np.ndarray) -> list[dict]:
    """Return 8-connected dark regions from a modest, downsampled mask."""
    height, width = binary.shape
    active = np.flatnonzero(binary)
    if active.size == 0:
        return []

    visited = np.zeros(binary.size, dtype=np.uint8)
    flat = binary.ravel()
    found: list[dict] = []
    for seed_value in active:
        seed = int(seed_value)
        if visited[seed]:
            continue
        visited[seed] = 1
        stack = [seed]
        xs: list[int] = []
        ys: list[int] = []
        while stack:
            index = stack.pop()
            y, x = divmod(index, width)
            xs.append(x)
            ys.append(y)
            for ny in range(max(0, y - 1), min(height, y + 2)):
                row = ny * width
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = row + nx
                    if not visited[neighbor] and flat[neighbor]:
                        visited[neighbor] = 1
                        stack.append(neighbor)

        found.append({
            "area": len(xs),
            "x": float(np.mean(xs)),
            "y": float(np.mean(ys)),
            "left": int(min(xs)),
            "top": int(min(ys)),
            "right": int(max(xs)),
            "bottom": int(max(ys)),
            "x_values": xs,
            "y_values": ys,
        })
    return found


def analyze_image(path: str | Path, min_diameter_percent: float, contrast_ratio: float) -> dict:
    """Look for a dark, connected spot inside a bright circular field."""
    original = Image.open(path).convert("RGB")
    original_size = original.size
    work = original.copy()
    work.thumbnail((MAX_ANALYSIS_SIDE, MAX_ANALYSIS_SIDE), Image.Resampling.LANCZOS)
    rgb = np.asarray(work, dtype=np.uint8)

    red = rgb[:, :, 0].astype(np.float32)
    green = rgb[:, :, 1].astype(np.float32)
    blue = rgb[:, :, 2].astype(np.float32)
    green_difference = green - np.maximum(red, blue)
    green_pixels = (green > 35) & (green_difference > 22)
    is_green = (
        np.count_nonzero(green_pixels) > rgb.shape[0] * rgb.shape[1] * 0.002
        and float(np.median(green_difference[green_pixels])) > 25
    )

    if is_green:
        signal = green
        bright = (green > 15) & (green_difference > 8)
        mode = "image verte"
    else:
        signal = (red + green + blue) / 3.0
        bright = signal > 12
        mode = "image niveaux de gris"

    high_percentile = float(np.percentile(signal, 99.5))
    threshold = max(10.0, high_percentile * 0.08)
    bright &= signal >= threshold
    ys, xs = np.nonzero(bright)
    if len(xs) < 100:
        return {
            "path": str(path), "detected": False, "mode": mode,
            "spots": [], "annotated": original, "error": "Disque lumineux non détecté",
        }

    # Percentile bounds and the radial distance are resistant to small defects,
    # including a point that cuts into the edge of the disk.
    cx = float(np.median(xs))
    cy = float(np.median(ys))
    distances = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    radius = float(np.percentile(distances, 99.7)) * 1.025
    if radius < 4:
        return {
            "path": str(path), "detected": False, "mode": mode,
            "spots": [], "annotated": original, "error": "Disque trop petit pour l’analyse",
        }

    height, width = signal.shape
    grid_y, grid_x = np.ogrid[:height, :width]
    radial_distance = np.sqrt((grid_x - cx) ** 2 + (grid_y - cy) ** 2)
    inside = radial_distance <= radius * 1.055

    # A radial median estimates the normal disk brightness at the same distance
    # from the edge. It prevents the dark background around the disk from being
    # mistaken for a defect, while preserving defects that touch the perimeter.
    radial_bins = np.floor(radial_distance).astype(np.int32)
    max_bin = int(min(np.max(radial_bins), math.ceil(radius * 1.06))) + 1
    profile = np.full(max_bin + 1, np.nan, dtype=np.float32)
    for bin_index in range(max_bin + 1):
        values = signal[inside & (radial_bins == bin_index)]
        if values.size:
            profile[bin_index] = float(np.median(values))
    known = np.flatnonzero(np.isfinite(profile))
    if known.size < 3:
        return {
            "path": str(path), "detected": False, "mode": mode,
            "spots": [], "annotated": original, "error": "Contraste insuffisant pour l’analyse",
        }
    profile = np.interp(np.arange(len(profile)), known, profile[known]).astype(np.float32)
    window = np.ones(7, dtype=np.float32) / 7.0
    profile = np.convolve(profile, window, mode="same")
    expected_radial = profile[np.minimum(radial_bins, len(profile) - 1)]

    blur_radius = max(2.0, radius * 0.035)
    channel_image = Image.fromarray(np.clip(signal, 0, 255).astype(np.uint8), mode="L")
    local_background = np.asarray(
        channel_image.filter(ImageFilter.GaussianBlur(radius=blur_radius)), dtype=np.float32
    )
    expected = np.maximum(expected_radial, local_background)
    darkness = expected - signal
    required_contrast = np.maximum(14.0, expected * float(contrast_ratio))
    dark_mask = inside & (darkness >= required_contrast) & (signal <= expected * 0.80)

    min_diameter = max(3.0, (2.0 * radius) * float(min_diameter_percent) / 100.0)
    min_area = max(5, int(math.pi * (min_diameter / 2.0) ** 2))
    components = _connected_components(dark_mask)
    accepted: list[dict] = []
    for component in components:
        x_values = np.asarray(component.pop("x_values"), dtype=np.int32)
        y_values = np.asarray(component.pop("y_values"), dtype=np.int32)
        component_radii = np.sqrt((x_values - cx) ** 2 + (y_values - cy) ** 2)
        touches_edge = bool(np.max(component_radii) >= radius * 0.965)
        needed_area = max(5, int(min_area * (0.48 if touches_edge else 1.0)))
        if component["area"] < needed_area:
            continue
        patch_values = signal[y_values, x_values]
        patch_expected = expected[y_values, x_values]
        very_dark_fraction = float(np.mean(patch_values < patch_expected * 0.55))
        average_contrast = float(np.mean(patch_expected - patch_values))
        if very_dark_fraction < (0.28 if touches_edge else 0.38):
            continue
        if average_contrast < 18:
            continue
        component["diameter"] = math.sqrt(4.0 * component["area"] / math.pi)
        component["touches_edge"] = touches_edge
        accepted.append(component)

    accepted.sort(key=lambda item: item["area"], reverse=True)
    scale_x = original_size[0] / width
    scale_y = original_size[1] / height
    draw = ImageDraw.Draw(original)
    disk_box = (
        (cx - radius) * scale_x,
        (cy - radius) * scale_y,
        (cx + radius) * scale_x,
        (cy + radius) * scale_y,
    )
    draw.ellipse(disk_box, outline=(255, 205, 0), width=max(3, original_size[0] // 700))
    spots: list[dict] = []
    for component in accepted:
        left = component["left"] * scale_x
        top = component["top"] * scale_y
        right = (component["right"] + 1) * scale_x
        bottom = (component["bottom"] + 1) * scale_y
        pad_x = max(7, (right - left) * 0.22)
        pad_y = max(7, (bottom - top) * 0.22)
        draw.ellipse(
            (left - pad_x, top - pad_y, right + pad_x, bottom + pad_y),
            outline=(255, 40, 40), width=max(4, original_size[0] // 500),
        )
        spots.append({
            "x": component["x"] * scale_x,
            "y": component["y"] * scale_y,
            "area_pixels": int(component["area"]),
            "touches_edge": component["touches_edge"],
        })

    return {
        "path": str(path), "detected": bool(spots), "mode": mode,
        "spots": spots, "annotated": original, "error": "",
    }


def scan_folders(config: dict) -> list[dict]:
    groups: dict[str, dict] = {}
    pattern = str(config.get("filename_pattern", "picture_2")).strip().casefold()
    roots = [
        ("gray", str(config.get("folder_gray", "")).strip()),
        ("green", str(config.get("folder_green", "")).strip()),
    ]
    for kind, root_text in roots:
        if not root_text:
            continue
        root = Path(root_text).expanduser()
        if not root.is_dir():
            continue
        try:
            product_folders = sorted(
                (item for item in root.iterdir() if item.is_dir()),
                key=lambda item: item.name.casefold(),
            )
            if product_folders:
                for product_folder in product_folders:
                    product_name = product_folder.name.strip()
                    if not product_name:
                        continue
                    product_key = product_name.casefold()
                    entry = groups.setdefault(product_key, {
                        "product_name": product_name, "gray": [], "green": [],
                    })
                    for file_path in product_folder.rglob("*"):
                        if not file_path.is_file() or file_path.suffix.casefold() not in IMAGE_EXTENSIONS:
                            continue
                        if pattern and pattern not in file_path.stem.casefold():
                            continue
                        entry[kind].append(file_path)
            else:
                # Also accept a root containing images directly, although product
                # subfolders are the expected structure.
                entry = groups.setdefault(".", {
                    "product_name": "Dossier racine", "gray": [], "green": [],
                })
                for file_path in root.iterdir():
                    if file_path.is_file() and file_path.suffix.casefold() in IMAGE_EXTENSIONS:
                        if not pattern or pattern in file_path.stem.casefold():
                            entry[kind].append(file_path)
        except OSError:
            continue

    results: list[dict] = []
    min_diameter = float(config.get("min_spot_diameter_percent", 1.8))
    contrast_ratio = float(config.get("contrast_ratio", 0.20))
    for group_key in sorted(groups, key=str.casefold):
        group = groups[group_key]
        states = {"gray": None, "green": None}
        image_paths = {"gray": [], "green": []}
        errors: list[str] = []
        for kind in ("gray", "green"):
            paths = sorted(group[kind], key=lambda p: str(p).casefold())
            if not paths:
                continue
            detected_any = False
            valid_count = 0
            for image_path in paths:
                image_paths[kind].append(str(image_path))
                try:
                    stat = image_path.stat()
                    cache_key = (
                        str(image_path.resolve()), stat.st_mtime_ns, stat.st_size,
                        min_diameter, contrast_ratio,
                    )
                    with ANALYSIS_CACHE_LOCK:
                        cached = ANALYSIS_CACHE.get(cache_key)
                    if cached is None:
                        detection = analyze_image(image_path, min_diameter, contrast_ratio)
                        cached = (bool(detection["detected"]), detection["mode"], detection["error"])
                        if not detection["error"]:
                            with ANALYSIS_CACHE_LOCK:
                                ANALYSIS_CACHE[cache_key] = cached
                    if cached[2]:
                        errors.append(f"{image_path.name}: {cached[2]}")
                    else:
                        valid_count += 1
                        detected_any = detected_any or cached[0]
                except Exception as exc:
                    errors.append(f"{image_path.name}: {exc}")
            if detected_any:
                states[kind] = True
            elif valid_count == len(paths):
                states[kind] = False
            else:
                states[kind] = None

        comparable = states["gray"] is not None and states["green"] is not None
        if not comparable:
            category = "À vérifier"
        elif states["gray"] and states["green"]:
            category = "Présent dans les deux"
        elif states["gray"]:
            category = "Dossier 1 uniquement"
        elif states["green"]:
            category = "Dossier 2 uniquement"
        else:
            category = "Aucun claquage"
        results.append({
            "group_key": group_key,
            "display_name": group["product_name"],
            "gray_state": states["gray"],
            "green_state": states["green"],
            "category": category,
            "comparable": comparable,
            "image_paths": image_paths,
            "errors": errors,
        })
    return results


def save_results(results: list[dict], config: dict) -> tuple[int, int]:
    analysis_date = str(config["analysis_date"])
    size = str(config["size"])
    scanned_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DATABASE_FILE) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS comparisons (
                analysis_date TEXT NOT NULL,
                size TEXT NOT NULL,
                product_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                gray_state TEXT NOT NULL,
                green_state TEXT NOT NULL,
                category TEXT NOT NULL,
                comparable INTEGER NOT NULL,
                image_paths TEXT NOT NULL,
                error_text TEXT NOT NULL,
                scanned_at TEXT NOT NULL,
                PRIMARY KEY (analysis_date, size, product_key)
            )
        """)
        connection.execute(
            "DELETE FROM comparisons WHERE analysis_date=? AND size=?",
            (analysis_date, size),
        )
        for row in results:
            gray_state = "oui" if row["gray_state"] is True else "non" if row["gray_state"] is False else "—"
            green_state = "oui" if row["green_state"] is True else "non" if row["green_state"] is False else "—"
            connection.execute("""
                INSERT INTO comparisons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                analysis_date, size, row["group_key"], row["display_name"],
                gray_state, green_state, row["category"], int(row["comparable"]),
                json.dumps(row["image_paths"], ensure_ascii=False),
                " | ".join(row["errors"]), scanned_at,
            ))
    total = sum(1 for item in results if item["comparable"])
    with_defect = sum(1 for item in results if item["category"] != "Aucun claquage" and item["comparable"])
    return total, with_defect


def read_current_results(analysis_date: str, size: str) -> list[tuple]:
    if not DATABASE_FILE.exists():
        return []
    try:
        with sqlite3.connect(DATABASE_FILE) as connection:
            return connection.execute("""
                SELECT display_name, gray_state, green_state, category, comparable, error_text
                FROM comparisons WHERE analysis_date=? AND size=?
                ORDER BY CASE category
                    WHEN 'Dossier 1 uniquement' THEN 1
                    WHEN 'Présent dans les deux' THEN 2
                    WHEN 'Dossier 2 uniquement' THEN 3
                    WHEN 'Aucun claquage' THEN 4
                    ELSE 5 END,
                    display_name COLLATE NOCASE
            """, (analysis_date, size)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return []
        raise


class ImageTestWindow:
    def __init__(self, parent: Tk | Toplevel, paths: tuple[str, ...], min_diameter: float, contrast: float):
        self.window = Toplevel(parent)
        self.window.title("Essai de détection")
        self.window.geometry("760x720")
        self.window.minsize(600, 520)
        self.paths = list(paths)
        self.min_diameter = min_diameter
        self.contrast = contrast
        self.index = 0
        self.photo = None

        self.summary = ttk.Label(self.window, text="", font=("Segoe UI", 12, "bold"))
        self.summary.pack(padx=12, pady=(12, 4))
        self.details = ttk.Label(self.window, text="", wraplength=700, justify="center")
        self.details.pack(padx=12, pady=4)
        self.image_label = ttk.Label(self.window, anchor="center")
        self.image_label.pack(fill="both", expand=True, padx=12, pady=8)
        buttons = ttk.Frame(self.window)
        buttons.pack(pady=(0, 12))
        ttk.Button(buttons, text="← Précédente", command=self.previous).pack(side="left", padx=6)
        ttk.Button(buttons, text="Suivante →", command=self.next).pack(side="left", padx=6)
        ttk.Button(buttons, text="Fermer", command=self.window.destroy).pack(side="left", padx=6)
        self.show_current()

    def show_current(self) -> None:
        if not self.paths:
            return
        path = self.paths[self.index]
        try:
            result = analyze_image(path, self.min_diameter, self.contrast)
            label = "CLAQUAGE DÉTECTÉ" if result["detected"] else "AUCUN POINT DÉTECTÉ"
            if result["error"]:
                label += " — " + result["error"]
            self.summary.configure(text=f"{label}  ({self.index + 1}/{len(self.paths)})")
            edge_spots = sum(1 for spot in result["spots"] if spot["touches_edge"])
            self.details.configure(
                text=f"{Path(path).name} · {result['mode']} · {len(result['spots'])} point(s)"
                + (f" dont {edge_spots} en périphérie" if edge_spots else "")
            )
            shown = result["annotated"]
            shown.thumbnail((680, 535), Image.Resampling.LANCZOS)
            self.photo = ImageTk.PhotoImage(shown)
            self.image_label.configure(image=self.photo)
        except Exception as exc:
            self.summary.configure(text="Image impossible à analyser")
            self.details.configure(text=f"{Path(path).name} · {exc}")
            self.image_label.configure(image="")

    def previous(self) -> None:
        self.index = (self.index - 1) % len(self.paths)
        self.show_current()

    def next(self) -> None:
        self.index = (self.index + 1) % len(self.paths)
        self.show_current()


class SettingsWindow:
    def __init__(self, app: "MonitorApp"):
        self.app = app
        self.window = Toplevel(app.root)
        self.window.title("Paramètres de surveillance")
        self.window.geometry("780x670")
        self.window.minsize(700, 600)
        self.window.transient(app.root)
        self.window.grab_set()

        self.folder_gray = StringVar(value=app.config.get("folder_gray", ""))
        self.folder_green = StringVar(value=app.config.get("folder_green", ""))
        self.pattern = StringVar(value=app.config.get("filename_pattern", "picture_2"))
        self.analysis_date = StringVar(value=app.config.get("analysis_date", date.today().isoformat()))
        self.size = StringVar(value=app.config.get("size", "16 mm"))
        self.interval = StringVar(value=str(app.config.get("interval_seconds", 30)))
        self.min_diameter = StringVar(value=str(app.config.get("min_spot_diameter_percent", 1.8)))
        self.contrast = StringVar(value=str(app.config.get("contrast_ratio", 0.20)))
        self.enabled = BooleanVar(value=bool(app.config.get("monitoring_enabled", True)))

        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Chaque sous-dossier de premier niveau correspond à un numéro produit. Le logiciel compare le même numéro dans les deux dossiers et cherche les fichiers dont le nom contient le motif indiqué.",
            wraplength=730,
        ).pack(anchor="w", pady=(0, 12))

        folders = ttk.LabelFrame(body, text="Dossiers à surveiller", padding=10)
        folders.pack(fill="x", pady=5)
        self._folder_row(folders, "Dossier 1 — images grises", self.folder_gray, 0)
        self._folder_row(folders, "Dossier 2 — images vertes", self.folder_green, 1)

        metadata = ttk.LabelFrame(body, text="Données du contrôle", padding=10)
        metadata.pack(fill="x", pady=5)
        ttk.Label(metadata, text="Date (AAAA-MM-JJ)").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(metadata, textvariable=self.analysis_date, width=18).grid(row=0, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(metadata, text="Taille du module").grid(row=0, column=2, sticky="w", padx=(20, 5), pady=5)
        ttk.Combobox(metadata, textvariable=self.size, values=("16 mm", "18 mm", "Autre"), width=12, state="readonly").grid(row=0, column=3, sticky="w", padx=5, pady=5)
        ttk.Label(metadata, text="Motif du nom de l’image").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(metadata, textvariable=self.pattern, width=20).grid(row=1, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(metadata, text="Exemple : picture_2").grid(row=1, column=2, columnspan=2, sticky="w", padx=5, pady=5)

        detection = ttk.LabelFrame(body, text="Réglage de détection", padding=10)
        detection.pack(fill="x", pady=5)
        ttk.Label(detection, text="Diamètre minimal du point (% du diamètre du disque)").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(detection, textvariable=self.min_diameter, width=8).grid(row=0, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(detection, text="Contraste minimal (0 à 1)").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(detection, textvariable=self.contrast, width=8).grid(row=1, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(detection, text="Les points proches du bord sont aussi recherchés.", foreground="#555555").grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=4)

        background = ttk.LabelFrame(body, text="Fonctionnement en arrière-plan", padding=10)
        background.pack(fill="x", pady=5)
        ttk.Checkbutton(background, text="Surveiller les dossiers automatiquement", variable=self.enabled).grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Label(background, text="Intervalle (secondes)").grid(row=0, column=1, sticky="e", padx=(20, 5), pady=5)
        ttk.Combobox(background, textvariable=self.interval, values=("10", "30", "60", "120"), width=8, state="readonly").grid(row=0, column=2, sticky="w", padx=5, pady=5)

        note = ttk.Label(
            body,
            text="Pour calculer le taux « dossier 2 uniquement », les images vertes sont aussi analysées pour les numéros sans claquage gris. Un produit n’entre dans les taux que si les deux images ont pu être analysées. Mode essai : sélectionnez des images pour voir le résultat et la zone détectée.",
            wraplength=730,
        )
        note.pack(anchor="w", pady=10)

        actions = ttk.Frame(body)
        actions.pack(fill="x", side="bottom", pady=(10, 0))
        ttk.Button(actions, text="Tester des images…", command=self.test_images).pack(side="left")
        ttk.Button(actions, text="Enregistrer", command=self.save).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="Analyser maintenant", command=self.save_and_scan).pack(side="right", padx=6)
        ttk.Button(actions, text="Fermer", command=self.window.destroy).pack(side="right", padx=6)

    @staticmethod
    def _folder_row(parent: ttk.LabelFrame, label: str, variable: StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=7)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=5, pady=7)
        ttk.Button(parent, text="Parcourir…", command=lambda: SettingsWindow._choose_folder(variable)).grid(row=row, column=2, padx=5, pady=7)
        parent.columnconfigure(1, weight=1)

    @staticmethod
    def _choose_folder(variable: StringVar) -> None:
        selected = filedialog.askdirectory(title="Choisir un dossier")
        if selected:
            variable.set(selected)

    def values(self) -> dict | None:
        try:
            datetime.strptime(self.analysis_date.get().strip(), "%Y-%m-%d")
            min_diameter = float(self.min_diameter.get().replace(",", "."))
            contrast = float(self.contrast.get().replace(",", "."))
            interval = int(self.interval.get())
            if min_diameter <= 0 or min_diameter > 20:
                raise ValueError("Le diamètre minimal doit être compris entre 0 et 20 %.")
            if contrast <= 0 or contrast >= 1:
                raise ValueError("Le contraste doit être compris entre 0 et 1.")
            if not self.pattern.get().strip():
                raise ValueError("Le motif du nom de fichier ne peut pas être vide.")
        except ValueError as exc:
            messagebox.showerror("Paramètres invalides", str(exc), parent=self.window)
            return None
        return {
            "folder_gray": self.folder_gray.get().strip(),
            "folder_green": self.folder_green.get().strip(),
            "filename_pattern": self.pattern.get().strip(),
            "analysis_date": self.analysis_date.get().strip(),
            "size": self.size.get(),
            "interval_seconds": interval,
            "min_spot_diameter_percent": min_diameter,
            "contrast_ratio": contrast,
            "monitoring_enabled": self.enabled.get(),
        }

    def save(self) -> bool:
        config = self.values()
        if config is None:
            return False
        self.app.set_config(config)
        self.window.destroy()
        return True

    def save_and_scan(self) -> None:
        if self.save():
            self.app.scan_now()

    def test_images(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Sélectionner une ou plusieurs images",
            filetypes=(("Images", "*.tif *.tiff *.jpg *.jpeg *.png *.bmp"), ("Tous les fichiers", "*.*")),
        )
        if not paths:
            return
        try:
            min_diameter = float(self.min_diameter.get().replace(",", "."))
            contrast = float(self.contrast.get().replace(",", "."))
        except ValueError:
            messagebox.showerror("Réglage invalide", "Vérifiez le diamètre minimal et le contraste.", parent=self.window)
            return
        ImageTestWindow(self.window, paths, min_diameter, contrast)


class MonitorApp:
    def __init__(self, root: Tk):
        self.root = root
        self.config = load_config()
        self.settings_window: SettingsWindow | None = None
        self.scan_running = False
        self.next_scan_id: str | None = None
        self.category_names = (
            "Dossier 1 uniquement",
            "Présent dans les deux",
            "Dossier 2 uniquement",
        )
        self.rate_vars = {name: StringVar(value="— %") for name in self.category_names}
        self.rate_count_vars = {name: StringVar(value="0 / 0 produit") for name in self.category_names}
        self.count_var = StringVar(value="En attente des paramètres")
        self.status_var = StringVar(value="Surveillance arrêtée")
        self.filter_var = StringVar(value="")
        self._build_main_window()
        self.refresh_summary()
        self.root.after(350, self.open_settings)
        self.schedule_next_scan(3)

    def _build_main_window(self) -> None:
        self.root.title("Suivi du claquage")
        self.root.geometry("520x475")
        self.root.minsize(490, 430)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)
        header = ttk.Frame(frame)
        header.pack(fill="x")
        ttk.Label(header, text="COMPARAISON DES CLAQUAGES PAR NUMÉRO PRODUIT", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        cards = ttk.Frame(frame)
        cards.pack(fill="x", pady=(8, 4))
        rate_labels = (
            ("Dossier 1 seul", self.category_names[0]),
            ("Dossier 1 + 2", self.category_names[1]),
            ("Dossier 2 seul", self.category_names[2]),
        )
        for column, (title, category) in enumerate(rate_labels):
            card = ttk.LabelFrame(cards, text=title, padding=(7, 4))
            card.grid(row=0, column=column, sticky="nsew", padx=3)
            cards.columnconfigure(column, weight=1)
            ttk.Label(card, textvariable=self.rate_vars[category], font=("Segoe UI", 18, "bold"), foreground="#b42318").pack()
            ttk.Label(card, textvariable=self.rate_count_vars[category], font=("Segoe UI", 8)).pack()

        ttk.Label(frame, textvariable=self.count_var, font=("Segoe UI", 10)).pack()
        ttk.Label(frame, textvariable=self.filter_var, foreground="#555555").pack(pady=(2, 4))
        ttk.Label(frame, textvariable=self.status_var, foreground="#555555").pack(pady=(0, 4))

        columns = ("produit", "gris", "vert", "categorie")
        self.table = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        self.table.heading("produit", text="Numéro produit")
        self.table.heading("gris", text="Gris")
        self.table.heading("vert", text="Vert")
        self.table.heading("categorie", text="Résultat")
        self.table.column("produit", width=140, anchor="w")
        self.table.column("gris", width=50, anchor="center")
        self.table.column("vert", width=50, anchor="center")
        self.table.column("categorie", width=190, anchor="w")
        self.table.pack(fill="both", expand=True, pady=(3, 8))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Paramètres", command=self.open_settings).pack(side="left")
        ttk.Button(buttons, text="Analyser", command=self.scan_now).pack(side="left", padx=6)
        ttk.Button(buttons, text="CSV", command=self.export_csv).pack(side="left")
        ttk.Button(buttons, text="Réduire", command=self.root.iconify).pack(side="right")

    def set_config(self, config: dict) -> None:
        self.config = config
        write_config(config)
        self.refresh_summary()
        self.status_var.set("Paramètres enregistrés")
        self.schedule_next_scan(2)

    def open_settings(self) -> None:
        if self.settings_window and self.settings_window.window.winfo_exists():
            self.settings_window.window.lift()
            return
        self.settings_window = SettingsWindow(self)

    def scan_now(self) -> None:
        if self.scan_running:
            self.status_var.set("Analyse déjà en cours…")
            return
        config = dict(self.config)
        if not config.get("folder_gray") and not config.get("folder_green"):
            self.status_var.set("Choisissez au moins un dossier")
            self.open_settings()
            return
        missing = [
            label for label, folder in (("gris", config.get("folder_gray")), ("vert", config.get("folder_green")))
            if folder and not Path(folder).expanduser().is_dir()
        ]
        if missing:
            self.status_var.set("Dossier introuvable : " + ", ".join(missing))
            return
        self.scan_running = True
        self.status_var.set("Analyse en cours…")
        worker = threading.Thread(target=self._scan_worker, args=(config,), daemon=True)
        worker.start()

    def _scan_worker(self, config: dict) -> None:
        started = time.monotonic()
        try:
            results = scan_folders(config)
            total, defects = save_results(results, config)
            duration = time.monotonic() - started
            self.root.after(0, lambda: self._scan_finished(results, total, defects, duration, ""))
        except Exception as exc:
            self.root.after(0, lambda message=str(exc): self._scan_finished([], 0, 0, 0, message))

    def _scan_finished(self, results: list[dict], total: int, defects: int, duration: float, error: str) -> None:
        self.scan_running = False
        if error:
            self.status_var.set("Erreur d’analyse")
            messagebox.showerror("Erreur", error, parent=self.root)
            return
        self.refresh_summary()
        if not results:
            self.status_var.set("Aucune image correspondant au motif")
        elif total == 0:
            self.status_var.set("Aucune paire grise/verte analysable pour les mêmes produits")
        else:
            self.status_var.set(f"Mis à jour · {datetime.now().strftime('%H:%M:%S')} · {duration:.1f} s")

    def refresh_summary(self) -> None:
        analysis_date = str(self.config.get("analysis_date", date.today().isoformat()))
        size = str(self.config.get("size", "16 mm"))
        rows = read_current_results(analysis_date, size)
        comparable_rows = [row for row in rows if row[4]]
        total = len(comparable_rows)
        category_counts = {
            category: sum(1 for row in comparable_rows if row[3] == category)
            for category in self.category_names
        }
        defects = sum(category_counts.values())
        if total:
            for category in self.category_names:
                count = category_counts[category]
                self.rate_vars[category].set(f"{100.0 * count / total:.1f} %")
                self.rate_count_vars[category].set(f"{count} / {total} produit(s)")
            self.count_var.set(f"{defects} produit(s) avec claquage · {total} paire(s) comparable(s)")
        else:
            for category in self.category_names:
                self.rate_vars[category].set("— %")
                self.rate_count_vars[category].set("0 / 0 produit")
            self.count_var.set("Aucun produit analysable dans les deux dossiers")
        self.filter_var.set(f"{analysis_date} · {size}")
        for item in self.table.get_children():
            self.table.delete(item)
        tags = {
            "Dossier 1 uniquement": "gray_only",
            "Présent dans les deux": "both",
            "Dossier 2 uniquement": "green_only",
            "À vérifier": "unknown",
        }
        for display_name, gray_state, green_state, category, comparable, error_text in rows[:500]:
            tag = tags.get(category, "")
            self.table.insert("", "end", values=(display_name, gray_state, green_state, category), tags=(tag,) if tag else ())
        self.table.tag_configure("gray_only", foreground="#9a6700")
        self.table.tag_configure("both", foreground="#b42318")
        self.table.tag_configure("green_only", foreground="#0969da")
        self.table.tag_configure("unknown", foreground="#6e7781")

    def schedule_next_scan(self, delay_seconds: int | None = None) -> None:
        if self.next_scan_id is not None:
            try:
                self.root.after_cancel(self.next_scan_id)
            except Exception:
                pass
        if delay_seconds is None:
            delay_seconds = max(5, int(self.config.get("interval_seconds", 30)))
        self.next_scan_id = self.root.after(delay_seconds * 1000, self._automatic_scan_tick)

    def _automatic_scan_tick(self) -> None:
        self.next_scan_id = None
        if self.config.get("monitoring_enabled") and (self.config.get("folder_gray") or self.config.get("folder_green")):
            self.scan_now()
        self.schedule_next_scan()

    def export_csv(self) -> None:
        if not DATABASE_FILE.exists():
            messagebox.showinfo("Export CSV", "Aucun résultat à exporter.", parent=self.root)
            return
        destination = filedialog.asksaveasfilename(
            title="Exporter l’historique",
            defaultextension=".csv",
            initialfile="rapport_claquage.csv",
            filetypes=(("CSV", "*.csv"),),
        )
        if not destination:
            return
        try:
            with sqlite3.connect(DATABASE_FILE) as connection:
                rows = connection.execute("""
                    SELECT analysis_date, size, display_name, gray_state, green_state,
                           category, comparable, image_paths, error_text, scanned_at
                    FROM comparisons ORDER BY analysis_date DESC, size, display_name COLLATE NOCASE
                """).fetchall()
            with open(destination, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream, delimiter=";")
                writer.writerow(("Date", "Taille", "Numéro produit", "Détecté en gris", "Détecté en vert", "Catégorie", "Paire comparable", "Chemins des images", "Erreur", "Dernière analyse"))
                writer.writerows(rows)
            self.status_var.set("Rapport CSV exporté")
        except Exception as exc:
            messagebox.showerror("Export impossible", str(exc), parent=self.root)

    def close(self) -> None:
        if self.next_scan_id is not None:
            try:
                self.root.after_cancel(self.next_scan_id)
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = Tk()
    try:
        ttk.Style(root).theme_use("vista" if os.name == "nt" else "clam")
    except Exception:
        pass
    MonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
