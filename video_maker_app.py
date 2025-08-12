import sys
import os
import tempfile
import shutil
import asyncio
import atexit
import aiohttp
import subprocess
import logging
import random
import time
import glob
import string
import math
import uuid
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple, Dict

from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtGui import QFontDatabase
from PySide6.QtCore import QThread, Signal, QObject, QMutex

from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    concatenate_videoclips,
    CompositeVideoClip,
    ColorClip,
)
from moviepy.video.fx import all as vfx
from pydub import AudioSegment
from duckduckgo_search import ddg_images
from faster_whisper import WhisperModel
import imagehash
from PIL import Image, ImageDraw, ImageFont

try:
    import torch
    TORCH_AVAILABLE = True
except Exception:
    TORCH_AVAILABLE = False


class Constants:
    IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
    AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".flac")
    DEFAULT_FONT_SIZE = 50
    MIN_IMAGE_RESOLUTION = (600, 400)
    RESOLUTIONS = {
        "720p": (1280, 720),
        "1080p": (1920, 1080),
        "4K": (3840, 2160),
    }
    MAX_CONCURRENT_DOWNLOADS = 8
    MAX_CONCURRENT_VIDEOS = 3
    CACHE_EXPIRY_HOURS = 24
    MAX_CACHE_SIZE_MB = 500
    TRANSITION_DURATION = 0.6


class ProcessingState(Enum):
    IDLE = "idle"
    ANALYZING_AUDIO = "analyzing_audio"
    FETCHING_IMAGES = "fetching_images"
    PROCESSING_VIDEO = "processing_video"
    COMPLETE = "complete"
    ERROR = "error"


class TransitionType(Enum):
    NONE = "none"
    CROSSFADE = "crossfade"
    SLIDE_LEFT = "slide_left"
    SLIDE_RIGHT = "slide_right"
    LIGHT_LEAK = "light_leak"
    ZOOM = "zoom"
    FADE_TO_BLACK = "fade_to_black"


class MotionEffect(Enum):
    NONE = "none"
    SLOW_ZOOM = "slow_zoom"
    KEN_BURNS = "ken_burns"


@dataclass
class AudioFileSettings:
    audio_path: str
    seconds_per_image: float = 5.0
    model_size: str = "tiny"
    font_size: int = 50
    font_family: str = "Arial"
    resolution: str = "1080p"
    use_transitions: bool = False
    transition_type: TransitionType = TransitionType.NONE
    motion_effect: MotionEffect = MotionEffect.KEN_BURNS
    status: ProcessingState = ProcessingState.IDLE
    progress: int = 0
    output_path: str = ""
    is_processing: bool = False

    @property
    def display_name(self) -> str:
        return os.path.splitext(os.path.basename(self.audio_path))[0]

    @property
    def duration(self) -> float:
        return get_audio_duration(self.audio_path)


@dataclass
class VideoProject:
    project_id: str
    audio_settings: Dict[str, AudioFileSettings] = field(default_factory=dict)
    images: List[str] = field(default_factory=list)
    image_selection: Dict[str, bool] = field(default_factory=dict)
    output_folder: str = ""
    status: ProcessingState = ProcessingState.IDLE
    current_audio_selection: str = ""


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("video_maker.log"), logging.StreamHandler()],
)

TEMP_DIR = tempfile.mkdtemp(prefix="vm_enhanced_")
CACHE_DIR = os.path.join(TEMP_DIR, "transcript_cache")
RESIZED_DIR = os.path.join(TEMP_DIR, "resized_images")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(RESIZED_DIR, exist_ok=True)

atexit.register(lambda: shutil.rmtree(TEMP_DIR, ignore_errors=True))

DARK_THEME = """
QWidget { background-color: #2b2b2b; color: #ffffff; font-family: 'Segoe UI', Arial, sans-serif; font-size: 14px; }
QPushButton { background-color: #404040; color: white; border: 1px solid #555555; border-radius: 6px; padding: 8px 16px; font-weight: bold; }
QPushButton:hover { background-color: #505050; }
QPushButton:pressed { background-color: #353535; }
QPushButton:disabled { background-color: #2a2a2a; color: #666666; }
QProgressBar { background: #404040; color: white; border: 1px solid #555555; border-radius: 6px; height: 20px; text-align: center; }
QProgressBar::chunk { background-color: #0078d4; border-radius: 5px; }
QComboBox, QSpinBox, QCheckBox, QLineEdit { background-color: #404040; color: white; border: 1px solid #555555; border-radius: 4px; padding: 4px; }
QListWidget { background-color: #353535; border: 1px solid #555555; border-radius: 4px; }
QListWidget::item { padding: 8px; border-bottom: 1px solid #444; }
QListWidget::item:selected { background-color: #0078d4; }
QScrollArea { background-color: #353535; border: 1px solid #555555; }
QTabWidget::pane { border: 1px solid #555555; background-color: #2b2b2b; }
QTabBar::tab { background-color: #404040; color: white; border: 1px solid #555555; padding: 8px 16px; margin: 2px; }
QTabBar::tab:selected { background-color: #0078d4; }
"""

LIGHT_THEME = """
QWidget { background-color: #ffffff; color: #000000; font-family: 'Segoe UI', Arial, sans-serif; font-size: 14px; }
QPushButton { background-color: #f0f0f0; color: black; border: 1px solid #cccccc; border-radius: 6px; padding: 8px 16px; font-weight: bold; }
QPushButton:hover { background-color: #e0e0e0; }
QPushButton:pressed { background-color: #d0d0d0; }
QPushButton:disabled { background-color: #f5f5f5; color: #999999; }
QProgressBar { background: #f0f0f0; color: black; border: 1px solid #cccccc; border-radius: 6px; height: 20px; text-align: center; }
QProgressBar::chunk { background-color: #0078d4; border-radius: 5px; }
QComboBox, QSpinBox, QCheckBox, QLineEdit { background-color: #ffffff; color: black; border: 1px solid #cccccc; border-radius: 4px; padding: 4px; }
QListWidget { background-color: #fafafa; border: 1px solid #cccccc; border-radius: 4px; }
QListWidget::item { padding: 8px; border-bottom: 1px solid #eee; }
QListWidget::item:selected { background-color: #0078d4; color: white; }
QScrollArea { background-color: #fafafa; border: 1px solid #cccccc; }
QTabWidget::pane { border: 1px solid #cccccc; background-color: white; }
QTabBar::tab { background-color: #f0f0f0; color: black; border: 1px solid #cccccc; padding: 8px 16px; margin: 2px; }
QTabBar::tab:selected { background-color: #0078d4; color: white; }
"""


def detect_nvenc() -> bool:
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
        output = (result.stdout or "") + (result.stderr or "")
        return "h264_nvenc" in output
    except Exception:
        return False


def detect_cuda_available() -> bool:
    try:
        if TORCH_AVAILABLE:
            return torch.cuda.is_available()
    except Exception:
        pass
    return False


def sanitize_filename(filename: str) -> str:
    valid_chars = f"-_.() {string.ascii_letters}{string.digits}"
    sanitized = "".join(c for c in filename if c in valid_chars)
    return sanitized[:200]


def create_safe_output_path(output_folder: str, base_name: str, extension: str = ".mp4") -> str:
    safe_base = sanitize_filename(base_name) or "video"
    output_path = os.path.join(output_folder, f"final_video_{safe_base}{extension}")
    counter = 1
    while os.path.exists(output_path):
        output_path = os.path.join(output_folder, f"final_video_{safe_base}_{counter}{extension}")
        counter += 1
    return output_path


def get_audio_duration(audio_path: str) -> float:
    try:
        audio = AudioSegment.from_file(audio_path)
        return audio.duration_seconds
    except Exception as e:
        logging.error(f"Failed to get audio duration for {audio_path}: {e}")
        return 0.0


def calculate_required_images(audio_duration: float, seconds_per_image: float, use_transitions: bool) -> int:
    if seconds_per_image <= 0:
        seconds_per_image = 1.0
    effective = seconds_per_image - (Constants.TRANSITION_DURATION if use_transitions else 0.0)
    effective = max(1.0, effective)
    return max(1, math.ceil(audio_duration / effective))


def parse_search_terms(search_text: str) -> List[str]:
    terms = [term.strip() for term in search_text.split(',') if term.strip()]
    return terms if terms else ([search_text.strip()] if search_text.strip() else [])


def remove_duplicates(image_paths: List[str]) -> List[str]:
    seen_hashes = set()
    unique = []
    for p in image_paths:
        try:
            with Image.open(p) as img:
                h = imagehash.average_hash(img)
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    unique.append(p)
        except Exception as e:
            logging.warning(f"Failed to process image {p}: {e}")
            continue
    return unique


def is_low_quality(image_path: str, min_resolution: Tuple[int, int] = Constants.MIN_IMAGE_RESOLUTION) -> bool:
    try:
        with Image.open(image_path) as img:
            return img.width < min_resolution[0] or img.height < min_resolution[1]
    except Exception:
        return True


def pre_resize_letterbox(in_path: str, out_dir: str, final_size: Tuple[int, int]) -> str:
    try:
        os.makedirs(out_dir, exist_ok=True)
        with Image.open(in_path) as img:
            img = img.convert("RGB")
            in_w, in_h = img.size
            target_w, target_h = final_size
            scale = min(target_w / in_w, target_h / in_h)
            new_w = max(1, int(in_w * scale))
            new_h = max(1, int(in_h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
            bg = Image.new("RGB", final_size, (0, 0, 0))
            x = (target_w - new_w) // 2
            y = (target_h - new_h) // 2
            bg.paste(img, (x, y))
            base = os.path.splitext(os.path.basename(in_path))[0]
            out_path = os.path.join(out_dir, f"resized_{sanitize_filename(base)}_{uuid.uuid4().hex[:8]}.jpg")
            bg.save(out_path, format="JPEG", quality=95)
            return out_path
    except Exception as e:
        logging.error(f"Failed to pre-resize {in_path}: {e}")
        return in_path


def apply_motion(clip: ImageClip, duration: float, final_size: Tuple[int, int], effect: MotionEffect) -> ImageClip:
    if effect == MotionEffect.NONE:
        return clip.set_duration(duration)

    if effect == MotionEffect.SLOW_ZOOM:
        def zoom_func(t):
            return 1.0 + 0.05 * (t / max(0.001, duration))
        zoomed = clip.resize(zoom_func)
        return zoomed.set_duration(duration)

    if effect == MotionEffect.KEN_BURNS:
        def zoom_func(t):
            return 1.0 + 0.12 * (t / max(0.001, duration))
        W, H = final_size
        start_x = random.uniform(-0.05, 0.05)
        start_y = random.uniform(-0.05, 0.05)
        end_x = random.uniform(-0.05, 0.05)
        end_y = random.uniform(-0.05, 0.05)

        def pos_func(t):
            p = t / max(0.001, duration)
            x = int((start_x + (end_x - start_x) * p) * W)
            y = int((start_y + (end_y - start_y) * p) * H)
            return (x, y)
        zoomed = clip.resize(zoom_func).set_position(pos_func)
        return zoomed.set_duration(duration)

    return clip.set_duration(duration)


def build_transitioned_timeline(clips: List[ImageClip], transition: TransitionType, d: float, final_size: Tuple[int, int]):
    if transition == TransitionType.NONE or len(clips) <= 1 or d <= 0:
        return concatenate_videoclips(clips, method="compose")

    timeline = []
    current_end = 0.0
    W, H = final_size

    for idx, clip in enumerate(clips):
        if idx == 0:
            timeline.append(clip.set_start(0))
            current_end = clip.duration
            continue

        prev = timeline[-1]
        start_time = max(0.0, current_end - d)

        if transition == TransitionType.CROSSFADE:
            timeline[-1] = prev.crossfadeout(d)
            timeline.append(clip.crossfadein(d).set_start(start_time))

        elif transition in (TransitionType.SLIDE_LEFT, TransitionType.SLIDE_RIGHT):
            direction = -1 if transition == TransitionType.SLIDE_LEFT else 1
            def pos_out(t, _prev=prev):
                p = min(1.0, max(0.0, (t - (prev.start + prev.duration - d)) / d))
                return (int(direction * p * W), 0)
            def pos_in(t):
                p = min(1.0, max(0.0, (t - start_time) / d))
                return (int(-direction * (1 - p) * W), 0)
            timeline[-1] = prev.set_position(pos_out)
            timeline.append(clip.set_start(start_time).set_position(pos_in))

        elif transition == TransitionType.FADE_TO_BLACK:
            black = ColorClip(size=(W, H), color=(0, 0, 0), duration=d)
            timeline[-1] = prev.crossfadeout(d)
            black = black.set_start(current_end - d)
            timeline.append(black)
            timeline.append(clip.set_start(current_end))

        elif transition == TransitionType.ZOOM:
            timeline[-1] = prev.fx(vfx.resize, lambda t: 1.0 + 0.1 * (1 - min(1, max(0, (t - (prev.start + prev.duration - d)) / d))))
            timeline.append(clip.fx(vfx.resize, lambda t: 1.1 - 0.1 * min(1, max(0, (t - start_time) / d))).set_start(start_time))

        elif transition == TransitionType.LIGHT_LEAK:
            leak = ColorClip(size=(W, H), color=(255, 180, 80), duration=d).set_opacity(0.0)
            def leak_opacity(t):
                p = min(1.0, max(0.0, (t - start_time) / d))
                return 0.0 + 0.4 * (0.5 - abs(p - 0.5) * 1.0)
            leak = leak.set_start(start_time).set_opacity(leak_opacity)
            timeline[-1] = prev.crossfadeout(d)
            timeline.append(leak)
            timeline.append(clip.crossfadein(d).set_start(start_time))

        else:
            timeline.append(clip.set_start(current_end))

        current_end = max(current_end, start_time + clip.duration)

    return CompositeVideoClip(timeline, size=final_size)


class CacheManager:
    @staticmethod
    def clean_old_cache(max_age_hours: int = Constants.CACHE_EXPIRY_HOURS, max_size_mb: int = Constants.MAX_CACHE_SIZE_MB):
        try:
            now = time.time()
            cache_files = []
            for file_path in glob.glob(os.path.join(CACHE_DIR, "*.json")):
                try:
                    stat = os.stat(file_path)
                    if (now - stat.st_mtime) / 3600 > max_age_hours:
                        os.remove(file_path)
                    else:
                        cache_files.append((file_path, stat.st_size, stat.st_mtime))
                except OSError:
                    continue
            total_size = sum(size for _, size, _ in cache_files)
            max_size_bytes = max_size_mb * 1024 * 1024
            if total_size > max_size_bytes:
                cache_files.sort(key=lambda x: x[2])
                for file_path, size, _ in cache_files:
                    try:
                        os.remove(file_path)
                        total_size -= size
                        if total_size <= max_size_bytes:
                            break
                    except OSError:
                        continue
        except Exception as e:
            logging.error(f"Error cleaning cache: {e}")


class MultiSearchImageDownloader(QObject):
    progress_updated = Signal(int, int, str)
    download_completed = Signal(list, str)
    error_occurred = Signal(str)

    def __init__(self):
        super().__init__()
        self._should_stop = False

    def stop(self):
        self._should_stop = True

    @QtCore.Slot(list, str, int)
    def download_images(self, search_terms: List[str], temp_dir: str, required_count: int):
        try:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("Loop is closed")
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            try:
                downloaded_paths = loop.run_until_complete(
                    self._async_download_multi_search(search_terms, temp_dir, required_count)
                )
                if not self._should_stop:
                    self.download_completed.emit(downloaded_paths, ", ".join(search_terms))
            finally:
                if not loop.is_running():
                    loop.close()
        except Exception as e:
            self.error_occurred.emit(f"Multi-search download failed: {str(e)}")

    async def _search_ddg(self, term: str, count: int, loop: asyncio.AbstractEventLoop):
        return await loop.run_in_executor(None, lambda: ddg_images(term, max_results=count) or [])

    async def _async_download_multi_search(self, search_terms: List[str], temp_dir: str, required_count: int) -> List[str]:
        connector = aiohttp.TCPConnector(limit=Constants.MAX_CONCURRENT_DOWNLOADS)
        timeout = aiohttp.ClientTimeout(total=60)
        loop = asyncio.get_event_loop()

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            all_urls: List[str] = []
            images_per_term = max(1, required_count // max(1, len(search_terms)))
            extra = required_count % max(1, len(search_terms))

            for i, term in enumerate(search_terms):
                if self._should_stop:
                    break
                self.progress_updated.emit(i + 1, len(search_terms), f"Searching for: {term}")
                try:
                    term_count = images_per_term + (1 if i < extra else 0)
                    search_count = min(term_count * 4, 50)
                    results = await self._search_ddg(term, search_count, loop)
                    urls = [r.get("image") for r in results if r.get("image")]
                    all_urls.extend(urls[: term_count * 3])
                except Exception as e:
                    logging.warning(f"Failed to search for term '{term}': {e}")
                    continue

            if not all_urls:
                return []

            async def _download(url: str, idx: int):
                local_path = os.path.join(temp_dir, f"multi_img_{idx}_{uuid.uuid4().hex[:8]}.jpg")
                for attempt in range(3):
                    if self._should_stop:
                        return (local_path, False)
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                with open(local_path, "wb") as f:
                                    f.write(content)
                                return (local_path, True)
                    except Exception:
                        await asyncio.sleep(0.5)
                return (local_path, False)

            download_count = min(len(all_urls), required_count * 3)
            tasks = [_download(all_urls[i], i) for i in range(download_count)]
            self.progress_updated.emit(0, len(tasks), "Downloading images...")
            results = []
            completed = 0
            for coro in asyncio.as_completed(tasks):
                res = await coro
                results.append(res)
                completed += 1
                self.progress_updated.emit(completed, len(tasks), f"Downloaded {completed}/{len(tasks)} images")
                if self._should_stop:
                    break

            quality_paths = [p for (p, ok) in results if ok and os.path.exists(p) and not is_low_quality(p)]
            return quality_paths[:required_count]


class SubtitleGenerator:
    _model_cache: Dict[str, WhisperModel] = {}

    @staticmethod
    def _model_key(model_size: str, device: str) -> str:
        return f"{model_size}_{device}"

    @staticmethod
    def generate_subtitles(audio_path: str, video_clip, font_family: str = "Arial", font_size: int = 50, position: Tuple = ("center", "bottom"), model_size: str = "tiny"):
        device = "cuda" if detect_cuda_available() else "cpu"
        segments = SubtitleGenerator._transcribe_with_cache(audio_path, model_size, device)
        try:
            return SubtitleGenerator._create_subtitle_clips(segments, video_clip, font_family, font_size, position)
        except Exception as e:
            logging.error(f"Subtitle creation failed: {e}")
            return video_clip

    @staticmethod
    def _transcribe_with_cache(audio_path: str, model_size: str, device: str) -> List[Dict]:
        try:
            stat = os.stat(audio_path)
            cache_key = f"{os.path.basename(audio_path)}_{int(stat.st_mtime)}_{stat.st_size}_{model_size}.json"
        except Exception:
            cache_key = f"{os.path.basename(audio_path)}_{model_size}.json"
        cache_path = os.path.join(CACHE_DIR, cache_key)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        model = None
        key = SubtitleGenerator._model_key(model_size, device)
        if key in SubtitleGenerator._model_cache:
            model = SubtitleGenerator._model_cache[key]
        else:
            try:
                compute_type = "float16" if device == "cuda" else "int8"
                model = WhisperModel(model_size, device=device, compute_type=compute_type)
                SubtitleGenerator._model_cache[key] = model
            except Exception as e:
                logging.warning(f"Failed to load {model_size} model: {e}")
                model = WhisperModel("base", device="cpu", compute_type="int8")
                SubtitleGenerator._model_cache[SubtitleGenerator._model_key("base", "cpu")] = model

        segments: List[Dict] = []
        try:
            seg_gen, _ = model.transcribe(audio_path, beam_size=5)
            for s in seg_gen:
                segments.append({"start": float(s.start), "end": float(s.end), "text": s.text.strip()})
        except Exception as e:
            logging.error(f"Transcription failed: {e}")

        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(segments, f)
        except Exception:
            pass
        return segments

    @staticmethod
    def _create_subtitle_clips(segments: List[Dict], video_clip, font_family: str, font_size: int, position: Tuple):
        if not segments:
            return video_clip
        subtitle_clips = []
        for seg in segments:
            txt = seg.get("text", "").strip()
            if not txt:
                continue
            try:
                txt_clip = (vfx.mask_color(CompositeVideoClip([ImageClip(SubtileTextRenderer.render_text_image(txt, font_family, font_size, video_clip.w))])) if False else None)  # placeholder to keep import usage
                txt_clip = TextClipSafe.make_text_clip(txt, font_family, font_size, (int(video_clip.w * 0.88), None))
                txt_clip = txt_clip.set_start(seg["start"]).set_duration(max(0.05, seg["end"] - seg["start"]))
                txt_clip = txt_clip.set_position(position)
                subtitle_clips.append(txt_clip)
            except Exception as e:
                logging.warning(f"Failed to create subtitle clip: {e}")
                continue
        if subtitle_clips:
            return CompositeVideoClip([video_clip] + subtitle_clips, size=video_clip.size)
        return video_clip


class TextClipSafe:
    @staticmethod
    def make_text_clip(text: str, font_family: str, font_size: int, size: Tuple[Optional[int], Optional[int]]):
        try:
            from moviepy.editor import TextClip  # lazy import
            return TextClip(text, fontsize=int(font_size), font=font_family, color="white", stroke_color="black", stroke_width=3, method="caption", size=size)
        except Exception:
            img = SubtileTextRenderer.render_text_image(text, font_family, font_size, width=size[0] if size and size[0] else 1200)
            return ImageClip(img)


class SubtileTextRenderer:
    @staticmethod
    def render_text_image(text: str, font_family: str, font_size: int, width: int = 1200):
        try:
            font = ImageFont.truetype(SubtileTextRenderer._find_font_file(font_family), size=int(font_size))
        except Exception:
            font = ImageFont.load_default()
        padding = 20
        lines = []
        words = text.split()
        img_dummy = Image.new("RGB", (width, 10))
        draw_dummy = ImageDraw.Draw(img_dummy)
        line = ""
        for w in words:
            test = (line + " " + w).strip()
            if draw_dummy.textlength(test, font=font) + padding * 2 > width:
                lines.append(line)
                line = w
            else:
                line = test
        if line:
            lines.append(line)
        line_height = int(font_size * 1.4)
        height = padding * 2 + line_height * len(lines)
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # background box
        draw.rectangle([0, 0, width, height], fill=(0, 0, 0, 120))
        y = padding
        for ln in lines:
            draw.text((padding, y), ln, font=font, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
            y += line_height
        return img

    @staticmethod
    def _find_font_file(font_family: str) -> str:
        try:
            import matplotlib.font_manager as fm
            for f in fm.findSystemFonts(fontpaths=None, fontext='ttf'):
                try:
                    if font_family.lower() in os.path.basename(f).lower():
                        return f
                except Exception:
                    continue
        except Exception:
            pass
        return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


class ProcessingToken:
    def __init__(self):
        self._stop = False
        self._pause = False

    def stop(self):
        self._stop = True

    def pause(self):
        self._pause = True

    def resume(self):
        self._pause = False

    def check(self):
        if self._stop:
            raise RuntimeError("Processing stopped")
        while self._pause and not self._stop:
            time.sleep(0.1)


def create_video_from_images_safe(image_files: List[str], audio_settings: AudioFileSettings, output_path: str, codec: str = 'libx264', add_subs: bool = True, token: Optional[ProcessingToken] = None):
    clips: List[ImageClip] = []
    final_clip = None
    audio_clip = None

    def check():
        if token:
            token.check()

    try:
        resolution = Constants.RESOLUTIONS[audio_settings.resolution]
        check()

        required_images = calculate_required_images(audio_settings.duration, audio_settings.seconds_per_image, audio_settings.use_transitions)
        if not image_files:
            raise ValueError("No image files provided")
        padded = list(image_files)
        while len(padded) < required_images:
            padded.extend(random.sample(image_files, min(len(image_files), required_images - len(padded))))
        padded = padded[:required_images]

        resized_paths: List[str] = []
        for img in padded:
            check()
            if os.path.exists(img):
                rp = pre_resize_letterbox(img, RESIZED_DIR, resolution)
                resized_paths.append(rp)
        if not resized_paths:
            raise ValueError("No valid images after preprocessing")

        for rp in resized_paths:
            check()
            base_clip = ImageClip(rp)
            base_clip = base_clip.set_duration(audio_settings.seconds_per_image)
            motioned = apply_motion(base_clip, audio_settings.seconds_per_image, resolution, audio_settings.motion_effect)
            clips.append(motioned)

        if not clips:
            raise ValueError("No valid image clips created")

        d = Constants.TRANSITION_DURATION if audio_settings.use_transitions else 0.0
        if audio_settings.use_transitions and audio_settings.transition_type != TransitionType.NONE and len(clips) > 1:
            final_clip = build_transitioned_timeline(clips, audio_settings.transition_type, d, resolution)
        else:
            final_clip = concatenate_videoclips(clips, method="compose")

        audio_clip = AudioFileClip(audio_settings.audio_path)
        if final_clip.duration < audio_clip.duration - 0.05:
            final_clip = final_clip.fx(vfx.loop, duration=audio_clip.duration)
        final_clip = final_clip.set_audio(audio_clip).set_duration(audio_clip.duration)

        if add_subs:
            try:
                font_size_scaled = int(audio_settings.font_size * (resolution[1] / 1080))
                check()
                final_clip = SubtitleGenerator.generate_subtitles(audio_settings.audio_path, final_clip, font_family=audio_settings.font_family, font_size=font_size_scaled, model_size=audio_settings.model_size)
            except Exception as e:
                logging.warning(f"Failed to add subtitles: {e}")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        crf = "23" if resolution[1] <= 720 else "18" if resolution[1] <= 1080 else "15"
        ffmpeg_params = ["-preset", "fast", "-movflags", "+faststart", "-crf", crf]
        check()
        final_clip.write_videofile(output_path, codec=codec, audio_codec="aac", threads=min(6, os.cpu_count() or 1), verbose=False, logger=None, ffmpeg_params=ffmpeg_params)
        logging.info(f"Video created successfully: {output_path}")

    except Exception as e:
        logging.error(f"Failed to create video: {e}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        raise
    finally:
        for clip in clips:
            try:
                if hasattr(clip, 'close'):
                    clip.close()
            except Exception:
                pass
        for main in [final_clip, audio_clip]:
            try:
                if main is not None:
                    main.close()
            except Exception:
                pass


class IndividualVideoProcessor(QObject):
    progress_updated = Signal(str, int, str)
    processing_completed = Signal(str, str)
    processing_error = Signal(str, str)

    def __init__(self, project_id: str, audio_settings: AudioFileSettings, images: List[str], output_folder: str, semaphore: QtCore.QSemaphore):
        super().__init__()
        self.project_id = project_id
        self.audio_settings = audio_settings
        self.images = list(images)
        self.output_folder = output_folder
        self.token = ProcessingToken()
        self.semaphore = semaphore

    def stop(self):
        self.token.stop()

    def pause(self):
        self.token.pause()

    def resume(self):
        self.token.resume()

    @QtCore.Slot()
    def process_video(self):
        try:
            self.semaphore.acquire()
            codec = "h264_nvenc" if detect_nvenc() else "libx264"
            base_name = self.audio_settings.display_name
            output_path = create_safe_output_path(self.output_folder, base_name)
            self.audio_settings.output_path = output_path
            self.progress_updated.emit(self.audio_settings.audio_path, 10, "Starting video creation...")
            create_video_from_images_safe(image_files=self.images, audio_settings=self.audio_settings, output_path=output_path, codec=codec, add_subs=True, token=self.token)
            self.progress_updated.emit(self.audio_settings.audio_path, 100, "Completed!")
            self.processing_completed.emit(self.audio_settings.audio_path, output_path)
        except Exception as e:
            self.processing_error.emit(self.audio_settings.audio_path, f"Failed to process video: {str(e)}")
        finally:
            try:
                self.semaphore.release()
            except Exception:
                pass


class VideoMakerApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.is_dark_mode = True
        self._ui_mutex = QMutex()
        self.active_projects: Dict[str, VideoProject] = {}
        self.project_tabs: Dict[str, Dict] = {}
        self.project_downloaders: Dict[str, Tuple[QThread, MultiSearchImageDownloader]] = {}
        self.project_processors: Dict[str, Tuple[QThread, IndividualVideoProcessor]] = {}
        self.video_semaphore = QtCore.QSemaphore(Constants.MAX_CONCURRENT_VIDEOS)
        self._setup_ui()
        self._apply_theme()
        CacheManager.clean_old_cache()
        self._create_new_project()

    def _setup_ui(self):
        self.setWindowTitle("Enhanced Video Maker - Individual Audio Settings")
        self.setMinimumSize(1100, 820)
        self.resize(1500, 1000)
        main_layout = QtWidgets.QVBoxLayout(self)

        header_layout = QtWidgets.QHBoxLayout()
        title_label = QtWidgets.QLabel("Enhanced Video Maker - Individual Audio Settings")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; padding: 10px;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        self.theme_toggle = QtWidgets.QPushButton("🌙 Dark Mode")
        self.theme_toggle.clicked.connect(self._toggle_theme)
        self.theme_toggle.setFixedSize(120, 35)
        header_layout.addWidget(self.theme_toggle)
        self.btn_new_project = QtWidgets.QPushButton("🆕 New Project")
        self.btn_new_project.clicked.connect(self._create_new_project)
        self.btn_new_project.setFixedSize(120, 35)
        header_layout.addWidget(self.btn_new_project)
        main_layout.addLayout(header_layout)

        self.tab_widget = QtWidgets.QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_project_tab)
        main_layout.addWidget(self.tab_widget)

        self.global_status_label = QtWidgets.QLabel("Ready - Create a new project to begin")
        main_layout.addWidget(self.global_status_label)

    def _create_project_tab(self, project_id: str) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)

        audio_group = QtWidgets.QGroupBox("Audio Files")
        audio_layout = QtWidgets.QVBoxLayout(audio_group)
        btn_audio = QtWidgets.QPushButton("📁 Select Audio Files")
        btn_audio.clicked.connect(lambda: self._select_audio_files(project_id))
        audio_layout.addWidget(btn_audio)
        audio_list = QtWidgets.QListWidget()
        audio_list.setMinimumHeight(220)
        audio_list.currentItemChanged.connect(lambda: self._on_audio_selection_changed(project_id))
        audio_layout.addWidget(QtWidgets.QLabel("Select an audio file to configure its settings:"))
        audio_layout.addWidget(audio_list)
        left_layout.addWidget(audio_group)

        progress_group = QtWidgets.QGroupBox("Individual Progress")
        progress_layout = QtWidgets.QVBoxLayout(progress_group)
        progress_scroll = QtWidgets.QScrollArea(); progress_scroll.setWidgetResizable(True)
        progress_content = QtWidgets.QWidget(); progress_content_layout = QtWidgets.QVBoxLayout(progress_content)
        progress_scroll.setWidget(progress_content)
        progress_scroll.setMinimumHeight(180)
        progress_layout.addWidget(progress_scroll)
        left_layout.addWidget(progress_group)

        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)

        settings_group = QtWidgets.QGroupBox("Settings for Selected Audio File")
        settings_layout = QtWidgets.QGridLayout(settings_group)
        selected_audio_label = QtWidgets.QLabel("No audio file selected")
        selected_audio_label.setStyleSheet("font-weight: bold; color: #0078d4;")
        settings_layout.addWidget(selected_audio_label, 0, 0, 1, 4)

        settings_layout.addWidget(QtWidgets.QLabel("Seconds per image:"), 1, 0)
        slider_label = QtWidgets.QLabel("5")
        settings_layout.addWidget(slider_label, 1, 1)
        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider.setRange(2, 15); slider.setValue(5)
        slider.valueChanged.connect(lambda v: slider_label.setText(str(v)))
        slider.valueChanged.connect(lambda: self._update_audio_settings(project_id))
        settings_layout.addWidget(slider, 1, 2, 1, 2)

        settings_layout.addWidget(QtWidgets.QLabel("Resolution:"), 2, 0)
        resolution_combo = QtWidgets.QComboBox(); resolution_combo.addItems(["720p", "1080p", "4K"]); resolution_combo.setCurrentText("1080p")
        resolution_combo.currentTextChanged.connect(lambda: self._update_audio_settings(project_id))
        settings_layout.addWidget(resolution_combo, 2, 1, 1, 3)

        settings_layout.addWidget(QtWidgets.QLabel("Whisper model:"), 3, 0)
        model_combo = QtWidgets.QComboBox(); model_combo.addItems(["tiny", "base", "medium", "large"]); model_combo.setCurrentText("tiny")
        model_combo.currentTextChanged.connect(lambda: self._update_audio_settings(project_id))
        settings_layout.addWidget(model_combo, 3, 1)

        settings_layout.addWidget(QtWidgets.QLabel("Font size:"), 3, 2)
        font_size_spin = QtWidgets.QSpinBox(); font_size_spin.setRange(24, 120); font_size_spin.setValue(50)
        font_size_spin.valueChanged.connect(lambda: self._update_audio_settings(project_id))
        settings_layout.addWidget(font_size_spin, 3, 3)

        settings_layout.addWidget(QtWidgets.QLabel("Font family:"), 4, 0)
        font_combo = QtWidgets.QComboBox(); self._setup_font_combo(font_combo)
        font_combo.currentTextChanged.connect(lambda: self._update_audio_settings(project_id))
        settings_layout.addWidget(font_combo, 4, 1, 1, 3)

        use_transitions_checkbox = QtWidgets.QCheckBox("Enable Transitions")
        use_transitions_checkbox.stateChanged.connect(lambda: self._update_audio_settings(project_id))
        settings_layout.addWidget(use_transitions_checkbox, 5, 0)

        settings_layout.addWidget(QtWidgets.QLabel("Transition Type:"), 5, 1)
        transition_combo = QtWidgets.QComboBox(); transition_combo.addItems(["Crossfade", "Slide Left", "Slide Right", "Light Leak", "Zoom", "Fade to Black"])
        transition_combo.setEnabled(False)
        transition_combo.currentTextChanged.connect(lambda: self._update_audio_settings(project_id))
        settings_layout.addWidget(transition_combo, 5, 2, 1, 2)
        use_transitions_checkbox.stateChanged.connect(lambda s: transition_combo.setEnabled(s == QtCore.Qt.Checked))

        settings_layout.addWidget(QtWidgets.QLabel("Motion Effect:"), 6, 0)
        motion_combo = QtWidgets.QComboBox(); motion_combo.addItems(["Ken Burns", "Slow Zoom", "None"])
        motion_combo.currentTextChanged.connect(lambda: self._update_audio_settings(project_id))
        settings_layout.addWidget(motion_combo, 6, 1, 1, 3)

        right_layout.addWidget(settings_group)

        image_group = QtWidgets.QGroupBox("Multi-Search Image Search")
        image_layout = QtWidgets.QVBoxLayout(image_group)
        search_info = QtWidgets.QLabel("💡 Use commas to search multiple terms: 'nature, mountains, sunset'")
        search_info.setStyleSheet("font-style: italic; color: #888; font-size: 12px;")
        image_layout.addWidget(search_info)
        search_layout = QtWidgets.QHBoxLayout()
        search_box = QtWidgets.QLineEdit(); search_box.setPlaceholderText("Enter search terms separated by commas")
        search_layout.addWidget(search_box)
        btn_fetch = QtWidgets.QPushButton("🔍 Multi-Search Images")
        btn_fetch.clicked.connect(lambda: self._fetch_images(project_id))
        search_layout.addWidget(btn_fetch)
        image_layout.addLayout(search_layout)

        thumb_controls = QtWidgets.QHBoxLayout()
        btn_select_all = QtWidgets.QPushButton("Select All")
        btn_select_none = QtWidgets.QPushButton("Select None")
        btn_remove_unchecked = QtWidgets.QPushButton("Remove Unchecked")
        btn_regen_fill = QtWidgets.QPushButton("Regenerate to Fill")
        thumb_controls.addWidget(btn_select_all)
        thumb_controls.addWidget(btn_select_none)
        thumb_controls.addWidget(btn_remove_unchecked)
        thumb_controls.addWidget(btn_regen_fill)
        thumb_controls.addStretch()
        image_layout.addLayout(thumb_controls)

        scroll_area = QtWidgets.QScrollArea(); scroll_area.setMinimumHeight(180); scroll_area.setWidgetResizable(True)
        thumb_widget = QtWidgets.QWidget(); thumb_grid_layout = QtWidgets.QGridLayout(thumb_widget)
        scroll_area.setWidget(thumb_widget)
        image_layout.addWidget(scroll_area)

        btn_select_all.clicked.connect(lambda: self._set_all_image_selection(project_id, True))
        btn_select_none.clicked.connect(lambda: self._set_all_image_selection(project_id, False))
        btn_remove_unchecked.clicked.connect(lambda: self._remove_unchecked_images(project_id))
        btn_regen_fill.clicked.connect(lambda: self._regen_fill_images(project_id))

        right_layout.addWidget(image_group)

        output_group = QtWidgets.QGroupBox("Output & Actions")
        output_layout = QtWidgets.QVBoxLayout(output_group)
        output_folder_layout = QtWidgets.QHBoxLayout()
        lbl_output = QtWidgets.QLabel("Output folder: (not selected)")
        output_folder_layout.addWidget(lbl_output, 1)
        btn_output = QtWidgets.QPushButton("📂 Select Output Folder")
        btn_output.clicked.connect(lambda: self._select_output_folder(project_id))
        output_folder_layout.addWidget(btn_output)
        output_layout.addLayout(output_folder_layout)

        button_layout = QtWidgets.QHBoxLayout()
        btn_process_selected = QtWidgets.QPushButton("🎬 Process Selected Audio")
        btn_process_selected.clicked.connect(lambda: self._process_selected_audio(project_id))
        btn_process_selected.setEnabled(False)
        button_layout.addWidget(btn_process_selected)
        btn_process_all = QtWidgets.QPushButton("🎬 Process All Audio Files")
        btn_process_all.clicked.connect(lambda: self._process_all_audio(project_id))
        btn_process_all.setEnabled(False)
        button_layout.addWidget(btn_process_all)
        btn_stop_all = QtWidgets.QPushButton("⏹ Stop All Processing")
        btn_stop_all.clicked.connect(lambda: self._stop_all_processing(project_id))
        btn_stop_all.setEnabled(False)
        btn_stop_all.setVisible(False)
        button_layout.addWidget(btn_stop_all)
        output_layout.addLayout(button_layout)

        right_layout.addWidget(output_group)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([420, 740])
        layout.addWidget(splitter)

        self.project_tabs[project_id] = {
            'tab': tab,
            'audio_list': audio_list,
            'progress_content_layout': progress_content_layout,
            'selected_audio_label': selected_audio_label,
            'slider': slider,
            'slider_label': slider_label,
            'resolution_combo': resolution_combo,
            'model_combo': model_combo,
            'font_size_spin': font_size_spin,
            'font_combo': font_combo,
            'use_transitions_checkbox': use_transitions_checkbox,
            'transition_combo': transition_combo,
            'motion_combo': motion_combo,
            'search_box': search_box,
            'btn_fetch': btn_fetch,
            'scroll_area': scroll_area,
            'thumb_widget': thumb_widget,
            'thumb_grid_layout': thumb_grid_layout,
            'lbl_output': lbl_output,
            'btn_process_selected': btn_process_selected,
            'btn_process_all': btn_process_all,
            'btn_stop_all': btn_stop_all,
            'individual_progress_bars': {},
        }
        return tab

    def _setup_font_combo(self, font_combo: QtWidgets.QComboBox):
        font_db = QFontDatabase(); fonts = font_db.families()
        preferred = ["Arial", "Helvetica", "DejaVu Sans", "Liberation Sans", "Noto Sans", "Calibri", "Verdana", "Tahoma"]
        for font in preferred:
            if font in fonts:
                font_combo.addItem(font)
        for font in sorted(fonts):
            if font not in preferred and font_combo.findText(font) == -1:
                font_combo.addItem(font)
        if "Arial" in fonts:
            font_combo.setCurrentText("Arial")

    def _create_new_project(self):
        project_id = f"project_{uuid.uuid4().hex[:8]}"
        project = VideoProject(project_id=project_id)
        self.active_projects[project_id] = project
        tab = self._create_project_tab(project_id)
        tab_index = self.tab_widget.addTab(tab, f"Project {len(self.active_projects)}")
        self.tab_widget.setCurrentIndex(tab_index)
        self._update_global_status(f"Created new project: {project_id}")
        return project_id

    def _close_project_tab(self, tab_index):
        project_id = None
        for pid, tab_data in self.project_tabs.items():
            if self.tab_widget.widget(tab_index) == tab_data['tab']:
                project_id = pid
                break
        if not project_id:
            return
        self._stop_all_processing(project_id)
        if project_id in self.project_downloaders:
            thread, worker = self.project_downloaders[project_id]
            worker.stop(); thread.quit(); thread.wait(); del self.project_downloaders[project_id]
        self._cleanup_project_resources(project_id)
        self.active_projects.pop(project_id, None)
        self.project_tabs.pop(project_id, None)
        self.tab_widget.removeTab(tab_index)
        if self.tab_widget.count() == 0:
            self._create_new_project()

    def _cleanup_project_resources(self, project_id: str):
        try:
            for file_path in glob.glob(os.path.join(TEMP_DIR, f"*{project_id}*.*")):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Error during project cleanup: {e}")

    def _select_audio_files(self, project_id: str):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Select Audio Files", "", "Audio Files (*.mp3 *.wav *.m4a *.flac);;All Files (*)", options=QtWidgets.QFileDialog.DontUseNativeDialog)
        if not files:
            return
        project = self.active_projects[project_id]
        for audio_file in files:
            if audio_file not in project.audio_settings:
                project.audio_settings[audio_file] = AudioFileSettings(audio_path=audio_file)
        self._update_audio_list(project_id)
        self._update_buttons_state(project_id)
        self._update_global_status(f"Selected {len(files)} audio files for {project_id}")

    def _update_audio_list(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project:
            return
        tabs = self.project_tabs[project_id]; audio_list: QtWidgets.QListWidget = tabs['audio_list']
        audio_list.clear()
        for audio_path, settings in project.audio_settings.items():
            item = QtWidgets.QListWidgetItem()
            display_name = settings.display_name; duration = settings.duration
            status_text = ""
            if settings.is_processing:
                status_text = f" [Processing... {settings.progress}%]"
                item.setBackground(QtGui.QColor(0, 120, 212, 50))
            elif settings.status == ProcessingState.COMPLETE:
                status_text = " [Completed ✓]"; item.setBackground(QtGui.QColor(16, 124, 16, 50))
            elif settings.status == ProcessingState.ERROR:
                status_text = " [Error ✗]"; item.setBackground(QtGui.QColor(209, 52, 56, 50))
            item.setText(f"{display_name} ({duration:.1f}s){status_text}")
            item.setData(QtCore.Qt.UserRole, audio_path)
            audio_list.addItem(item)
        if audio_list.count() > 0 and not audio_list.currentItem():
            audio_list.setCurrentRow(0)

    def _on_audio_selection_changed(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project:
            return
        tabs = self.project_tabs[project_id]
        audio_list: QtWidgets.QListWidget = tabs['audio_list']
        current_item = audio_list.currentItem()
        if current_item:
            audio_path = current_item.data(QtCore.Qt.UserRole)
            project.current_audio_selection = audio_path
            settings = project.audio_settings.get(audio_path)
            if settings:
                self._load_settings_to_ui(project_id, settings)
                self._update_buttons_state(project_id)
        else:
            project.current_audio_selection = ""
            tabs['selected_audio_label'].setText("No audio file selected")
            self._update_buttons_state(project_id)

    def _load_settings_to_ui(self, project_id: str, settings: AudioFileSettings):
        tabs = self.project_tabs[project_id]
        tabs['selected_audio_label'].setText(f"Settings for: {settings.display_name} ({settings.duration:.1f}s)")
        tabs['slider'].blockSignals(True); tabs['slider'].setValue(int(settings.seconds_per_image)); tabs['slider_label'].setText(str(int(settings.seconds_per_image))); tabs['slider'].blockSignals(False)
        tabs['resolution_combo'].blockSignals(True); tabs['resolution_combo'].setCurrentText(settings.resolution); tabs['resolution_combo'].blockSignals(False)
        tabs['model_combo'].blockSignals(True); tabs['model_combo'].setCurrentText(settings.model_size); tabs['model_combo'].blockSignals(False)
        tabs['font_size_spin'].blockSignals(True); tabs['font_size_spin'].setValue(settings.font_size); tabs['font_size_spin'].blockSignals(False)
        tabs['font_combo'].blockSignals(True); tabs['font_combo'].setCurrentText(settings.font_family); tabs['font_combo'].blockSignals(False)
        tabs['use_transitions_checkbox'].blockSignals(True); tabs['use_transitions_checkbox'].setChecked(settings.use_transitions); tabs['use_transitions_checkbox'].blockSignals(False)
        transition_names = {TransitionType.CROSSFADE: "Crossfade", TransitionType.SLIDE_LEFT: "Slide Left", TransitionType.SLIDE_RIGHT: "Slide Right", TransitionType.LIGHT_LEAK: "Light Leak", TransitionType.ZOOM: "Zoom", TransitionType.FADE_TO_BLACK: "Fade to Black"}
        tabs['transition_combo'].blockSignals(True); tabs['transition_combo'].setCurrentText(transition_names.get(settings.transition_type, "Crossfade")); tabs['transition_combo'].setEnabled(settings.use_transitions); tabs['transition_combo'].blockSignals(False)
        motion_map_rev = {MotionEffect.KEN_BURNS: "Ken Burns", MotionEffect.SLOW_ZOOM: "Slow Zoom", MotionEffect.NONE: "None"}
        tabs['motion_combo'].blockSignals(True); tabs['motion_combo'].setCurrentText(motion_map_rev.get(settings.motion_effect, "Ken Burns")); tabs['motion_combo'].blockSignals(False)

    def _update_audio_settings(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project or not project.current_audio_selection:
            return
        settings = project.audio_settings.get(project.current_audio_selection)
        if not settings:
            return
        tabs = self.project_tabs[project_id]
        settings.seconds_per_image = float(tabs['slider'].value())
        settings.resolution = tabs['resolution_combo'].currentText()
        settings.model_size = tabs['model_combo'].currentText()
        settings.font_size = tabs['font_size_spin'].value()
        settings.font_family = tabs['font_combo'].currentText()
        settings.use_transitions = tabs['use_transitions_checkbox'].isChecked()
        transition_map = {"Crossfade": TransitionType.CROSSFADE, "Slide Left": TransitionType.SLIDE_LEFT, "Slide Right": TransitionType.SLIDE_RIGHT, "Light Leak": TransitionType.LIGHT_LEAK, "Zoom": TransitionType.ZOOM, "Fade to Black": TransitionType.FADE_TO_BLACK}
        settings.transition_type = transition_map.get(tabs['transition_combo'].currentText(), TransitionType.NONE)
        motion_map = {"Ken Burns": MotionEffect.KEN_BURNS, "Slow Zoom": MotionEffect.SLOW_ZOOM, "None": MotionEffect.NONE}
        settings.motion_effect = motion_map.get(tabs['motion_combo'].currentText(), MotionEffect.KEN_BURNS)
        self._update_audio_list(project_id)

    def _update_buttons_state(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project:
            return
        tabs = self.project_tabs[project_id]
        has_audio = bool(project.audio_settings)
        has_selected_audio = bool(project.current_audio_selection)
        has_images = any(project.image_selection.get(p, True) for p in project.images)
        has_output = bool(project.output_folder)
        any_processing = any(s.is_processing for s in project.audio_settings.values())
        tabs['btn_process_selected'].setEnabled(has_selected_audio and has_images and has_output and not any_processing)
        tabs['btn_process_all'].setEnabled(has_audio and has_images and has_output and not any_processing)
        tabs['btn_stop_all'].setEnabled(any_processing); tabs['btn_stop_all'].setVisible(any_processing)
        tabs['btn_fetch'].setEnabled(not any_processing)

    def _select_output_folder(self, project_id: str):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Output Folder", options=QtWidgets.QFileDialog.DontUseNativeDialog)
        if folder:
            self.active_projects[project_id].output_folder = folder
            tabs = self.project_tabs[project_id]
            tabs['lbl_output'].setText(f"Output folder: {folder}")
            self._update_buttons_state(project_id)
            self._update_global_status(f"Output folder set for {project_id}: {os.path.basename(folder)}")

    def _fetch_images(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project:
            return
        tabs = self.project_tabs[project_id]
        search_text = tabs['search_box'].text().strip()
        if not search_text:
            QtWidgets.QMessageBox.warning(self, "No Query", "Please enter search terms.")
            return
        if not project.audio_settings:
            QtWidgets.QMessageBox.warning(self, "Audio Required", "Please select audio files first to calculate image requirements.")
            return
        if hasattr(self, f'_downloading_{project_id}') and getattr(self, f'_downloading_{project_id}'):
            QtWidgets.QMessageBox.information(self, "Download in Progress", "Please wait for current download to complete.")
            return
        search_terms = parse_search_terms(search_text)
        max_duration = max(settings.duration for settings in project.audio_settings.values())
        required_count = max(20, calculate_required_images(max_duration, 5.0, True))
        tabs['btn_fetch'].setEnabled(False); tabs['btn_fetch'].setText("Multi-Searching...")
        self._clear_thumbnails(project_id)
        self._start_multi_search_download(project_id, search_terms, required_count)

    def _start_multi_search_download(self, project_id: str, search_terms: List[str], required_count: int):
        if project_id in self.project_downloaders:
            thread, worker = self.project_downloaders[project_id]
            worker.stop(); thread.quit(); thread.wait(); del self.project_downloaders[project_id]
        setattr(self, f'_downloading_{project_id}', True)
        thread = QThread(); worker = MultiSearchImageDownloader(); worker.moveToThread(thread)
        worker.progress_updated.connect(lambda current, total, message: self._update_global_status(f"[{project_id}] {message}"))
        worker.download_completed.connect(lambda paths, terms: self._on_download_completed(project_id, paths, terms))
        worker.error_occurred.connect(lambda error: self._on_download_error(project_id, error))
        thread.started.connect(lambda: worker.download_images(search_terms, TEMP_DIR, required_count))
        thread.finished.connect(worker.deleteLater); thread.finished.connect(thread.deleteLater)
        self.project_downloaders[project_id] = (thread, worker)
        thread.start()

    def _on_download_completed(self, project_id: str, downloaded_paths: List[str], search_terms: str):
        if hasattr(self, f'_downloading_{project_id}'):
            setattr(self, f'_downloading_{project_id}', False)
        if project_id in self.project_downloaders:
            del self.project_downloaders[project_id]
        if not downloaded_paths:
            QtWidgets.QMessageBox.warning(self, "Download Failed", f"No high-quality images could be downloaded for: {search_terms}")
            self._reset_fetch_buttons(project_id)
            return
        unique_paths = remove_duplicates(downloaded_paths)
        project = self.active_projects[project_id]
        for p in unique_paths:
            if p not in project.images:
                project.images.append(p)
                project.image_selection[p] = True
        self._display_thumbnails(project_id)
        self._reset_fetch_buttons(project_id)
        self._update_buttons_state(project_id)
        self._update_global_status(f"[{project_id}] ✓ Downloaded {len(unique_paths)} high-quality images")

    def _on_download_error(self, project_id: str, error_message: str):
        if hasattr(self, f'_downloading_{project_id}'):
            setattr(self, f'_downloading_{project_id}', False)
        if project_id in self.project_downloaders:
            del self.project_downloaders[project_id]
        self._reset_fetch_buttons(project_id)
        QtWidgets.QMessageBox.critical(self, "Download Error", error_message)

    def _clear_thumbnails(self, project_id: str):
        if project_id not in self.project_tabs:
            return
        layout = self.project_tabs[project_id]['thumb_grid_layout']
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _on_thumbnail_toggle(self, project_id: str, path: str, checked: bool):
        project = self.active_projects.get(project_id)
        if not project:
            return
        project.image_selection[path] = checked
        self._update_buttons_state(project_id)

    def _display_thumbnails(self, project_id: str):
        self._clear_thumbnails(project_id)
        project = self.active_projects.get(project_id)
        if not project or not project.images:
            return
        layout = self.project_tabs[project_id]['thumb_grid_layout']
        cols = 4
        for i, path in enumerate(project.images):
            try:
                container = QtWidgets.QWidget(); container_layout = QtWidgets.QVBoxLayout(container); container_layout.setContentsMargins(3, 3, 3, 3)
                label = QtWidgets.QLabel(); pixmap = QtGui.QPixmap(path).scaled(180, 110, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                label.setPixmap(pixmap); label.setAlignment(QtCore.Qt.AlignCenter); label.setStyleSheet("border: 1px solid #555; margin: 2px; padding: 2px;")
                checkbox = QtWidgets.QCheckBox("Use this image"); checkbox.setChecked(project.image_selection.get(path, True))
                checkbox.stateChanged.connect(lambda state, p=path: self._on_thumbnail_toggle(project_id, p, state == QtCore.Qt.Checked))
                container_layout.addWidget(label); container_layout.addWidget(checkbox)
                row = i // cols; col = i % cols
                layout.addWidget(container, row, col)
            except Exception as e:
                logging.warning(f"Failed to display thumbnail for {path}: {e}")

    def _reset_fetch_buttons(self, project_id: str):
        if project_id not in self.project_tabs:
            return
        tabs = self.project_tabs[project_id]
        tabs['btn_fetch'].setEnabled(True); tabs['btn_fetch'].setText("🔍 Multi-Search Images")

    def _validate_project_ready(self, project_id: str) -> bool:
        project = self.active_projects.get(project_id)
        if not project:
            return False
        errors = []
        if not project.audio_settings:
            errors.append("Please select at least one audio file")
        if not any(project.image_selection.get(p, True) for p in project.images):
            errors.append("Please fetch and select images first")
        if not project.output_folder:
            errors.append("Please select an output folder")
        elif not os.path.exists(project.output_folder):
            errors.append("Output folder does not exist")
        elif not os.access(project.output_folder, os.W_OK):
            errors.append("Output folder is not writable")
        if errors:
            QtWidgets.QMessageBox.warning(self, "Validation Failed", "\n".join(errors))
            return False
        return True

    def _gather_selected_images(self, project_id: str) -> List[str]:
        project = self.active_projects.get(project_id)
        if not project:
            return []
        return [p for p in project.images if project.image_selection.get(p, True)]

    def _process_selected_audio(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project or not project.current_audio_selection:
            QtWidgets.QMessageBox.warning(self, "No Selection", "Please select an audio file to process.")
            return
        if not self._validate_project_ready(project_id):
            return
        audio_path = project.current_audio_selection
        self._start_individual_processing(project_id, audio_path)

    def _process_all_audio(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project or not project.audio_settings:
            QtWidgets.QMessageBox.warning(self, "No Audio Files", "Please select audio files to process.")
            return
        if not self._validate_project_ready(project_id):
            return
        for audio_path in project.audio_settings.keys():
            if not project.audio_settings[audio_path].is_processing:
                self._start_individual_processing(project_id, audio_path)
        QtWidgets.QMessageBox.information(self, "Processing Started", "Started processing all audio files. Watch the individual progress bars below!")

    def _start_individual_processing(self, project_id: str, audio_path: str):
        project = self.active_projects.get(project_id)
        if not project:
            return
        settings = project.audio_settings.get(audio_path)
        if not settings or settings.is_processing:
            return
        settings.is_processing = True; settings.status = ProcessingState.PROCESSING_VIDEO; settings.progress = 0
        self._update_audio_list(project_id); self._update_buttons_state(project_id)
        self._create_individual_progress_bar(project_id, audio_path)
        selected_images = self._gather_selected_images(project_id)
        thread = QThread()
        processor = IndividualVideoProcessor(project_id, settings, selected_images, project.output_folder, self.video_semaphore)
        processor.moveToThread(thread)
        processor.progress_updated.connect(self._on_individual_progress)
        processor.processing_completed.connect(self._on_individual_completed)
        processor.processing_error.connect(self._on_individual_error)
        thread.started.connect(processor.process_video)
        thread.finished.connect(processor.deleteLater); thread.finished.connect(thread.deleteLater)
        processor_key = f"{project_id}_{audio_path}"
        self.project_processors[processor_key] = (thread, processor)
        thread.start()
        self._update_global_status(f"[{project_id}] Started processing: {settings.display_name}")

    def _create_individual_progress_bar(self, project_id: str, audio_path: str):
        tabs = self.project_tabs.get(project_id)
        if not tabs:
            return
        project = self.active_projects[project_id]
        settings = project.audio_settings[audio_path]
        progress_widget = QtWidgets.QWidget(); progress_layout = QtWidgets.QVBoxLayout(progress_widget); progress_layout.setContentsMargins(5, 5, 5, 5)
        name_label = QtWidgets.QLabel(f"🎵 {settings.display_name}"); name_label.setStyleSheet("font-weight: bold;")
        progress_layout.addWidget(name_label)
        info = f"{settings.resolution} | {settings.seconds_per_image}s/img | {'Transitions' if settings.use_transitions else 'No transitions'} | {settings.motion_effect.value}"
        info_label = QtWidgets.QLabel(info); info_label.setStyleSheet("font-size: 11px; color: #888;")
        progress_layout.addWidget(info_label)
        progress_bar = QtWidgets.QProgressBar(); progress_bar.setRange(0, 100); progress_bar.setValue(10)
        progress_layout.addWidget(progress_bar)
        status_label = QtWidgets.QLabel("Starting..."); status_label.setStyleSheet("font-size: 11px;")
        progress_layout.addWidget(status_label)
        button_layout = QtWidgets.QHBoxLayout()
        pause_btn = QtWidgets.QPushButton("⏸ Pause"); pause_btn.setFixedSize(70, 26)
        stop_btn = QtWidgets.QPushButton("⏹ Stop"); stop_btn.setFixedSize(70, 26)
        pause_btn.clicked.connect(lambda: self._pause_individual_processing(project_id, audio_path))
        stop_btn.clicked.connect(lambda: self._stop_individual_processing(project_id, audio_path))
        button_layout.addWidget(pause_btn); button_layout.addWidget(stop_btn); button_layout.addStretch()
        progress_layout.addLayout(button_layout)
        separator = QtWidgets.QFrame(); separator.setFrameShape(QtWidgets.QFrame.HLine); separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        progress_layout.addWidget(separator)
        tabs['progress_content_layout'].addWidget(progress_widget)
        tabs['individual_progress_bars'][audio_path] = {'widget': progress_widget, 'progress_bar': progress_bar, 'status_label': status_label, 'pause_btn': pause_btn, 'stop_btn': stop_btn}

    def _on_individual_progress(self, audio_path: str, progress: int, message: str):
        project_id = None
        for pid, project in self.active_projects.items():
            if audio_path in project.audio_settings:
                project_id = pid; break
        if not project_id:
            return
        settings = self.active_projects[project_id].audio_settings[audio_path]
        settings.progress = progress
        tabs = self.project_tabs.get(project_id)
        if tabs and audio_path in tabs['individual_progress_bars']:
            w = tabs['individual_progress_bars'][audio_path]
            w['progress_bar'].setValue(progress)
            w['status_label'].setText(message)
        self._update_audio_list(project_id)

    def _on_individual_completed(self, audio_path: str, output_path: str):
        project_id = None
        for pid, project in self.active_projects.items():
            if audio_path in project.audio_settings:
                project_id = pid; break
        if not project_id:
            return
        settings = self.active_projects[project_id].audio_settings[audio_path]
        settings.is_processing = False; settings.status = ProcessingState.COMPLETE; settings.progress = 100; settings.output_path = output_path
        tabs = self.project_tabs.get(project_id)
        if tabs and audio_path in tabs['individual_progress_bars']:
            w = tabs['individual_progress_bars'][audio_path]
            w['status_label'].setText(f"✓ Completed: {os.path.basename(output_path)}")
            w['pause_btn'].setEnabled(False); w['stop_btn'].setText("✓ Done"); w['stop_btn'].setEnabled(False)
        self._update_audio_list(project_id); self._update_buttons_state(project_id)
        processor_key = f"{project_id}_{audio_path}"
        if processor_key in self.project_processors:
            thread, processor = self.project_processors[processor_key]
            thread.quit(); thread.wait(); del self.project_processors[processor_key]
        self._update_global_status(f"[{project_id}] ✓ Completed: {settings.display_name}")
        project = self.active_projects[project_id]
        if not any(s.is_processing for s in project.audio_settings.values()):
            completed_count = sum(1 for s in project.audio_settings.values() if s.status == ProcessingState.COMPLETE)
            QtWidgets.QMessageBox.information(self, "Processing Complete", f"Project {project_id} processing complete!\n\n✓ {completed_count}/{len(project.audio_settings)} videos completed successfully\n\nOutput location: {project.output_folder}")

    def _on_individual_error(self, audio_path: str, error_message: str):
        project_id = None
        for pid, project in self.active_projects.items():
            if audio_path in project.audio_settings:
                project_id = pid; break
        if not project_id:
            return
        settings = self.active_projects[project_id].audio_settings[audio_path]
        settings.is_processing = False; settings.status = ProcessingState.ERROR
        tabs = self.project_tabs.get(project_id)
        if tabs and audio_path in tabs['individual_progress_bars']:
            w = tabs['individual_progress_bars'][audio_path]
            w['status_label'].setText(f"✗ Error: {error_message}")
            w['pause_btn'].setEnabled(False); w['stop_btn'].setText("✗ Error"); w['stop_btn'].setEnabled(False)
        self._update_audio_list(project_id); self._update_buttons_state(project_id)
        processor_key = f"{project_id}_{audio_path}"
        if processor_key in self.project_processors:
            thread, processor = self.project_processors[processor_key]
            thread.quit(); thread.wait(); del self.project_processors[processor_key]
        logging.error(f"Processing error for {settings.display_name}: {error_message}")
        self._update_global_status(f"[{project_id}] ✗ Error: {settings.display_name}")

    def _pause_individual_processing(self, project_id: str, audio_path: str):
        key = f"{project_id}_{audio_path}"
        if key in self.project_processors:
            thread, processor = self.project_processors[key]
            processor.pause()
            tabs = self.project_tabs.get(project_id)
            if tabs and audio_path in tabs['individual_progress_bars']:
                w = tabs['individual_progress_bars'][audio_path]
                w['pause_btn'].setText("▶ Resume")
                w['pause_btn'].clicked.disconnect()
                w['pause_btn'].clicked.connect(lambda: self._resume_individual_processing(project_id, audio_path))
                w['status_label'].setText("⏸ Paused")

    def _resume_individual_processing(self, project_id: str, audio_path: str):
        key = f"{project_id}_{audio_path}"
        if key in self.project_processors:
            thread, processor = self.project_processors[key]
            processor.resume()
            tabs = self.project_tabs.get(project_id)
            if tabs and audio_path in tabs['individual_progress_bars']:
                w = tabs['individual_progress_bars'][audio_path]
                w['pause_btn'].setText("⏸ Pause")
                w['pause_btn'].clicked.disconnect()
                w['pause_btn'].clicked.connect(lambda: self._pause_individual_processing(project_id, audio_path))
                w['status_label'].setText("▶ Resumed processing...")

    def _stop_individual_processing(self, project_id: str, audio_path: str):
        key = f"{project_id}_{audio_path}"
        if key in self.project_processors:
            thread, processor = self.project_processors[key]
            processor.stop(); thread.quit(); thread.wait(); del self.project_processors[key]
        settings = self.active_projects[project_id].audio_settings[audio_path]
        settings.is_processing = False; settings.status = ProcessingState.IDLE
        tabs = self.project_tabs.get(project_id)
        if tabs and audio_path in tabs['individual_progress_bars']:
            w = tabs['individual_progress_bars'][audio_path]
            w['status_label'].setText("⏹ Stopped by user")
            w['pause_btn'].setEnabled(False); w['stop_btn'].setEnabled(False)
        self._update_audio_list(project_id); self._update_buttons_state(project_id)
        self._update_global_status(f"[{project_id}] Stopped: {settings.display_name}")

    def _stop_all_processing(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project:
            return
        for audio_path in list(project.audio_settings.keys()):
            if project.audio_settings[audio_path].is_processing:
                self._stop_individual_processing(project_id, audio_path)
        self._update_global_status(f"[{project_id}] Stopped all processing")

    def _set_all_image_selection(self, project_id: str, value: bool):
        project = self.active_projects.get(project_id)
        if not project:
            return
        for p in project.images:
            project.image_selection[p] = value
        self._display_thumbnails(project_id)
        self._update_buttons_state(project_id)

    def _remove_unchecked_images(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project:
            return
        project.images = [p for p in project.images if project.image_selection.get(p, True)]
        project.image_selection = {p: True for p in project.images}
        self._display_thumbnails(project_id)
        self._update_buttons_state(project_id)

    def _regen_fill_images(self, project_id: str):
        project = self.active_projects.get(project_id)
        if not project or not project.audio_settings:
            return
        max_duration = max(settings.duration for settings in project.audio_settings.values())
        seconds_per_image = 5.0
        required_count = max(20, calculate_required_images(max_duration, seconds_per_image, True))
        current_selected = sum(1 for p in project.images if project.image_selection.get(p, True))
        needed = max(0, required_count - current_selected)
        if needed == 0:
            QtWidgets.QMessageBox.information(self, "No Regeneration Needed", "You already have enough selected images.")
            return
        tabs = self.project_tabs[project_id]
        terms = parse_search_terms(tabs['search_box'].text().strip())
        if not terms:
            QtWidgets.QMessageBox.warning(self, "No Query", "Enter search terms to regenerate images.")
            return
        self._start_multi_search_download(project_id, terms, needed)

    def _apply_theme(self):
        if self.is_dark_mode:
            self.setStyleSheet(DARK_THEME); self.theme_toggle.setText("☀️ Light Mode")
        else:
            self.setStyleSheet(LIGHT_THEME); self.theme_toggle.setText("🌙 Dark Mode")

    def _toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode; self._apply_theme()

    def _update_global_status(self, message: str):
        self.global_status_label.setText(message); logging.info(message)

    def closeEvent(self, event):
        for project_id in list(self.active_projects.keys()):
            self._stop_all_processing(project_id)
        for project_id in list(self.project_downloaders.keys()):
            thread, worker = self.project_downloaders[project_id]
            worker.stop(); thread.quit(); thread.wait()
        for project_id in list(self.active_projects.keys()):
            self._cleanup_project_resources(project_id)
        CacheManager.clean_old_cache()
        event.accept()


def main():
    try:
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Enhanced Video Maker")
    app.setApplicationVersion("3.1.0")
    window = VideoMakerApp(); window.show()
    def cleanup():
        try:
            CacheManager.clean_old_cache()
        except Exception:
            pass
    app.aboutToQuit.connect(cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()