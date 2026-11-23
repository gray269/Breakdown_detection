from __future__ import annotations

import csv
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import traceback
import zipfile
from datetime import date, datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageTk


APP_NAME = "SuiviClaquage"
APP_VERSION = "2026.11.20-cachefix"
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}
MAX_ANALYSIS_SIDE = 640
ANALYSIS_CACHE_VERSION = 2
# Bump this table suffix whenever the image detection logic changes. Older
# cached labels were produced before the disk-boundary correction and are stale.
IMAGE_ANALYSIS_CACHE_TABLE = "image_analysis_cache_v2"
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
LOG_DIRECTORY = DATA_DIR / "logs"


class AnalysisLogger:
    """Write one flushed, human-readable diagnostic log for each scan."""

    def __init__(self) -> None:
        LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.path = LOG_DIRECTORY / f"analyse_{stamp}.log"
        self._stream = self.path.open("w", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self.write(f"APPLICATION | version={APP_VERSION}")
        self.write("Démarrage du journal d’analyse")
        old_logs = sorted(LOG_DIRECTORY.glob("analyse_*.log"), key=lambda item: item.name, reverse=True)
        for obsolete in old_logs[30:]:
            try:
                obsolete.unlink()
            except OSError:
                pass

    def write(self, message: str) -> None:
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        with self._lock:
            self._stream.write(f"{timestamp} | {message.rstrip()}\n")
            self._stream.flush()

    def write_exception(self, message: str) -> None:
        self.write(f"{message}\n{traceback.format_exc().rstrip()}")

    def close(self) -> None:
        with self._lock:
            if not self._stream.closed:
                self._stream.flush()
                self._stream.close()


DEFAULT_CONFIG = {
    "folder_gray": "",
    "folder_green": "",
    "results_folder": "",
    "filename_pattern": "picture_2",
    "analysis_start_date": date.today().isoformat(),
    "analysis_end_date": date.today().isoformat(),
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
            config.pop("size", None)
            legacy_date = saved.get("analysis_date")
            if "analysis_start_date" not in saved:
                config["analysis_start_date"] = legacy_date or date.today().isoformat()
            if "analysis_end_date" not in saved:
                config["analysis_end_date"] = legacy_date or config["analysis_start_date"]
            config.pop("analysis_date", None)
    except (OSError, json.JSONDecodeError):
        pass
    return config


def write_config(config: dict) -> None:
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def _connected_components(binary: np.ndarray) -> list[dict]:
    """Find 8-connected regions by joining horizontal runs instead of pixels."""
    height, _width = binary.shape
    parents: list[int] = []
    ranks: list[int] = []
    runs: list[tuple[int, int, int, int]] = []
    previous: list[tuple[int, int, int]] = []

    def find(value: int) -> int:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if ranks[left_root] < ranks[right_root]:
            left_root, right_root = right_root, left_root
        parents[right_root] = left_root
        if ranks[left_root] == ranks[right_root]:
            ranks[left_root] += 1

    for y in range(height):
        row = binary[y].astype(np.int8, copy=False)
        changes = np.diff(np.pad(row, (1, 1)))
        starts = np.flatnonzero(changes == 1)
        ends = np.flatnonzero(changes == -1) - 1
        current: list[tuple[int, int, int]] = []
        previous_index = 0
        for start_value, end_value in zip(starts, ends):
            start, end = int(start_value), int(end_value)
            label = len(parents)
            parents.append(label)
            ranks.append(0)
            runs.append((y, start, end, label))
            while previous_index < len(previous) and previous[previous_index][1] < start - 1:
                previous_index += 1
            scan_index = previous_index
            while scan_index < len(previous) and previous[scan_index][0] <= end + 1:
                union(label, previous[scan_index][2])
                scan_index += 1
            current.append((start, end, label))
        previous = current

    grouped_runs: dict[int, list[tuple[int, int, int]]] = {}
    for y, start, end, label in runs:
        grouped_runs.setdefault(find(label), []).append((y, start, end))

    components: list[dict] = []
    for component_runs in grouped_runs.values():
        area = sum(end - start + 1 for _y, start, end in component_runs)
        x_sum = sum((start + end) * (end - start + 1) / 2 for _y, start, end in component_runs)
        y_sum = sum(y * (end - start + 1) for y, start, end in component_runs)
        components.append({
            "area": area,
            "x": x_sum / area,
            "y": y_sum / area,
            "left": min(start for _y, start, _end in component_runs),
            "top": min(y for y, _start, _end in component_runs),
            "right": max(end for _y, _start, end in component_runs),
            "bottom": max(y for y, _start, _end in component_runs),
            "runs": component_runs,
        })
    return components


def analyze_image(
    path: str | Path,
    min_diameter_percent: float,
    contrast_ratio: float,
    annotate: bool = True,
    max_side: int = MAX_ANALYSIS_SIDE,
) -> dict:
    """Look for a dark, connected spot inside a bright circular field."""
    with Image.open(path) as source:
        original_size = source.size
        original = source.convert("RGB") if annotate else None
        # Resize while the file is still open, then detach the reduced pixels.
        # The batch scan uses annotate=False; accessing a lazy Pillow image
        # after leaving this context caused every TIFF to fail in batch mode.
        source.thumbnail((max_side, max_side), Image.Resampling.BOX)
        work = source.convert("RGB")
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
    radius = float(np.percentile(distances, 99.7)) * 1.005
    if radius < 4:
        return {
            "path": str(path), "detected": False, "mode": mode,
            "spots": [], "annotated": original, "error": "Disque trop petit pour l’analyse",
        }

    height, width = signal.shape
    grid_y, grid_x = np.ogrid[:height, :width]
    radial_distance = np.sqrt((grid_x - cx) ** 2 + (grid_y - cy) ** 2)
    # Keep the detection mask inside the illuminated field. A wide margin here
    # includes the black image background as one huge edge-connected "spot".
    # The light mask used above excludes real dark spots from the radius fit,
    # so the small inset still leaves enough of perimeter defects to detect.
    inside = radial_distance <= radius * 0.995

    # A radial median estimates the normal disk brightness at the same distance
    # from the edge. It prevents the dark background around the disk from being
    # mistaken for a defect, while preserving defects that touch the perimeter.
    radial_bins = np.floor(radial_distance).astype(np.int32)
    max_bin = int(min(np.max(radial_bins), math.ceil(radius * 1.06))) + 1
    signal_uint8 = np.clip(signal, 0, 255).astype(np.uint8)
    bin_values = radial_bins[inside] * 256 + signal_uint8[inside]
    histogram = np.bincount(
        bin_values,
        minlength=(max_bin + 1) * 256,
    ).reshape(max_bin + 1, 256)
    bin_counts = histogram.sum(axis=1)
    cumulative = np.cumsum(histogram, axis=1)
    median_targets = ((bin_counts + 1) // 2)[:, None]
    profile = np.full(max_bin + 1, np.nan, dtype=np.float32)
    nonempty = bin_counts > 0
    profile[nonempty] = np.argmax(cumulative[nonempty] >= median_targets[nonempty], axis=1)
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
    channel_image = Image.fromarray(signal_uint8)
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
        farthest_radius = max(
            math.hypot(x - cx, y - cy)
            for y, left, right in component["runs"]
            for x in (left, right)
        )
        touches_edge = farthest_radius >= radius * 0.975
        needed_area = max(5, int(min_area * (0.48 if touches_edge else 1.0)))
        if component["area"] < needed_area:
            continue
        component_width = component["right"] - component["left"] + 1
        component_height = component["bottom"] - component["top"] + 1
        aspect_ratio = max(component_width, component_height) / max(1, min(component_width, component_height))
        fill_ratio = component["area"] / max(1, component_width * component_height)
        # Reject edge streaks and broad background bands. The target defects
        # are compact, dark spots; a peripheral spot can be clipped, but should
        # still fill a reasonable portion of its bounding box.
        if aspect_ratio > 3.0 or fill_ratio < 0.16:
            continue
        very_dark_count = 0
        contrast_total = 0.0
        for y, left, right in component["runs"]:
            patch_values = signal[y, left:right + 1]
            patch_expected = expected[y, left:right + 1]
            very_dark_count += int(np.count_nonzero(patch_values < patch_expected * 0.55))
            contrast_total += float(np.sum(patch_expected - patch_values))
        very_dark_fraction = very_dark_count / component["area"]
        average_contrast = contrast_total / component["area"]
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
    draw = ImageDraw.Draw(original) if annotate and original is not None else None
    if draw is not None:
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
        if draw is not None:
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


def _date_folder_key(folder_name: str) -> str | None:
    for date_format in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y", "%d_%m_%Y", "%d.%m.%Y", "%Y_%m_%d"):
        try:
            return datetime.strptime(folder_name.strip(), date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _canonical_product_name(folder_name: str) -> str:
    """Remove a trailing capture timestamp from product folders, if present."""
    name = folder_name.strip()
    match = re.fullmatch(r"(\d+)_(?:\d{8}|\d{14})", name)
    return match.group(1) if match else name


def _exception_description(exc: Exception) -> str:
    """Include exception type and arguments, even when str(exc) is empty."""
    return f"{type(exc).__name__}: {exc!r}"


def _classify_states(gray_state: bool | None, green_state: bool | None) -> str:
    """Compare a product only when both image analyses produced a result."""
    if gray_state is None or green_state is None:
        return "À vérifier"
    if gray_state is True and green_state is True:
        return "Présent dans les deux"
    if gray_state is True:
        return "Dossier 1 uniquement"
    if green_state is True:
        return "Dossier 2 uniquement"
    if gray_state is False and green_state is False:
        return "Aucun claquage"
    return "À vérifier"


def _folder_created_date(folder: Path) -> date | None:
    """Read the folder creation date (st_birthtime where available, Windows ctime otherwise)."""
    try:
        stat = folder.stat()
        timestamp = getattr(stat, "st_birthtime", None)
        if timestamp is None:
            timestamp = stat.st_ctime
        return datetime.fromtimestamp(timestamp).date()
    except (OSError, OverflowError, ValueError):
        return None


def _picture_2_in(product_folder: Path, pattern: str) -> Path | None:
    """Return one exact picture_2 file from the product folder, without walking deeper."""
    extension_priority = {".tif": 0, ".tiff": 1, ".png": 2, ".jpg": 3, ".jpeg": 4, ".bmp": 5}
    try:
        candidates = [
            item for item in product_folder.iterdir()
            if item.is_file()
            and item.suffix.casefold() in IMAGE_EXTENSIONS
            and item.stem.strip().casefold() == pattern
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return min(candidates, key=lambda item: (extension_priority.get(item.suffix.casefold(), 99), item.name.casefold()))


def scan_folders(config: dict, progress_callback=None, logger: AnalysisLogger | None = None) -> list[dict]:
    min_diameter = float(config.get("min_spot_diameter_percent", 1.8))
    contrast_ratio = float(config.get("contrast_ratio", 0.20))
    connection = sqlite3.connect(DATABASE_FILE)
    try:
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {IMAGE_ANALYSIS_CACHE_TABLE} (
                path TEXT NOT NULL,
                modified_ns INTEGER NOT NULL,
                file_size INTEGER NOT NULL,
                min_diameter REAL NOT NULL,
                contrast_ratio REAL NOT NULL,
                max_side INTEGER NOT NULL,
                detected INTEGER NOT NULL,
                mode TEXT NOT NULL,
                error_text TEXT NOT NULL,
                PRIMARY KEY (path, modified_ns, file_size, min_diameter, contrast_ratio, max_side)
            )
        """)
        rows = connection.execute(f"""
            SELECT path, modified_ns, file_size, min_diameter, contrast_ratio,
                   max_side, detected, mode, error_text
            FROM {IMAGE_ANALYSIS_CACHE_TABLE}
            WHERE min_diameter=? AND contrast_ratio=?
        """, (min_diameter, contrast_ratio)).fetchall()
        with ANALYSIS_CACHE_LOCK:
            for path, modified_ns, file_size, min_value, contrast_value, max_side, detected, mode, error_text in rows:
                key = (ANALYSIS_CACHE_VERSION, path, modified_ns, file_size, min_value, contrast_value, max_side)
                ANALYSIS_CACHE[key] = (bool(detected), mode, error_text)
        connection.commit()
        return _scan_folders_with_cache(config, progress_callback, connection, logger)
    finally:
        connection.commit()
        connection.close()


def _scan_folders_with_cache(
    config: dict,
    progress_callback,
    cache_connection: sqlite3.Connection,
    logger: AnalysisLogger | None = None,
) -> list[dict]:
    """Scan exact picture_2 images for product/date folders in the selected interval."""
    groups: dict[str, dict] = {}
    pattern = str(config.get("filename_pattern", "picture_2")).strip().casefold()
    start_date = datetime.strptime(config["analysis_start_date"], "%Y-%m-%d").date()
    end_date = datetime.strptime(config["analysis_end_date"], "%Y-%m-%d").date()
    if progress_callback:
        progress_callback({
            "phase": "search",
            "message": f"Recherche des dossiers picture_2 du {start_date:%d/%m/%Y} au {end_date:%d/%m/%Y}…",
        })

    roots = [("gray", str(config.get("folder_gray", "")).strip()),
             ("green", str(config.get("folder_green", "")).strip())]
    discovery = {
        "gray": {"folders": 0, "in_interval": 0, "missing_date": 0, "outside_interval": 0, "images": 0},
        "green": {"folders": 0, "in_interval": 0, "missing_date": 0, "outside_interval": 0, "images": 0},
    }
    for kind, root_text in roots:
        if not root_text:
            if logger:
                logger.write(f"SOURCE_SEARCH | source={kind} | ignorée: aucun dossier configuré")
            continue
        root = Path(root_text).expanduser()
        if not root.is_dir():
            if logger:
                logger.write(f"SOURCE_SEARCH_ERROR | source={kind} | dossier inexistant ou inaccessible: {root}")
            continue
        try:
            if logger:
                logger.write(f"SOURCE_SEARCH | source={kind} | racine={root}")
            top_folders = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold())
            if kind == "gray":
                product_folders = [(item, None) for item in top_folders]
            else:
                # Green layout can be root/date/product or a selected date
                # folder/product. Use the YYYYMMDD date-folder name when it is
                # available; product-folder creation times can change when
                # data is copied and do not identify the image capture date.
                root_date_key = _date_folder_key(root.name)
                dated_children = [item for item in top_folders if _date_folder_key(item.name)]
                if root_date_key is not None:
                    root_date = date.fromisoformat(root_date_key)
                    product_folders = (
                        [(item, root_date) for item in top_folders]
                        if start_date <= root_date <= end_date else []
                    )
                elif dated_children:
                    product_folders = []
                    for date_folder in dated_children:
                        folder_date = date.fromisoformat(_date_folder_key(date_folder.name))
                        if not start_date <= folder_date <= end_date:
                            continue
                        product_folders.extend(
                            (item, folder_date)
                            for item in date_folder.iterdir() if item.is_dir()
                        )
                else:
                    # Also handle date-folder labels that are not parseable as
                    # a numeric date: a date/grouping folder contains product
                    # directories. In this fallback, product-folder creation
                    # time is the only available date signal.
                    nested_folders: list[tuple[Path, date | None]] = []
                    for possible_date_folder in top_folders:
                        nested_folders.extend(
                            (item, None)
                            for item in possible_date_folder.iterdir() if item.is_dir()
                        )
                    product_folders = sorted(
                        nested_folders or [(item, None) for item in top_folders],
                        key=lambda item: item[0].name.casefold(),
                    )
            product_folders.sort(key=lambda item: (
                _canonical_product_name(item[0].name).casefold(),
                item[0].name.casefold() != _canonical_product_name(item[0].name).casefold(),
                item[0].name.casefold(),
            ))
            folder_total = len(product_folders)
            discovery[kind]["folders"] += folder_total
            for folder_index, (product_folder, date_from_parent) in enumerate(product_folders, start=1):
                if progress_callback and (folder_index == 1 or folder_index % 50 == 0 or folder_index == folder_total):
                    progress_callback({
                        "phase": "search_progress", "done": folder_index,
                        "total": folder_total, "source": "gris" if kind == "gray" else "vert",
                    })
                product_name = product_folder.name.strip()
                if not product_name:
                    continue
                created_on = date_from_parent or _folder_created_date(product_folder)
                if created_on is None:
                    discovery[kind]["missing_date"] += 1
                    continue
                if not start_date <= created_on <= end_date:
                    discovery[kind]["outside_interval"] += 1
                    continue
                discovery[kind]["in_interval"] += 1
                canonical_name = _canonical_product_name(product_name)
                product_key = canonical_name.casefold()
                entry = groups.setdefault(product_key, {
                    "product_name": canonical_name, "gray": None, "green": None,
                })
                image_path = _picture_2_in(product_folder, pattern)
                if image_path is not None and entry[kind] is None:
                    entry[kind] = image_path
                    discovery[kind]["images"] += 1
        except OSError:
            if logger:
                logger.write_exception(f"SOURCE_SEARCH_ERROR | source={kind} | racine={root}")
            continue
        if logger:
            summary = discovery[kind]
            logger.write(
                f"SOURCE_SEARCH_SUMMARY | source={kind} | dossiers={summary['folders']} | "
                f"dans_intervalle={summary['in_interval']} | dates_absentes={summary['missing_date']} | "
                f"hors_intervalle={summary['outside_interval']} | picture_2_trouvées={summary['images']}"
            )

    min_diameter = float(config.get("min_spot_diameter_percent", 1.8))
    contrast_ratio = float(config.get("contrast_ratio", 0.20))
    ordered_keys = sorted(groups, key=str.casefold)
    total_images = sum(
        int(groups[key]["gray"] is not None)
        + int(groups[key]["green"] is not None)
        for key in ordered_keys
    )
    processed_images = 0
    if progress_callback:
        progress_callback({"phase": "process", "done": 0, "total": total_images})

    cache_writes = 0
    cache_hits = 0
    decoded_images = 0

    def detect(image_path: Path, max_side: int) -> tuple[bool, str, str, bool]:
        nonlocal cache_writes, cache_hits, decoded_images
        stat = image_path.stat()
        resolved_path = str(image_path.resolve())
        cache_key = (
            ANALYSIS_CACHE_VERSION, resolved_path, stat.st_mtime_ns, stat.st_size,
            min_diameter, contrast_ratio, max_side,
        )
        with ANALYSIS_CACHE_LOCK:
            cached = ANALYSIS_CACHE.get(cache_key)
        cache_hit = cached is not None
        if cached is None:
            decoded_images += 1
            detection = analyze_image(
                image_path, min_diameter, contrast_ratio, annotate=False, max_side=max_side
            )
            cached = (bool(detection["detected"]), detection["mode"], detection["error"])
            # Keep negative and unusable images cached too; automatic refreshes
            # should not decode the same unchanged TIFFs over and over.
            with ANALYSIS_CACHE_LOCK:
                ANALYSIS_CACHE[cache_key] = cached
            cache_connection.execute(f"""
                INSERT OR REPLACE INTO {IMAGE_ANALYSIS_CACHE_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                resolved_path, stat.st_mtime_ns, stat.st_size,
                min_diameter, contrast_ratio, max_side,
                int(cached[0]), cached[1], cached[2],
            ))
            cache_writes += 1
            if cache_writes % 100 == 0:
                cache_connection.commit()
        else:
            cache_hits += 1
        return cached[0], cached[1], cached[2], cache_hit

    states = {key: {"gray": None, "green": None} for key in ordered_keys}
    errors = {key: [] for key in ordered_keys}

    # Complete the gray pass first at the same resolution as the test window.
    for key in ordered_keys:
        image_path = groups[key]["gray"]
        if image_path is None:
            continue
        gray_pass = "gris"
        try:
            detected, _mode, error, cache_hit = detect(image_path, MAX_ANALYSIS_SIDE)
            if error:
                errors[key].append(f"{image_path.name}: {error}")
                if logger:
                    logger.write(
                        f"IMAGE_ERROR | source=gray | produit={groups[key]['product_name']} | "
                        f"cache={cache_hit} | fichier={image_path} | détail={error}"
                    )
            else:
                states[key]["gray"] = detected
                if logger:
                    logger.write(
                        f"IMAGE_RESULT | source=gray | produit={groups[key]['product_name']} | "
                        f"détecté={'oui' if detected else 'non'} | cache={cache_hit} | fichier={image_path}"
                    )
        except Exception as exc:
            errors[key].append(f"{image_path.name}: {_exception_description(exc)}")
            if logger:
                logger.write_exception(
                    f"IMAGE_EXCEPTION | source=gray | produit={groups[key]['product_name']} | fichier={image_path} | "
                    f"détail={_exception_description(exc)}"
                )
        processed_images += 1
        if progress_callback and (processed_images % 5 == 0 or processed_images == total_images):
            progress_callback({
                "phase": "process", "done": processed_images, "total": total_images,
                "product": groups[key]["product_name"], "source": gray_pass,
            })

    # Analyze the green source independently, even if its gray counterpart is
    # missing or unreadable. The join happens only after both passes finish.
    for key in ordered_keys:
        image_path = groups[key]["green"]
        if image_path is None:
            continue
        green_pass = "vert"
        try:
            detected, _mode, error, cache_hit = detect(image_path, MAX_ANALYSIS_SIDE)
            if error:
                errors[key].append(f"{image_path.name}: {error}")
                if logger:
                    logger.write(
                        f"IMAGE_ERROR | source=green | produit={groups[key]['product_name']} | "
                        f"cache={cache_hit} | fichier={image_path} | détail={error}"
                    )
            else:
                states[key]["green"] = detected
                if logger:
                    logger.write(
                        f"IMAGE_RESULT | source=green | produit={groups[key]['product_name']} | "
                        f"détecté={'oui' if detected else 'non'} | cache={cache_hit} | fichier={image_path}"
                    )
        except Exception as exc:
            errors[key].append(f"{image_path.name}: {_exception_description(exc)}")
            if logger:
                logger.write_exception(
                    f"IMAGE_EXCEPTION | source=green | produit={groups[key]['product_name']} | fichier={image_path} | "
                    f"détail={_exception_description(exc)}"
                )
        processed_images += 1
        if progress_callback and (processed_images % 5 == 0 or processed_images == total_images):
            progress_callback({
                "phase": "process", "done": processed_images, "total": total_images,
                "product": groups[key]["product_name"], "source": green_pass,
            })

    results: list[dict] = []
    for key in ordered_keys:
        group = groups[key]
        gray_state, green_state = states[key]["gray"], states[key]["green"]
        comparable = gray_state is not None and green_state is not None
        category = _classify_states(gray_state, green_state)
        results.append({
            "group_key": key,
            "display_name": group["product_name"],
            "gray_state": gray_state,
            "green_state": green_state,
            "category": category,
            "comparable": comparable,
            "image_paths": {
                "gray": [str(group["gray"])] if group["gray"] else [],
                "green": [str(group["green"])] if group["green"] else [],
            },
            "errors": errors[key],
        })
    if progress_callback:
        progress_callback({"phase": "save", "done": processed_images, "total": total_images})
    if logger:
        logger.write(
            f"SCAN_SUMMARY | images={total_images} | décodées={decoded_images} | cache={cache_hits} | "
            f"erreurs={sum(len(item) for item in errors.values())} | produits={len(results)}"
        )
        for kind, label in (("gray", "gris"), ("green", "vert")):
            found = sum(groups[key][kind] is not None for key in ordered_keys)
            analyzed = sum(states[key][kind] is not None for key in ordered_keys)
            positives = sum(states[key][kind] is True for key in ordered_keys)
            logger.write(
                f"SOURCE_RESULT_SUMMARY | source={label} | images_trouvées={found} | "
                f"analysées={analyzed} | détectées={positives} | sans_détection={analyzed - positives}"
            )
        category_counts = {
            category: sum(row["comparable"] and row["category"] == category for row in results)
            for category in ("Dossier 1 uniquement", "Présent dans les deux", "Dossier 2 uniquement")
        }
        logger.write(
            f"COMPARISON_SUMMARY | paires={sum(row['comparable'] for row in results)} | "
            + " | ".join(f"{name}={count}" for name, count in category_counts.items())
        )
    return results


def save_results(results: list[dict], config: dict) -> tuple[int, int]:
    start_date = str(config["analysis_start_date"])
    end_date = str(config["analysis_end_date"])
    scanned_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DATABASE_FILE) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS comparisons_intervals (
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                product_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                gray_state TEXT NOT NULL,
                green_state TEXT NOT NULL,
                category TEXT NOT NULL,
                comparable INTEGER NOT NULL,
                image_paths TEXT NOT NULL,
                error_text TEXT NOT NULL,
                scanned_at TEXT NOT NULL,
                PRIMARY KEY (start_date, end_date, product_key)
            )
        """)
        connection.execute(
            "DELETE FROM comparisons_intervals WHERE start_date=? AND end_date=?",
            (start_date, end_date),
        )
        for row in results:
            gray_state = "oui" if row["gray_state"] is True else "non" if row["gray_state"] is False else "—"
            green_state = "oui" if row["green_state"] is True else "non" if row["green_state"] is False else "—"
            connection.execute("""
                INSERT INTO comparisons_intervals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                start_date, end_date, row["group_key"], row["display_name"],
                gray_state, green_state, row["category"], int(row["comparable"]),
                json.dumps(row["image_paths"], ensure_ascii=False),
                " | ".join(row["errors"]), scanned_at,
            ))
    comparable_products = sum(1 for item in results if item["comparable"])
    with_defect = sum(
        1 for item in results
        if item["comparable"] and item["category"] in {
            "Dossier 1 uniquement", "Présent dans les deux", "Dossier 2 uniquement"
        }
    )
    return comparable_products, with_defect


def read_current_results(start_date: str, end_date: str) -> list[tuple]:
    if not DATABASE_FILE.exists():
        return []
    try:
        with sqlite3.connect(DATABASE_FILE) as connection:
            return connection.execute("""
                SELECT display_name, gray_state, green_state, category, comparable, error_text
                FROM comparisons_intervals WHERE start_date=? AND end_date=?
                ORDER BY CASE category
                    WHEN 'Dossier 1 uniquement' THEN 1
                    WHEN 'Présent dans les deux' THEN 2
                    WHEN 'Dossier 2 uniquement' THEN 3
                    WHEN 'Aucun claquage' THEN 4
                    ELSE 5 END,
                    display_name COLLATE NOCASE
            """, (start_date, end_date)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return []
        raise


def read_archive_rows(start_date: str, end_date: str) -> list[tuple]:
    """Read source states and paths for archive generation for one interval."""
    if not DATABASE_FILE.exists():
        return []
    try:
        with sqlite3.connect(DATABASE_FILE) as connection:
            return connection.execute("""
                SELECT display_name, gray_state, green_state, image_paths
                FROM comparisons_intervals
                WHERE start_date=? AND end_date=?
                ORDER BY display_name COLLATE NOCASE
            """, (start_date, end_date)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return []
        raise


ARCHIVE_CATEGORIES = {
    "Dossier 1 uniquement": "T1_Dossier1_uniquement.zip",
    "Présent dans les deux": "T2_Present_dans_les_deux.zip",
    "Dossier 2 uniquement": "T3_Dossier2_uniquement.zip",
}


def create_category_archives(
    start_date: str,
    end_date: str,
    destination_root: str | Path,
    progress_callback=None,
) -> tuple[Path, dict[str, int], list[str]]:
    """Create one ZIP per comparable defect category, each with gray and green images."""
    rows = read_archive_rows(start_date, end_date)
    categorized: dict[str, list[tuple[str, dict[str, list[str]]]]] = {
        category: [] for category in ARCHIVE_CATEGORIES
    }
    errors: list[str] = []
    for display_name, gray_label, green_label, paths_json in rows:
        gray_state = {"oui": True, "non": False}.get(gray_label)
        green_state = {"oui": True, "non": False}.get(green_label)
        category = _classify_states(gray_state, green_state)
        if category not in categorized:
            continue
        try:
            image_paths = json.loads(paths_json)
            gray_paths = image_paths.get("gray", [])
            green_paths = image_paths.get("green", [])
            if not gray_paths or not green_paths:
                errors.append(f"{category} | {display_name} | image grise ou verte absente du résultat")
                continue
            categorized[category].append((display_name, {
                "gray": [str(gray_paths[0])],
                "green": [str(green_paths[0])],
            }))
        except (json.JSONDecodeError, TypeError, AttributeError, IndexError) as exc:
            errors.append(f"{category} | {display_name} | chemins illisibles : {_exception_description(exc)}")
            continue

    root = Path(destination_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_directory = root / f"Resultats_claquage_{start_date}_{end_date}_{stamp}"
    output_directory.mkdir()

    total_products = sum(len(products) for products in categorized.values())
    processed = 0
    counts = {category: 0 for category in ARCHIVE_CATEGORIES}
    for category, archive_name in ARCHIVE_CATEGORIES.items():
        archive_path = output_directory / archive_name
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(
                "Lisez-moi.txt",
                f"Catégorie : {category}\nPériode : {start_date} au {end_date}\n"
                "Chaque dossier produit contient l’image grise et l’image verte picture_2.\n",
            )
            for product_name, image_paths in categorized[category]:
                processed += 1
                try:
                    gray_path = Path(image_paths["gray"][0])
                    green_path = Path(image_paths["green"][0])
                    gray_bytes = gray_path.read_bytes()
                    green_bytes = green_path.read_bytes()
                    product_folder = re.sub(r'[<>:"/\\|?*]+', "_", product_name.strip()) or "produit_inconnu"
                    archive.writestr(f"{product_folder}/gris_{gray_path.name}", gray_bytes)
                    archive.writestr(f"{product_folder}/vert_{green_path.name}", green_bytes)
                    counts[category] += 1
                except (OSError, ValueError) as exc:
                    detail = f"{category} | {product_name} | {_exception_description(exc)}"
                    errors.append(detail)
                if progress_callback:
                    progress_callback({
                        "phase": "archive", "done": processed, "total": total_products,
                        "product": product_name, "category": category,
                    })

    summary_lines = [
        f"Période : {start_date} au {end_date}",
        f"Dossier de résultats : {output_directory}",
        "",
    ]
    summary_lines.extend(f"{category} : {counts[category]} produit(s)" for category in ARCHIVE_CATEGORIES)
    summary_lines.extend(["", f"Erreurs de copie : {len(errors)}"])
    if errors:
        summary_lines.extend(errors)
    (output_directory / "resume_archivage.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    return output_directory, counts, errors


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
            self.details.configure(text=f"{Path(path).name} · {_exception_description(exc)}")
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
        self.window.geometry("780x730")
        self.window.minsize(700, 640)
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self.save_and_close)

        self.folder_gray = StringVar(value=app.config.get("folder_gray", ""))
        self.folder_green = StringVar(value=app.config.get("folder_green", ""))
        self.results_folder = StringVar(value=app.config.get("results_folder", ""))
        self.pattern = StringVar(value=app.config.get("filename_pattern", "picture_2"))
        self.analysis_start_date = StringVar(value=app.config.get("analysis_start_date", date.today().isoformat()))
        self.analysis_end_date = StringVar(value=app.config.get("analysis_end_date", date.today().isoformat()))
        self.interval = StringVar(value=str(app.config.get("interval_seconds", 30)))
        self.min_diameter = StringVar(value=str(app.config.get("min_spot_diameter_percent", 1.8)))
        self.contrast = StringVar(value=str(app.config.get("contrast_ratio", 0.20)))
        self.enabled = BooleanVar(value=bool(app.config.get("monitoring_enabled", True)))
        self.save_status_var = StringVar(value="")

        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Dossier 1 : la racine contient les sous-dossiers par numéro produit. Dossier 2 : la racine contient les sous-dossiers par date, puis par numéro produit. La période filtre la date de création des dossiers gris et la date du dossier vert (format AAAAMMJJ). Les dossiers gris suffixés par un horodatage sont associés au numéro produit de base. Seule l’image nommée exactement picture_2 est retenue.",
            wraplength=730,
        ).pack(anchor="w", pady=(0, 12))

        folders = ttk.LabelFrame(body, text="Dossiers à surveiller", padding=10)
        folders.pack(fill="x", pady=5)
        self._folder_row(folders, "Dossier 1 — images grises", self.folder_gray, 0)
        self._folder_row(folders, "Dossier 2 — images vertes", self.folder_green, 1)

        results = ttk.LabelFrame(body, text="Export des résultats en archives ZIP", padding=10)
        results.pack(fill="x", pady=5)
        self._folder_row(results, "Dossier de destination", self.results_folder, 0)
        ttk.Label(
            results,
            text="Le bouton « Archives » créera un sous-dossier daté avec un ZIP par catégorie T1, T2 et T3.",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 4))

        metadata = ttk.LabelFrame(body, text="Période et image à analyser", padding=10)
        metadata.pack(fill="x", pady=5)
        ttk.Label(metadata, text="Du (AAAA-MM-JJ)").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(metadata, textvariable=self.analysis_start_date, width=16).grid(row=0, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(metadata, text="Au (AAAA-MM-JJ)").grid(row=0, column=2, sticky="w", padx=(20, 5), pady=5)
        ttk.Entry(metadata, textvariable=self.analysis_end_date, width=16).grid(row=0, column=3, sticky="w", padx=5, pady=5)
        ttk.Label(metadata, text="Nom exact de l’image").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(metadata, textvariable=self.pattern, width=20).grid(row=1, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(metadata, text="Nom exact : picture_2 · toutes tailles regroupées").grid(row=1, column=2, columnspan=2, sticky="w", padx=5, pady=5)

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
            text="Pour le dossier vert, les dossiers date AAAAMMJJ sont filtrés directement sur la période. Pour le dossier gris, la date de création du dossier produit est utilisée. Les images passent par la même résolution de détection que le mode essai afin de garder les mêmes résultats. Les détections sont conservées localement et réutilisées si le fichier et les réglages n’ont pas changé. Mode essai : sélectionnez des images pour voir le résultat et la zone détectée.",
            wraplength=730,
        )
        note.pack(anchor="w", pady=10)

        actions = ttk.Frame(body)
        actions.pack(fill="x", side="bottom", pady=(10, 0))
        ttk.Button(actions, text="Tester des images…", command=self.test_images).pack(side="left")
        ttk.Label(actions, textvariable=self.save_status_var).pack(side="left", padx=8)
        ttk.Button(actions, text="Enregistrer", command=self.save).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="Analyser maintenant", command=self.save_and_scan).pack(side="right", padx=6)
        ttk.Button(actions, text="Fermer et enregistrer", command=self.save_and_close).pack(side="right", padx=6)

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
            start_date = datetime.strptime(self.analysis_start_date.get().strip(), "%Y-%m-%d").date()
            end_date = datetime.strptime(self.analysis_end_date.get().strip(), "%Y-%m-%d").date()
            if start_date > end_date:
                raise ValueError("La date de début doit être antérieure ou égale à la date de fin.")
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
            "results_folder": self.results_folder.get().strip(),
            "filename_pattern": self.pattern.get().strip(),
            "analysis_start_date": self.analysis_start_date.get().strip(),
            "analysis_end_date": self.analysis_end_date.get().strip(),
            "interval_seconds": interval,
            "min_spot_diameter_percent": min_diameter,
            "contrast_ratio": contrast,
            "monitoring_enabled": self.enabled.get(),
        }

    def save(self) -> bool:
        config = self.values()
        if config is None:
            return False
        try:
            self.app.set_config(config)
        except Exception as exc:
            self.save_status_var.set("Échec de l’enregistrement")
            messagebox.showerror(
                "Enregistrement impossible",
                f"Les paramètres n’ont pas été enregistrés.\n\n{_exception_description(exc)}",
                parent=self.window,
            )
            return False
        self.save_status_var.set(f"Enregistré · {datetime.now():%H:%M:%S}")
        return True

    def _destroy_window(self) -> None:
        try:
            self.window.grab_release()
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass
        self.app.settings_window = None

    def save_and_close(self) -> None:
        if self.save():
            self._destroy_window()

    def close(self) -> None:
        self.save_and_close()

    def save_and_scan(self) -> None:
        if self.save():
            self._destroy_window()
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
        self.archive_running = False
        self.last_scan_signature: tuple | None = None
        self.progress_queue: queue.Queue = queue.Queue()
        self.progress_mode = "determinate"
        self.next_scan_id: str | None = None
        try:
            previous_logs = list(LOG_DIRECTORY.glob("analyse_*.log"))
            self.latest_log_path: Path | None = max(previous_logs, key=lambda item: item.stat().st_mtime) if previous_logs else None
        except OSError:
            self.latest_log_path = None
        self.category_names = (
            "Dossier 1 uniquement",
            "Présent dans les deux",
            "Dossier 2 uniquement",
        )
        self.rate_vars = {name: StringVar(value="— %") for name in self.category_names}
        self.rate_count_vars = {name: StringVar(value="0 / 0 paire") for name in self.category_names}
        self.source_summary_var = StringVar(value="Détections individuelles : en attente")
        self.count_var = StringVar(value="Paires comparables : en attente")
        self.status_var = StringVar(value="Surveillance arrêtée")
        self.progress_text_var = StringVar(value="Prêt")
        self.filter_var = StringVar(value="")
        self._build_main_window()
        self.refresh_summary()
        self.root.after(350, self.open_settings)
        self.schedule_next_scan(3)

    def _build_main_window(self) -> None:
        self.root.title("Suivi du claquage")
        self.root.geometry("560x515")
        self.root.minsize(540, 480)
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

        ttk.Label(frame, textvariable=self.source_summary_var, font=("Segoe UI", 9)).pack(pady=(1, 0))
        ttk.Label(frame, textvariable=self.count_var, font=("Segoe UI", 9)).pack()
        ttk.Label(
            frame,
            text="À vérifier = image manquante ou illisible ; exclu des taux T1/T2/T3",
            font=("Segoe UI", 8), foreground="#555555",
        ).pack()
        ttk.Label(frame, textvariable=self.filter_var, foreground="#555555").pack(pady=(2, 4))
        progress_row = ttk.Frame(frame)
        progress_row.pack(fill="x", pady=(0, 4))
        ttk.Label(progress_row, textvariable=self.progress_text_var, width=34, anchor="w").pack(side="left")
        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate", maximum=1, length=200)
        self.progress_bar.pack(side="right", fill="x", expand=True, padx=(8, 0))
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
        ttk.Button(buttons, text="Réouvrir les paramètres", command=self.open_settings).pack(side="left")
        ttk.Button(buttons, text="Analyser", command=self.scan_now).pack(side="left", padx=6)
        ttk.Button(buttons, text="CSV", command=self.export_csv).pack(side="left")
        ttk.Button(buttons, text="Archives", command=self.create_archives).pack(side="left", padx=6)
        ttk.Button(buttons, text="Journal", command=self.open_diagnostics).pack(side="left", padx=6)
        ttk.Button(buttons, text="Réduire", command=self.root.iconify).pack(side="right")

    def set_config(self, config: dict) -> None:
        analysis_changed = self._analysis_signature(self.config) != self._analysis_signature(config)
        self.config = config
        write_config(config)
        self.refresh_summary()
        self.status_var.set("Paramètres enregistrés")
        # Changing only the archive destination must not trigger a costly
        # image rescan. Analysis settings still refresh promptly in background.
        self.schedule_next_scan(2 if analysis_changed else None)

    def open_settings(self) -> None:
        if self.settings_window:
            try:
                if self.settings_window.window.winfo_exists():
                    self.settings_window.window.deiconify()
                    self.settings_window.window.lift()
                    self.settings_window.window.focus_force()
                    return
            except Exception:
                pass
        self.settings_window = SettingsWindow(self)

    def _write_preflight_log(self, config: dict, message: str) -> None:
        try:
            logger = AnalysisLogger()
            logger.write("CONFIG | " + json.dumps(config, ensure_ascii=False))
            logger.write(f"PREFLIGHT_ERROR | {message}")
            self.latest_log_path = logger.path
            logger.close()
        except Exception:
            pass

    def scan_now(self) -> None:
        if self.archive_running:
            self.status_var.set("Création des archives en cours…")
            return
        if self.scan_running:
            self.status_var.set("Analyse déjà en cours…")
            return
        config = dict(self.config)
        if not config.get("folder_gray") and not config.get("folder_green"):
            self._write_preflight_log(config, "Aucun dossier gris ou vert n’est configuré.")
            self.status_var.set("Choisissez au moins un dossier")
            self.open_settings()
            return
        missing = [
            label for label, folder in (("gris", config.get("folder_gray")), ("vert", config.get("folder_green")))
            if folder and not Path(folder).expanduser().is_dir()
        ]
        if missing:
            message = "Dossier introuvable ou inaccessible : " + ", ".join(missing)
            self._write_preflight_log(config, message)
            self.status_var.set(message + " · ouvrir Journal")
            return
        while True:
            try:
                self.progress_queue.get_nowait()
            except queue.Empty:
                break
        self.scan_running = True
        self.last_scan_signature = None
        self.status_var.set("Analyse en cours…")
        self.progress_text_var.set("Recherche des images…")
        self.progress_bar.configure(mode="indeterminate", maximum=100, value=0)
        self.progress_bar.start(12)
        worker = threading.Thread(target=self._scan_worker, args=(config,), daemon=True)
        worker.start()
        self.root.after(100, self._poll_progress)

    def _poll_progress(self) -> None:
        latest = None
        while True:
            try:
                latest = self.progress_queue.get_nowait()
            except queue.Empty:
                break
        if latest:
            phase = latest.get("phase")
            if phase == "search":
                if self.progress_mode != "indeterminate":
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="indeterminate")
                    self.progress_bar.start(12)
                    self.progress_mode = "indeterminate"
                self.progress_text_var.set(latest.get("message", "Recherche des images…"))
            elif phase == "search_progress":
                if self.progress_mode != "determinate":
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
                    self.progress_mode = "determinate"
                total = int(latest.get("total", 0))
                done = int(latest.get("done", 0))
                self.progress_bar.configure(maximum=max(1, total), value=done)
                self.progress_text_var.set(
                    f"Dossiers {latest.get('source', '')} · {done}/{total}"
                )
            elif phase in {"process", "save"}:
                if self.progress_mode != "determinate":
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
                    self.progress_mode = "determinate"
                total = int(latest.get("total", 0))
                done = int(latest.get("done", 0))
                self.progress_bar.configure(maximum=max(1, total), value=done)
                if phase == "save":
                    self.progress_text_var.set("Enregistrement des résultats…")
                elif total:
                    self.progress_text_var.set(
                        f"{done}/{total} · {latest.get('source', '')} · {latest.get('product', '')}"
                    )
                else:
                    self.progress_text_var.set("Aucune image trouvée")
            elif phase == "archive":
                if self.progress_mode != "determinate":
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
                    self.progress_mode = "determinate"
                total = int(latest.get("total", 0))
                done = int(latest.get("done", 0))
                self.progress_bar.configure(maximum=max(1, total), value=done)
                self.progress_text_var.set(
                    f"ZIP {done}/{total} · {latest.get('category', '')} · {latest.get('product', '')}"
                )
        if self.scan_running or self.archive_running:
            self.root.after(100, self._poll_progress)

    def _scan_worker(self, config: dict) -> None:
        started = time.monotonic()
        logger = None
        try:
            logger = AnalysisLogger()
            self.latest_log_path = logger.path
            logger.write(
                "CONFIG | " + json.dumps({
                    "analysis_start_date": config.get("analysis_start_date"),
                    "analysis_end_date": config.get("analysis_end_date"),
                    "folder_gray": config.get("folder_gray"),
                    "folder_green": config.get("folder_green"),
                    "results_folder": config.get("results_folder"),
                    "filename_pattern": config.get("filename_pattern"),
                    "min_spot_diameter_percent": config.get("min_spot_diameter_percent"),
                    "contrast_ratio": config.get("contrast_ratio"),
                }, ensure_ascii=False),
            )
            logger.write(
                f"IMAGE_CACHE | version={ANALYSIS_CACHE_VERSION} | table={IMAGE_ANALYSIS_CACHE_TABLE}"
            )
            results = scan_folders(config, progress_callback=self.progress_queue.put, logger=logger)
            total, defects = save_results(results, config)
            self.last_scan_signature = self._analysis_signature(config)
            duration = time.monotonic() - started
            logger.write(
                f"RUN_FINISHED | résultat_enregistré | durée_secondes={duration:.2f} | "
                f"produits={len(results)} | paires={total} | détections_dans_paires={defects}"
            )
            log_path = logger.path
            self.root.after(0, lambda: self._scan_finished(results, total, defects, duration, "", log_path))
        except Exception as exc:
            message = _exception_description(exc)
            if logger:
                logger.write_exception(f"RUN_FATAL | {message}")
                log_path = logger.path
            else:
                log_path = None
            self.root.after(0, lambda: self._scan_finished([], 0, 0, 0, message, log_path))
        finally:
            if logger:
                logger.close()

    def _scan_finished(
        self,
        results: list[dict],
        total: int,
        defects: int,
        duration: float,
        error: str,
        log_path: Path | None = None,
    ) -> None:
        self.scan_running = False
        if log_path:
            self.latest_log_path = log_path
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=1, value=0 if error else 1)
        self.progress_mode = "determinate"
        if error:
            self.progress_text_var.set("Échec de l’analyse")
            self.status_var.set("Erreur d’analyse · ouvrir Journal pour le détail")
            messagebox.showerror(
                "Erreur d’analyse",
                error + (f"\n\nJournal détaillé : {log_path}" if log_path else ""),
                parent=self.root,
            )
            return
        self.refresh_summary()
        if not results:
            self.progress_text_var.set("Aucun dossier dans l’intervalle")
            self.status_var.set("Aucun dossier produit créé pendant cette période")
        else:
            gray_analyzed = sum(row["gray_state"] is not None for row in results)
            green_analyzed = sum(row["green_state"] is not None for row in results)
            pair_count = sum(row["comparable"] for row in results)
            gray_products = sum(bool(row["image_paths"]["gray"]) for row in results)
            green_products = sum(bool(row["image_paths"]["green"]) for row in results)
            errors = [error for row in results for error in row["errors"]]
            self.progress_text_var.set(
                f"Terminé · gris {gray_analyzed}/{gray_products} · vert {green_analyzed}/{green_products} · paires {pair_count}"
            )
            if errors:
                first_error = errors[0][:110]
                self.status_var.set(f"{len(errors)} erreur(s) · exemple : {first_error} · ouvrir Journal")
            elif not gray_analyzed and not green_analyzed:
                self.status_var.set("Aucune image analysable · vérifier l’accès aux fichiers et les dates")
            else:
                self.status_var.set(f"Mis à jour · {datetime.now().strftime('%H:%M:%S')} · {duration:.1f} s")

    def create_archives(self) -> None:
        if self.scan_running:
            self.status_var.set("Attends la fin de l’analyse avant de créer les archives")
            return
        if self.archive_running:
            self.status_var.set("Création des archives déjà en cours…")
            return
        destination = str(self.config.get("results_folder", "")).strip()
        if not destination:
            self.status_var.set("Choisis un dossier de destination dans les paramètres")
            self.open_settings()
            return
        start_date = str(self.config.get("analysis_start_date", ""))
        end_date = str(self.config.get("analysis_end_date", ""))
        if self.last_scan_signature != self._analysis_signature(self.config):
            messagebox.showinfo(
                "Analyse nécessaire",
                "Lance une analyse avec les paramètres actuels avant de créer les archives. Cela évite d’archiver les résultats d’un ancien dossier ou d’une ancienne période.",
                parent=self.root,
            )
            return
        try:
            if not read_archive_rows(start_date, end_date):
                messagebox.showinfo(
                    "Aucun résultat",
                    "Aucun résultat enregistré pour cette période. Lance d’abord une analyse.",
                    parent=self.root,
                )
                return
        except Exception as exc:
            messagebox.showerror("Lecture des résultats impossible", _exception_description(exc), parent=self.root)
            return

        while True:
            try:
                self.progress_queue.get_nowait()
            except queue.Empty:
                break
        self.archive_running = True
        self.status_var.set("Création des ZIP par catégorie…")
        self.progress_text_var.set("Préparation des images à archiver…")
        self.progress_bar.configure(mode="indeterminate", maximum=100, value=0)
        self.progress_bar.start(12)
        worker = threading.Thread(
            target=self._archive_worker,
            args=(start_date, end_date, destination),
            daemon=True,
        )
        worker.start()
        self.root.after(100, self._poll_progress)

    @staticmethod
    def _analysis_signature(config: dict) -> tuple:
        return (
            str(config.get("folder_gray", "")).strip(),
            str(config.get("folder_green", "")).strip(),
            str(config.get("filename_pattern", "picture_2")).strip().casefold(),
            str(config.get("analysis_start_date", "")),
            str(config.get("analysis_end_date", "")),
            float(config.get("min_spot_diameter_percent", 1.8)),
            float(config.get("contrast_ratio", 0.20)),
        )

    def _archive_worker(self, start_date: str, end_date: str, destination: str) -> None:
        try:
            output_directory, counts, errors = create_category_archives(
                start_date, end_date, destination,
                progress_callback=self.progress_queue.put,
            )
            self.root.after(
                0,
                lambda: self._archive_finished(output_directory, counts, errors, ""),
            )
        except Exception as exc:
            message = _exception_description(exc)
            logger = None
            log_path = None
            try:
                logger = AnalysisLogger()
                logger.write_exception(f"ARCHIVE_FATAL | {message}")
                log_path = logger.path
                self.latest_log_path = log_path
            except Exception:
                pass
            finally:
                if logger:
                    logger.close()
            self.root.after(
                0,
                lambda: self._archive_finished(None, {}, [], message, log_path),
            )

    def _archive_finished(
        self,
        output_directory: Path | None,
        counts: dict[str, int],
        errors: list[str],
        error: str,
        log_path: Path | None = None,
    ) -> None:
        self.archive_running = False
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=1, value=0 if error else 1)
        self.progress_mode = "determinate"
        if log_path:
            self.latest_log_path = log_path
        if error:
            self.progress_text_var.set("Échec de la création des archives")
            self.status_var.set("Erreur pendant la création des ZIP · ouvrir Journal")
            message = error + (f"\n\nJournal : {log_path}" if log_path else "")
            messagebox.showerror("Création des archives impossible", message, parent=self.root)
            return
        total = sum(counts.values())
        self.progress_text_var.set(f"Archives créées · {total} produit(s)")
        if errors:
            self.status_var.set(f"{len(errors)} copie(s) impossible(s) · détails dans resume_archivage.txt")
        else:
            self.status_var.set("Archives créées sans erreur")
        summary = "\n".join(
            f"{category} : {counts.get(category, 0)} produit(s)"
            for category in ARCHIVE_CATEGORIES
        )
        if errors:
            summary += f"\n\n{len(errors)} image(s) n’ont pas pu être copiées. Voir resume_archivage.txt."
        messagebox.showinfo(
            "Archives prêtes",
            f"Les archives ont été créées dans :\n{output_directory}\n\n{summary}",
            parent=self.root,
        )

    def refresh_summary(self) -> None:
        start_date = str(self.config.get("analysis_start_date", date.today().isoformat()))
        end_date = str(self.config.get("analysis_end_date", start_date))
        rows = read_current_results(start_date, end_date)
        classified_rows = []
        for row in rows:
            gray_value = {"oui": True, "non": False}.get(row[1])
            green_value = {"oui": True, "non": False}.get(row[2])
            classified_rows.append((row, _classify_states(gray_value, green_value)))
        gray_rows = [row for row in rows if row[1] in {"oui", "non"}]
        green_rows = [row for row in rows if row[2] in {"oui", "non"}]
        pair_rows = [row for row in rows if row[1] in {"oui", "non"} and row[2] in {"oui", "non"}]
        pair_count = len(pair_rows)
        category_counts = {
            category: sum(
                1 for row in pair_rows
                if _classify_states(row[1] == "oui", row[2] == "oui") == category
            )
            for category in self.category_names
        }
        defects = sum(category_counts.values())
        gray_positive = sum(1 for row in gray_rows if row[1] == "oui")
        green_positive = sum(1 for row in green_rows if row[2] == "oui")

        def source_rate(label: str, positive: int, analyzed: int) -> str:
            if analyzed == 0:
                return f"{label} : — (0/0 image)"
            percentage = 100.0 * positive / analyzed
            return f"{label} : {positive}/{analyzed} ({percentage:.1f} %)"

        self.source_summary_var.set(
            source_rate("Gris", gray_positive, len(gray_rows))
            + "  ·  "
            + source_rate("Vert", green_positive, len(green_rows))
        )
        if pair_count:
            for category in self.category_names:
                count = category_counts[category]
                self.rate_vars[category].set(f"{100.0 * count / pair_count:.1f} %")
                self.rate_count_vars[category].set(f"{count} / {pair_count} paire(s)")
            self.count_var.set(
                f"Paires comparables : {pair_count} · {defects} avec au moins une détection"
            )
        else:
            for category in self.category_names:
                self.rate_vars[category].set("— %")
                self.rate_count_vars[category].set("0 / 0 paire")
            self.count_var.set(
                f"Aucune paire comparable · {len(gray_rows)} image(s) grise(s) analysée(s) · "
                f"{len(green_rows)} verte(s) analysée(s)"
            )
        self.filter_var.set(f"{start_date} → {end_date} · toutes tailles de module")
        for item in self.table.get_children():
            self.table.delete(item)
        tags = {
            "Dossier 1 uniquement": "gray_only",
            "Présent dans les deux": "both",
            "Dossier 2 uniquement": "green_only",
            "À vérifier": "unknown",
        }
        for row, category in classified_rows[:500]:
            display_name, gray_state, green_state, _stored_category, comparable, error_text = row
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

    def open_diagnostics(self) -> None:
        if not self.latest_log_path or not self.latest_log_path.is_file():
            try:
                candidates = list(LOG_DIRECTORY.glob("analyse_*.log"))
                self.latest_log_path = max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None
            except OSError:
                self.latest_log_path = None

        window = Toplevel(self.root)
        window.title("Journal de diagnostic")
        window.geometry("760x540")
        window.minsize(600, 420)
        window.transient(self.root)

        ttk.Label(
            window,
            text="Le journal détaille les dossiers parcourus, les résultats image par image et les erreurs avec leur trace technique.",
            wraplength=720,
        ).pack(anchor="w", padx=12, pady=(12, 4))
        ttk.Label(
            window,
            text="Il contient les chemins des dossiers et numéros produit, mais aucune image. Vérifie ces informations avant de le transmettre.",
            wraplength=720, foreground="#555555",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        content = ttk.Frame(window, padding=(12, 0, 12, 0))
        content.pack(fill="both", expand=True)
        viewer = Text(content, wrap="none", height=20, state="normal")
        vertical = ttk.Scrollbar(content, orient="vertical", command=viewer.yview)
        horizontal = ttk.Scrollbar(content, orient="horizontal", command=viewer.xview)
        viewer.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        viewer.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        if self.latest_log_path and self.latest_log_path.is_file():
            try:
                lines = self.latest_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                viewer.insert("1.0", f"Journal : {self.latest_log_path}\n\n" + "\n".join(lines[-600:]))
            except OSError as exc:
                viewer.insert("1.0", f"Impossible de lire le journal : {_exception_description(exc)}")
        else:
            viewer.insert("1.0", "Aucun journal disponible. Lance une analyse, puis ouvre à nouveau cette fenêtre.")
        viewer.configure(state="disabled")

        buttons = ttk.Frame(window, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Exporter le journal…", command=self.export_log).pack(side="left")
        ttk.Button(buttons, text="Fermer", command=window.destroy).pack(side="right")

    def export_log(self) -> None:
        if not self.latest_log_path or not self.latest_log_path.is_file():
            messagebox.showinfo("Journal", "Aucun journal à exporter. Lance d’abord une analyse.", parent=self.root)
            return
        destination = filedialog.asksaveasfilename(
            title="Exporter le journal de diagnostic",
            defaultextension=".log",
            initialfile="journal_claquage.log",
            filetypes=(("Journal texte", "*.log"), ("Fichier texte", "*.txt")),
        )
        if not destination:
            return
        try:
            shutil.copyfile(self.latest_log_path, destination)
            self.status_var.set("Journal de diagnostic exporté")
            messagebox.showinfo("Journal exporté", f"Le journal a été enregistré ici :\n{destination}", parent=self.root)
        except OSError as exc:
            messagebox.showerror("Export impossible", _exception_description(exc), parent=self.root)

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
                    SELECT start_date, end_date, display_name, gray_state, green_state,
                           category, comparable, image_paths, error_text, scanned_at
                    FROM comparisons_intervals ORDER BY start_date DESC, end_date DESC, display_name COLLATE NOCASE
                """).fetchall()
            with open(destination, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream, delimiter=";")
                writer.writerow(("Début", "Fin", "Numéro produit", "Détecté en gris", "Détecté en vert", "Catégorie", "Paire comparable", "Chemins des images", "Erreur", "Dernière analyse"))
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
