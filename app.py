import os
import sys
import time
import sqlite3
import threading
import json
import csv
import queue
from collections import deque
from datetime import datetime, date, timedelta

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import Qt, QThread, Signal, QRect, QPoint, QTimer
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QFrame, QMessageBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QDialog, QScrollArea, QSlider, QLineEdit, QComboBox, QFileDialog, QInputDialog, QSizePolicy
)

CAMERA_INDEX = 0
CAMERA_SCAN_LIMIT = 6
DEFAULT_PHONE_URL = "http://192.168.137.226:8080/video"
FRAME_WIDTH, FRAME_HEIGHT = 640, 480
MIN_AREA = 1800
MIN_AREA_MIN = 300
MIN_AREA_MAX = 5000
CONFIRM_FRAMES = 3
CLEAR_FRAMES = 8
WARMUP_SECONDS = 5
MONITOR_AREA = (160, 120, 480, 360)
SAVE_MESSAGE_SECONDS = 3
PRE_RECORD_SECONDS = 5
POST_RECORD_SECONDS = 10
RECORD_FPS = 10
RECORD_INTERVAL = 1.0 / RECORD_FPS
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTURE_DIR = os.path.join(BASE_DIR, 'captures')
VIDEO_DIR = os.path.join(BASE_DIR, 'recordings')
DATABASE_PATH = os.path.join(BASE_DIR, 'alarms.db')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
DEVICE_NAMES_PATH = os.path.join(BASE_DIR, 'device_names.json')
os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)


def validate_monitor_area(value):
    """校验配置中的区域坐标，避免非法数据影响检测。"""
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) is not int for v in value)):
        return None
    x1, y1, x2, y2 = value
    if not (0 <= x1 < x2 <= FRAME_WIDTH and 0 <= y1 < y2 <= FRAME_HEIGHT):
        return None
    if x2 - x1 < 12 or y2 - y1 < 12:
        return None
    return tuple(value)


def validate_min_area(value):
    """有效运动面积阈值，数值越小越敏感。"""
    if type(value) is not int or not MIN_AREA_MIN <= value <= MIN_AREA_MAX:
        return None
    return value


def load_settings():
    """兼容旧版仅包含 monitor_area 的配置文件。"""
    if not os.path.exists(CONFIG_PATH):
        return MONITOR_AREA, MIN_AREA, CAMERA_INDEX, DEFAULT_PHONE_URL, None
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError('配置根节点必须为对象')
        area = validate_monitor_area(data.get('monitor_area'))
        if area is None:
            raise ValueError('监控区域坐标不合法')
        raw_min_area = data.get('min_area', MIN_AREA)
        min_area = validate_min_area(raw_min_area)
        if min_area is None:
            raise ValueError('运动检测面积阈值不合法')
        camera_index = data.get('camera_index', CAMERA_INDEX)
        if type(camera_index) is not int or not 0 <= camera_index < CAMERA_SCAN_LIMIT:
            camera_index = CAMERA_INDEX
        phone_url = str(data.get("phone_url", DEFAULT_PHONE_URL)).strip()
        source = data.get("camera_source", camera_index)
        if source != "phone" and (type(source) is not int or not 0 <= source < CAMERA_SCAN_LIMIT):
            source = camera_index
        return area, min_area, source, phone_url, None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return MONITOR_AREA, MIN_AREA, CAMERA_INDEX, DEFAULT_PHONE_URL, f'配置读取失败，已使用默认设置：{exc}'


def save_settings(area, min_area, camera_index=CAMERA_INDEX, phone_url=DEFAULT_PHONE_URL):
    """同时保存区域和灵敏度，防止更新一项时覆盖另一项。"""
    validated_area = validate_monitor_area(area)
    validated_min_area = validate_min_area(min_area)
    if validated_area is None or validated_min_area is None:
        raise ValueError('配置参数不合法')
    if camera_index != 'phone' and (type(camera_index) is not int or not 0 <= camera_index < CAMERA_SCAN_LIMIT):
        raise ValueError('摄像头编号不合法')
    if not isinstance(phone_url, str) or not phone_url.startswith(('http://', 'https://')):
        raise ValueError('手机视频流地址不合法')
    temp_path = CONFIG_PATH + '.tmp'
    try:
        with open(temp_path, 'w', encoding='utf-8') as file:
            json.dump({'monitor_area': list(validated_area),
                       'min_area': validated_min_area,
                        'camera_index': camera_index if isinstance(camera_index, int) else CAMERA_INDEX,
                        'camera_source': camera_index,
                        'phone_url': phone_url}, file,
                      ensure_ascii=False, indent=2)
            file.write('\n')
        os.replace(temp_path, CONFIG_PATH)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def load_device_names():
    """设备别名独立保存，不影响原有监控配置。"""
    try:
        with open(DEVICE_NAMES_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v)[:40] for k, v in data.items()
                    if isinstance(v, str) and v.strip()}
    except (OSError, ValueError, UnicodeError):
        pass
    return {}


def save_device_names(names):
    tmp = DEVICE_NAMES_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(names, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DEVICE_NAMES_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def get_font():
    for path in ('C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simhei.ttf'):
        if os.path.isfile(path):
            return ImageFont.truetype(path, 24)
    raise RuntimeError('找不到中文字体，请检查 Windows 字体目录')


def draw_chinese_text(frame, text, position, color, font):
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    ImageDraw.Draw(image).text(position, text, font=font, fill=color)
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def init_database():
    with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS alarm_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alarm_type TEXT NOT NULL,
            alarm_time TEXT NOT NULL,
            image_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '未处理'
        )''')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(alarm_records)')}
        if 'video_path' not in columns:
            conn.execute('ALTER TABLE alarm_records ADD COLUMN video_path TEXT DEFAULT NULL')


def save_alarm_screenshot(frame):
    filename = datetime.now().strftime('alarm_%Y%m%d_%H%M%S_%f.jpg')
    path = os.path.join(CAPTURE_DIR, filename)
    success, encoded = cv2.imencode('.jpg', frame)
    if not success:
        raise OSError('截图编码失败')
    encoded.tofile(path)
    return path


def save_alarm_record(image_path):
    with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
        cursor = conn.execute('''INSERT INTO alarm_records
            (alarm_type, alarm_time, image_path, status)
            VALUES (?, ?, ?, ?)''', (
            '区域入侵', datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            os.path.relpath(image_path, BASE_DIR), '未处理'
        ))
        return cursor.lastrowid


def intersects(box, area):
    x, y, w, h = box
    ax1, ay1, ax2, ay2 = area
    return x + w > ax1 and x < ax2 and y + h > ay1 and y < ay2


class SelectableVideoLabel(QLabel):
    """允许在按比例显示的视频上用鼠标选择矩形区域。"""
    area_selected = Signal(tuple)

    def __init__(self, text=''):
        super().__init__(text)
        self.selection_enabled = False
        self.drag_start = None
        self.drag_end = None

    def begin_selection(self):
        self.selection_enabled = True
        self.drag_start = None
        self.drag_end = None
        self.setCursor(Qt.CrossCursor)
        self.update()

    def cancel_selection(self):
        self.selection_enabled = False
        self.drag_start = None
        self.drag_end = None
        self.unsetCursor()
        self.update()

    def image_rect(self):
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        bounds = self.contentsRect()
        width, height = pixmap.width(), pixmap.height()
        return QRect(bounds.x() + (bounds.width() - width) // 2,
                     bounds.y() + (bounds.height() - height) // 2,
                     width, height)

    def mousePressEvent(self, event):
        rect = self.image_rect()
        if (self.selection_enabled and rect is not None
                and event.button() == Qt.LeftButton and rect.contains(event.position().toPoint())):
            self.drag_start = event.position().toPoint()
            self.drag_end = self.drag_start
            self.update()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.selection_enabled and self.drag_start is not None:
            rect = self.image_rect()
            if rect is not None:
                point = event.position().toPoint()
                self.drag_end = QPoint(
                    max(rect.left(), min(point.x(), rect.right())),
                    max(rect.top(), min(point.y(), rect.bottom())))
                self.update()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if not self.selection_enabled or self.drag_start is None:
            super().mouseReleaseEvent(event)
            return
        if event.button() != Qt.LeftButton:
            return
        rect = self.image_rect()
        if rect is None:
            self.cancel_selection()
            return
        end = event.position().toPoint()
        end = QPoint(max(rect.left(), min(end.x(), rect.right())),
                     max(rect.top(), min(end.y(), rect.bottom())))
        start = self.drag_start
        x1, x2 = sorted((start.x(), end.x()))
        y1, y2 = sorted((start.y(), end.y()))
        self.cancel_selection()
        if x2 - x1 < 12 or y2 - y1 < 12:
            return
        # 还原为原始 640x480 视频坐标，避免窗口大小影响检测区域。
        area = (round((x1 - rect.x()) * FRAME_WIDTH / rect.width()),
                round((y1 - rect.y()) * FRAME_HEIGHT / rect.height()),
                round((x2 - rect.x()) * FRAME_WIDTH / rect.width()),
                round((y2 - rect.y()) * FRAME_HEIGHT / rect.height()))
        self.area_selected.emit(area)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.selection_enabled and self.drag_start is not None and self.drag_end is not None:
            painter = QPainter(self)
            painter.setPen(QPen(QColor('#38bdf8'), 2, Qt.DashLine))
            painter.setBrush(QColor(56, 189, 248, 55))
            painter.drawRect(QRect(self.drag_start, self.drag_end).normalized())
            painter.end()


def open_camera(index):
    """Windows 本地设备使用 DirectShow，手机 MJPEG 视频流使用 FFmpeg。"""
    if isinstance(index, str):
        return cv2.VideoCapture(index, cv2.CAP_FFMPEG)
    if sys.platform == 'win32':
        return cv2.VideoCapture(index, cv2.CAP_DSHOW)
    return cv2.VideoCapture(index)


class EventVideoWriter(threading.Thread):
    """后台写入 MP4，避免视频编码阻塞监控采集线程。"""
    def __init__(self, video_path, record_ids, frames, callback):
        super().__init__(daemon=False)
        self.video_path = video_path
        self.record_ids = record_ids
        self.callback = callback
        self.frames = queue.Queue(maxsize=300)
        self.initial_frames = list(frames)
        self.failure = None

    def submit(self, frame):
        try:
            self.frames.put_nowait(frame.copy())
        except queue.Full:
            self.failure = '录像编码速度不足，部分视频帧被丢弃'

    def finish(self):
        self.frames.put(None)

    def run(self):
        writer = None
        success = False
        try:
            writer = cv2.VideoWriter(self.video_path,
                                     cv2.VideoWriter_fourcc(*'mp4v'),
                                     RECORD_FPS, (FRAME_WIDTH, FRAME_HEIGHT))
            if not writer.isOpened():
                raise OSError('MP4 编码器无法打开，请检查 OpenCV 视频编码支持')
            for frame in self.initial_frames:
                writer.write(frame)
            while True:
                frame = self.frames.get()
                if frame is None:
                    break
                writer.write(frame)
            success = True
        except Exception as exc:
            self.failure = str(exc)
        finally:
            if writer is not None:
                writer.release()
            if success and os.path.isfile(self.video_path) and os.path.getsize(self.video_path) > 0:
                try:
                    relative_path = os.path.relpath(self.video_path, BASE_DIR)
                    with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
                        conn.executemany('UPDATE alarm_records SET video_path=? WHERE id=?',
                                         [(relative_path, record_id) for record_id in self.record_ids])
                except (OSError, sqlite3.Error) as exc:
                    self.failure = f'录像已保存，但关联数据库失败：{exc}'
            else:
                try:
                    if os.path.exists(self.video_path):
                        os.remove(self.video_path)
                except OSError:
                    pass
            self.callback(self.failure)


class CameraThread(QThread):
    frame_ready = Signal(QImage)
    status_changed = Signal(str)
    camera_error = Signal(str)
    alarm_saved = Signal(int)
    video_saved = Signal(str)

    def __init__(self, camera_index=CAMERA_INDEX):
        super().__init__()
        self.camera_index = camera_index
        self.running = False
        self._area_lock = threading.Lock()
        self._monitor_area = MONITOR_AREA
        self._area_version = 0
        self._min_area = MIN_AREA

    def set_monitor_area(self, area):
        with self._area_lock:
            self._monitor_area = tuple(area)
            self._area_version += 1

    def get_monitor_area(self):
        with self._area_lock:
            return self._monitor_area, self._area_version

    def set_min_area(self, value):
        with self._area_lock:
            self._min_area = value

    def get_min_area(self):
        with self._area_lock:
            return self._min_area

    def stop(self):
        self.running = False

    def run(self):
        self.running = True
        cap = open_camera(self.camera_index)
        if not cap.isOpened():
            self.running = False
            self.camera_error.emit('视频源打开失败，请检查手机 IP Webcam 是否运行、网络是否正常。' if isinstance(self.camera_index, str) else f'摄像头 {self.camera_index} 打开失败，请检查设备连接或占用情况。')
            cap.release()
            return
        try:
            font = get_font()
            subtractor = cv2.createBackgroundSubtractorMOG2(
                history=500, varThreshold=50, detectShadows=True)
            small = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            large = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
            motion_frames = clear_frames = 0
            intrusion_frames = safe_frames = 0
            motion_confirmed = intrusion_confirmed = False
            previous_intrusion = False
            save_message_until = 0.0
            start_time = time.monotonic()
            last_area_version = -1
            pre_frames = deque(maxlen=PRE_RECORD_SECONDS * RECORD_FPS)
            recorder = None
            record_end_time = 0.0
            last_sample = 0.0
            pending_writers = []

            while self.running:
                ret, frame = cap.read()
                if not ret:
                    self.camera_error.emit('无法读取摄像头画面。')
                    break
                frame = cv2.flip(cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT)), 1)
                mask = subtractor.apply(frame)
                _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, small)
                mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, large)
                mask = cv2.dilate(mask, small, iterations=2)
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                               cv2.CHAIN_APPROX_SIMPLE)
                boxes = [cv2.boundingRect(c) for c in contours
                         if cv2.contourArea(c) >= self.get_min_area()]
                monitor_area, area_version = self.get_monitor_area()
                if area_version != last_area_version:
                    intrusion_frames = safe_frames = 0
                    intrusion_confirmed = False
                    previous_intrusion = False
                    last_area_version = area_version
                intrusion_detected = any(intersects(box, monitor_area) for box in boxes)
                warming_up = time.monotonic() - start_time < WARMUP_SECONDS

                if warming_up:
                    motion_frames = clear_frames = 0
                    intrusion_frames = safe_frames = 0
                    motion_confirmed = intrusion_confirmed = False
                else:
                    if boxes:
                        motion_frames += 1
                        clear_frames = 0
                        if motion_frames >= CONFIRM_FRAMES:
                            motion_confirmed = True
                    else:
                        clear_frames += 1
                        motion_frames = 0
                        if clear_frames >= CLEAR_FRAMES:
                            motion_confirmed = False
                    if intrusion_detected:
                        intrusion_frames += 1
                        safe_frames = 0
                        if intrusion_frames >= CONFIRM_FRAMES:
                            intrusion_confirmed = True
                    else:
                        safe_frames += 1
                        intrusion_frames = 0
                        if safe_frames >= CLEAR_FRAMES:
                            intrusion_confirmed = False

                ax1, ay1, ax2, ay2 = monitor_area
                cv2.rectangle(frame, (ax1, ay1), (ax2, ay2),
                              (0, 0, 255) if intrusion_confirmed else (0, 165, 255),
                              4 if intrusion_confirmed else 2)
                for box in boxes:
                    x, y, w, h = box
                    color = (0, 0, 255) if intersects(box, monitor_area) else (0, 255, 0)
                    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)

                if warming_up:
                    status, color = '正在初始化背景...', (255, 220, 80)
                elif intrusion_confirmed:
                    status, color = '警告：检测到区域入侵！', (255, 60, 60)
                elif motion_confirmed:
                    status, color = '检测到运动目标（区域外）', (255, 200, 50)
                else:
                    status, color = '监控区域正常', (0, 255, 100)
                frame = draw_chinese_text(frame, status, (20, 20), color, font)
                frame = draw_chinese_text(frame, '自定义监控区域',
                                          (ax1 + 5, ay1 + 5), (255, 220, 100), font)

                now = time.monotonic()
                # 固定约 10 帧/秒采样；缓存的是已绘制监控标注的画面。
                sampled = now - last_sample >= RECORD_INTERVAL
                if sampled:
                    last_sample = now
                    if recorder is None:
                        pre_frames.append(frame.copy())
                    else:
                        recorder.submit(frame)

                if intrusion_confirmed and not previous_intrusion:
                    try:
                        path = save_alarm_screenshot(frame.copy())
                        record_id = save_alarm_record(path)
                        self.alarm_saved.emit(record_id)
                        if recorder is None:
                            video_name = datetime.now().strftime('event_%Y%m%d_%H%M%S_%f.mp4')
                            video_path = os.path.join(VIDEO_DIR, video_name)
                            recorder = EventVideoWriter(
                                video_path, [record_id], pre_frames,
                                lambda err: self.video_saved.emit(
                                    f'录像处理提示：{err}' if err else '事件录像已保存，可在报警历史中查看'))
                            recorder.start()
                            pre_frames.clear()
                        else:
                            recorder.record_ids.append(record_id)
                        record_end_time = now + POST_RECORD_SECONDS
                        save_message_until = time.monotonic() + SAVE_MESSAGE_SECONDS
                    except (OSError, sqlite3.Error) as exc:
                        self.camera_error.emit(f'报警保存失败：{exc}')
                previous_intrusion = intrusion_confirmed
                if recorder is not None and intrusion_confirmed:
                    record_end_time = now + POST_RECORD_SECONDS
                if recorder is not None and now >= record_end_time:
                    recorder.finish()
                    pending_writers.append(recorder)
                    recorder = None
                    pre_frames.clear()
                pending_writers = [w for w in pending_writers if w.is_alive()]

                if time.monotonic() < save_message_until:
                    frame = draw_chinese_text(frame, '报警截图与记录已保存',
                                              (20, 60), (255, 220, 80), font)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                height, width, channels = rgb.shape
                image = QImage(rgb.data, width, height, width * channels,
                               QImage.Format_RGB888).copy()
                self.frame_ready.emit(image)
                self.status_changed.emit(status)
                self.msleep(30)
        except Exception as exc:
            self.camera_error.emit(f'监控线程异常：{exc}')
        finally:
            if 'recorder' in locals() and recorder is not None:
                recorder.finish()  # 手动停止或断线时保留已录制部分
                pending_writers.append(recorder)
            if 'pending_writers' in locals():
                for writer in pending_writers:
                    writer.join(timeout=30)
            cap.release()
            self.running = False


class ImagePreview(QDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle('报警截图预览')
        self.resize(850, 620)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap(path)
        if pixmap.isNull():
            label.setText('无法读取截图')
        else:
            label.setPixmap(pixmap)
        scroll.setWidget(label)
        layout.addWidget(scroll)


class AlarmChart(QWidget):
    """轻量 Qt 原生柱状图：无需 matplotlib 或其他新依赖。"""

    def __init__(self, title, labels, parent=None):
        super().__init__(parent)
        self.chart_title = title
        self.labels = list(labels)
        self.values = [0] * len(self.labels)
        self.setMinimumHeight(174)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_values(self, values):
        self.values = [max(0, int(x)) for x in values]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor('#e2e8f0'))
        painter.drawText(QRect(16, 10, self.width()-32, 26),
                         Qt.AlignLeft | Qt.AlignVCenter, self.chart_title)
        left, right, top, bottom = 36, 16, 48, 28
        width = max(1, self.width()-left-right)
        height = max(1, self.height()-top-bottom)
        maximum = max(1, *(self.values or [0]))
        painter.setPen(QPen(QColor('#31445e'), 1))
        for i in range(3):
            y = top + round(height*i/2)
            painter.drawLine(left, y, left+width, y)
        count = max(1, len(self.labels))
        slot = width / count
        bar_width = max(5, min(34, int(slot*0.55)))
        for i, (label, value) in enumerate(zip(self.labels, self.values)):
            x = round(left+slot*(i+0.5))
            bar_height = round(value/maximum * max(1, height-18))
            if value:
                painter.fillRect(QRect(x-bar_width//2, top+height-bar_height,
                                       bar_width, bar_height), QColor('#3b82f6'))
            painter.setPen(QColor('#cbd5e1'))
            painter.drawText(QRect(int(x-slot/2), top+height+4, int(slot), 20),
                             Qt.AlignCenter, str(label))
            if value:
                painter.drawText(QRect(int(x-slot/2), max(top, top+height-bar_height-20),
                                       int(slot), 18), Qt.AlignCenter, str(value))
        painter.end()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        init_database()
        self.camera_thread = None
        self.current_image = None
        self.monitor_area, self.min_area, self.camera_index, self.phone_url, self.config_warning = load_settings()
        self.available_cameras = []
        self.device_names = load_device_names()
        self.device_error = False
        self._switching_camera = False
        self.setWindowTitle('OpenCV 智能视觉监控系统')
        self.resize(1240, 840)
        self.init_ui()
        self.load_records()
        QTimer.singleShot(0, self.refresh_cameras)
        if self.config_warning:
            QMessageBox.warning(self, '区域配置提示', self.config_warning)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 26, 16, 20)
        side.setSpacing(12)
        brand = QLabel('◉  智能视觉监控')
        brand.setObjectName('brand')
        side.addWidget(brand)
        self.dashboard_nav = QPushButton('▦   系统概览')
        self.monitor_nav = QPushButton('◉   实时监控')
        self.history_nav = QPushButton('▤   报警历史')
        for button in (self.dashboard_nav, self.monitor_nav, self.history_nav):
            button.setObjectName('navButton')
            side.addWidget(button)
        side.addStretch()
        side.addWidget(QLabel('OpenCV  /  PySide6  /  SQLite'))
        root.addWidget(sidebar)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.dashboard_nav.clicked.connect(lambda: self.show_page(0))
        self.monitor_nav.clicked.connect(lambda: self.show_page(1))
        self.history_nav.clicked.connect(lambda: self.show_page(2))

        self.build_dashboard()

        monitor = QWidget()
        monitor_layout = QVBoxLayout(monitor)
        monitor_layout.setContentsMargins(28, 24, 28, 24)
        monitor_layout.setSpacing(18)
        monitor_layout.addWidget(self.heading('实时监控'))
        monitor_layout.addWidget(self.subtitle('运动目标检测  /  自定义区域入侵报警  /  自动截图'))
        video_card = QFrame()
        video_card.setObjectName('card')
        video_layout = QVBoxLayout(video_card)
        self.video_label = SelectableVideoLabel('摄像头未启动')
        self.video_label.area_selected.connect(self.apply_monitor_area)
        self.video_label.setObjectName('video')
        self.video_label.setMinimumSize(500, 400)
        self.video_label.setAlignment(Qt.AlignCenter)
        video_layout.addWidget(self.video_label)
        monitor_layout.addWidget(video_card, 1)
        self.status_label = QLabel('●  当前状态：未启动')
        self.status_label.setObjectName('monitorStatus')
        monitor_layout.addWidget(self.status_label)
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel('监控设备'))
        self.camera_combo = QComboBox()
        self.camera_combo.setMinimumWidth(240)
        self.camera_combo.addItem(self.device_title(self.camera_index), self.camera_index)
        self.camera_combo.currentIndexChanged.connect(self.change_camera)
        self.scan_camera_button = QPushButton('扫描摄像头')
        self.scan_camera_button.clicked.connect(self.refresh_cameras)
        device_row.addWidget(self.camera_combo, 1)
        device_row.addWidget(self.scan_camera_button)
        self.rename_camera_button = QPushButton('重命名设备')
        self.rename_camera_button.clicked.connect(self.rename_camera)
        device_row.addWidget(self.rename_camera_button)
        phone_row = QHBoxLayout()
        self.phone_url_input = QLineEdit(self.phone_url)
        self.phone_url_input.setPlaceholderText('手机视频地址，例如 http://192.168.137.226:8080/video')
        self.phone_url_button = QPushButton('保存手机地址')
        self.phone_url_button.clicked.connect(self.save_phone_url)
        phone_row.addWidget(QLabel('手机视频流'))
        phone_row.addWidget(self.phone_url_input, 1)
        phone_row.addWidget(self.phone_url_button)
        monitor_layout.addLayout(device_row)
        monitor_layout.addLayout(phone_row)
        device_status_row = QHBoxLayout()
        self.device_status_label = QLabel('设备状态：未连接')
        self.device_status_label.setObjectName('pageSubtitle')
        self.reconnect_button = QPushButton('重新连接')
        self.reconnect_button.clicked.connect(self.reconnect_camera)
        device_status_row.addWidget(self.device_status_label, 1)
        device_status_row.addWidget(self.reconnect_button)
        monitor_layout.addLayout(device_status_row)
        controls = QHBoxLayout()
        self.start_button = QPushButton('启动监控')
        self.stop_button = QPushButton('停止监控')
        self.select_area_button = QPushButton('设置监控区域')
        self.reset_area_button = QPushButton('重置区域')
        self.stop_button.setObjectName('stopButton')
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_camera)
        self.stop_button.clicked.connect(self.stop_camera)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.select_area_button)
        controls.addWidget(self.reset_area_button)
        self.select_area_button.clicked.connect(self.start_area_selection)
        self.reset_area_button.clicked.connect(self.reset_monitor_area)
        self.select_area_button.setEnabled(False)
        controls.addStretch()
        monitor_layout.addLayout(controls)
        self.area_info = QLabel(self.area_description())
        self.area_info.setObjectName('pageSubtitle')
        monitor_layout.addWidget(self.area_info)

        sensitivity_row = QHBoxLayout()
        sensitivity_title = QLabel('报警灵敏度')
        sensitivity_title.setObjectName('pageSubtitle')
        self.sensitivity_slider = QSlider(Qt.Horizontal)
        # 左侧低灵敏度(大面积阈值)，右侧高灵敏度(小面积阈值)。
        self.sensitivity_slider.setRange(MIN_AREA_MIN, MIN_AREA_MAX)
        self.sensitivity_slider.setSingleStep(100)
        self.sensitivity_slider.setPageStep(300)
        self.sensitivity_slider.setValue(MIN_AREA_MAX + MIN_AREA_MIN - self.min_area)
        self.sensitivity_value = QLabel('')
        self.sensitivity_value.setObjectName('pageSubtitle')
        self.sensitivity_value.setMinimumWidth(165)
        self.sensitivity_slider.valueChanged.connect(self.update_sensitivity)
        sensitivity_row.addWidget(sensitivity_title)
        sensitivity_row.addWidget(QLabel('低'))
        sensitivity_row.addWidget(self.sensitivity_slider, 1)
        sensitivity_row.addWidget(QLabel('高'))
        sensitivity_row.addWidget(self.sensitivity_value)
        monitor_layout.addLayout(sensitivity_row)
        self.update_sensitivity_label()
        self.pages.addWidget(monitor)

        history = QWidget()
        history_layout = QVBoxLayout(history)
        history_layout.setContentsMargins(28, 24, 28, 24)
        history_layout.setSpacing(16)
        history_layout.addWidget(self.heading('报警历史'))
        history_layout.addWidget(self.subtitle('查看报警事件  /  预览截图与录像  /  更新处理状态'))
        stats = QHBoxLayout()
        self.total_value = self.make_stat_card(stats, '累计报警')
        self.today_value = self.make_stat_card(stats, '今日报警')
        self.pending_value = self.make_stat_card(stats, '未处理报警')
        history_layout.addLayout(stats)
        actions = QHBoxLayout()
        refresh = QPushButton('刷新记录')
        preview = QPushButton('查看报警截图')
        video_preview = QPushButton('查看事件录像')
        processed = QPushButton('标记为已处理')
        export_button = QPushButton('导出记录')
        refresh.clicked.connect(self.load_records)
        preview.clicked.connect(self.preview_image)
        video_preview.clicked.connect(self.preview_video)
        processed.clicked.connect(self.mark_processed)
        export_button.clicked.connect(self.export_records)
        for button in (refresh, preview, video_preview, processed, export_button):
            actions.addWidget(button)
        actions.addStretch()
        history_layout.addLayout(actions)

        # 报警记录查询条件：关键词、处理状态、日期范围。
        filters = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('搜索报警编号或报警类型')
        self.search_input.setClearButtonEnabled(True)
        self.status_filter = QComboBox()
        self.status_filter.addItems(['全部状态', '未处理', '已处理'])
        self.date_filter = QComboBox()
        self.date_filter.addItems(['全部日期', '今天', '最近7天'])
        self.search_input.textChanged.connect(self.filter_records)
        self.status_filter.currentIndexChanged.connect(self.filter_records)
        self.date_filter.currentIndexChanged.connect(self.filter_records)
        filters.addWidget(self.search_input, 2)
        filters.addWidget(self.status_filter, 1)
        filters.addWidget(self.date_filter, 1)
        history_layout.addLayout(filters)
        self.filter_count = QLabel('显示 0 条记录')
        self.filter_count.setObjectName('pageSubtitle')
        history_layout.addWidget(self.filter_count)
        self.all_records = []
        self.filtered_records = []
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ['报警编号', '报警类型', '报警时间', '截图路径', '处理状态', '事件录像'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.preview_image)
        history_layout.addWidget(self.table, 1)
        self.pages.addWidget(history)

        self.setStyleSheet('''
            QMainWindow, QStackedWidget, QWidget {
                background: #0b1220; color: #e2e8f0;
                font-family: "Microsoft YaHei UI"; font-size: 13px;
            }
            /* QLabel 默认透明，避免全局 QWidget 背景规则产生黑色文字底块 */
            QLabel { background: transparent; }
            QFrame#sidebar { background: #111c30; border-right: 1px solid #26354c; }
            QFrame#sidebar QLabel { background: transparent; color: #8fa4bf; }
            QLabel#brand { color: #f8fafc; font-size: 19px; font-weight: bold;
                padding-bottom: 24px; }
            QPushButton#navButton { background: transparent; color: #a9bdd4;
                text-align: left; padding: 15px; border-radius: 9px;
                font-weight: bold; }
            QPushButton#navButton:hover { background: #1c2d46; color: white; }
            QPushButton#navButton:checked { background: #1d4ed8; color: white; }
            QLabel#pageTitle { color: #f8fafc; font-size: 27px; font-weight: bold; }
            QLabel#pageSubtitle { color: #91a6c1; font-size: 13px; }
            QFrame#card { background: #142238; border: 1px solid #263b55;
                border-radius: 12px; }
            QLabel#video { background: #050b14; color: #8498b2;
                font-size: 19px; border-radius: 9px; }
            QLabel#monitorStatus { background: #142238; border: 1px solid #263b55;
                border-radius: 8px; padding: 12px; color: #93c5fd;
                font-size: 15px; font-weight: bold; }
            QLabel#sectionTitle { color: #e2e8f0; font-size: 18px; font-weight: bold; }
            QLabel#detailName { color: #9fb1c8; font-size: 14px; }
            QLabel#detailValue { color: #93c5fd; font-size: 14px; font-weight: bold; }
            QLabel#statName { color: #9fb1c8; font-size: 14px; }
            QLabel#statValue { color: #60a5fa; font-size: 31px; font-weight: bold; }
            QScrollArea#dashboardScroll, QWidget#dashboardContent {
                background: #0b1220; border: none;
            }
            QPushButton { background: #2563eb; color: white; padding: 11px 19px;
                border: none; border-radius: 8px; font-weight: bold; }
            QPushButton:hover { background: #1d4ed8; }
            QPushButton:disabled { background: #34465f; color: #94a3b8; }
            QPushButton#stopButton { background: #334155; }
            QPushButton#stopButton:hover { background: #475569; }
            QPushButton#stopButton:disabled { background: #26354a; color: #64748b; }
            QTableWidget { background: #142238; alternate-background-color: #192940;
                color: #e2e8f0; border: 1px solid #263b55;
                border-radius: 9px; selection-background-color: #1e40af;
                selection-color: #ffffff; outline: none; }
            QHeaderView::section { background: #20334d; color: #d6e5f6;
                border: none; border-bottom: 1px solid #344a65;
                padding: 12px; font-weight: bold; }
            QTableCornerButton::section { background: #20334d; border: none; }
            QStatusBar { background: #101c2d; color: #9fb1c8; }
            QScrollBar:vertical { background: #142238; width: 11px; }
            QScrollBar::handle:vertical { background: #405776; border-radius: 5px; }
            QLineEdit, QComboBox { background: #142238; color: #e2e8f0;
                border: 1px solid #344a65; border-radius: 7px; padding: 9px; }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #60a5fa; }
            QComboBox QAbstractItemView { background: #142238; color: #e2e8f0;
                selection-background-color: #1e40af; }
            QSlider::groove:horizontal { background: #34465f; height: 7px; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #2563eb; border-radius: 3px; }
            QSlider::handle:horizontal { background: #93c5fd; width: 17px;
                margin: -6px 0; border-radius: 8px; }
        ''')
        self.show_page(0)

    def build_dashboard(self):
        # 使用独立滚动容器，窗口较小时仍能访问完整概览内容。
        dashboard_scroll = QScrollArea()
        dashboard_scroll.setWidgetResizable(True)
        dashboard_scroll.setFrameShape(QFrame.NoFrame)
        dashboard_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        dashboard_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        dashboard_scroll.setObjectName('dashboardScroll')

        dashboard = QWidget()
        dashboard.setObjectName('dashboardContent')
        dashboard.setMinimumWidth(740)
        layout = QVBoxLayout(dashboard)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(self.heading('系统概览'))
        layout.addWidget(self.subtitle('设备状态  /  报警统计  /  最近事件'))

        top = QHBoxLayout()
        top.setSpacing(12)
        self.dashboard_total = self.make_stat_card(top, '累计报警')
        self.dashboard_today = self.make_stat_card(top, '今日报警')
        self.dashboard_pending = self.make_stat_card(top, '未处理报警')
        self.dashboard_processed = self.make_stat_card(top, '已处理报警')
        layout.addLayout(top)

        device_card = QFrame()
        device_card.setObjectName('card')
        device_layout = QVBoxLayout(device_card)
        device_layout.setContentsMargins(22, 18, 22, 18)
        device_layout.setSpacing(12)
        device_title = QLabel('设备与系统状态')
        device_title.setObjectName('sectionTitle')
        device_layout.addWidget(device_title)
        self.dashboard_camera = QLabel('未启动')
        self.dashboard_detection = QLabel('待启动')
        self.dashboard_database = QLabel('正常')
        for name, value in (
            ('摄像头状态', self.dashboard_camera),
            ('入侵检测', self.dashboard_detection),
            ('数据库连接', self.dashboard_database)
        ):
            line = QHBoxLayout()
            label = QLabel(name)
            label.setObjectName('detailName')
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value.setObjectName('detailValue')
            line.addWidget(label)
            line.addStretch()
            line.addWidget(value)
            device_layout.addLayout(line)
        layout.addWidget(device_card)

        charts_row = QHBoxLayout()
        charts_row.setSpacing(12)
        self.week_chart = AlarmChart('最近 7 天报警趋势', [])
        self.hour_chart = AlarmChart('报警时段分布（每 4 小时）',
                                     ['00-04', '04-08', '08-12', '12-16', '16-20', '20-24'])
        for chart in (self.week_chart, self.hour_chart):
            card = QFrame()
            card.setObjectName('card')
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(8, 8, 8, 8)
            card_layout.addWidget(chart)
            charts_row.addWidget(card, 1)
        layout.addLayout(charts_row)
        for chart in (self.week_chart, self.hour_chart):
            chart.setMinimumHeight(210)
            chart.setMaximumHeight(260)

        self.process_summary = QLabel('处理情况：已处理 0（0%） / 未处理 0（0%）')
        self.process_summary.setObjectName('pageSubtitle')
        layout.addWidget(self.process_summary)

        recent_title = QLabel('最近报警事件')
        recent_title.setObjectName('sectionTitle')
        layout.addWidget(recent_title)
        self.recent_table = QTableWidget(0, 3)
        self.recent_table.setHorizontalHeaderLabels(['报警时间', '报警类型', '处理状态'])
        self.recent_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.setAlternatingRowColors(True)
        self.recent_table.setShowGrid(False)
        self.recent_table.verticalHeader().setDefaultSectionSize(48)
        self.recent_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.recent_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.recent_table.setMinimumHeight(235)
        self.recent_table.setMaximumHeight(320)
        layout.addWidget(self.recent_table)

        buttons = QHBoxLayout()
        view_history = QPushButton('查看全部报警记录')
        view_history.clicked.connect(lambda: self.show_page(2))
        view_monitor = QPushButton('进入实时监控')
        view_monitor.clicked.connect(lambda: self.show_page(1))
        buttons.addWidget(view_monitor)
        buttons.addWidget(view_history)
        buttons.addStretch()
        layout.addLayout(buttons)
        layout.addStretch(1)
        dashboard_scroll.setWidget(dashboard)
        self.pages.addWidget(dashboard_scroll)

    @staticmethod
    def heading(text):
        label = QLabel(text)
        label.setObjectName('pageTitle')
        return label

    @staticmethod
    def subtitle(text):
        label = QLabel(text)
        label.setObjectName('pageSubtitle')
        return label

    @staticmethod
    def make_stat_card(layout, title):
        card = QFrame()
        card.setObjectName('card')
        card_layout = QVBoxLayout(card)
        label = QLabel(title)
        label.setObjectName('statName')
        value = QLabel('0')
        value.setObjectName('statValue')
        card_layout.addWidget(label)
        card_layout.addWidget(value)
        layout.addWidget(card)
        return value

    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        for nav in (self.dashboard_nav, self.monitor_nav, self.history_nav):
            nav.setCheckable(True)
        self.dashboard_nav.setChecked(index == 0)
        self.monitor_nav.setChecked(index == 1)
        self.history_nav.setChecked(index == 2)
        if index in (0, 2):
            self.load_records()

    def start_area_selection(self):
        if self.camera_thread is None or not self.camera_thread.isRunning():
            QMessageBox.information(self, '提示', '请先启动摄像头。')
            return
        self.video_label.begin_selection()
        self.statusBar().showMessage('在视频画面上按住鼠标左键拖动，松开后完成区域设置。', 7000)

    def area_description(self):
        area = self.monitor_area
        prefix = '默认中央区域 ' if area == MONITOR_AREA else ''
        return (f'当前监控区域：{prefix}'
                f'({area[0]}, {area[1]}) - ({area[2]}, {area[3]})')

    def update_sensitivity_label(self):
        self.sensitivity_value.setText(f'面积阈值：{self.min_area} px²')

    def update_sensitivity(self, slider_value):
        self.min_area = MIN_AREA_MAX + MIN_AREA_MIN - slider_value
        self.update_sensitivity_label()
        if self.camera_thread is not None and self.camera_thread.isRunning():
            self.camera_thread.set_min_area(self.min_area)
        try:
            save_settings(self.monitor_area, self.min_area, self.camera_index, self.phone_url)
        except (OSError, ValueError) as exc:
            self.statusBar().showMessage(f'灵敏度已生效，但保存失败：{exc}', 7000)
        else:
            self.statusBar().showMessage('灵敏度已更新并保存。', 2500)

    def apply_monitor_area(self, area):
        validated = validate_monitor_area(area)
        if validated is None:
            QMessageBox.warning(self, '区域设置失败', '选择的监控区域无效，请重新绘制。')
            return
        self.monitor_area = validated
        if self.camera_thread is not None and self.camera_thread.isRunning():
            self.camera_thread.set_monitor_area(validated)
        self.area_info.setText(self.area_description())
        try:
            save_settings(validated, self.min_area, self.camera_index, self.phone_url)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '配置保存失败',
                                f'监控区域已在本次运行中生效，但未能保存到 config.json：\n{exc}')
            return
        self.statusBar().showMessage('监控区域已更新，并自动保存到 config.json。', 5000)

    def reset_monitor_area(self):
        self.video_label.cancel_selection()
        self.apply_monitor_area(MONITOR_AREA)

    def save_phone_url(self):
        url = self.phone_url_input.text().strip()
        if not url.startswith(('http://', 'https://')):
            QMessageBox.warning(self, '地址错误', '请输入以 http:// 或 https:// 开头的视频流地址。')
            return
        if url.endswith('/'):
            url = url.rstrip('/')
        if url.endswith(':8080'):
            url += '/video'
        was_running = self.camera_index == 'phone' and self.camera_thread is not None and self.camera_thread.isRunning()
        if was_running:
            self.stop_camera()
        self.phone_url = url
        self.phone_url_input.setText(url)
        try:
            save_settings(self.monitor_area, self.min_area, self.camera_index, self.phone_url)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '配置保存失败', str(exc))
        self.statusBar().showMessage('手机视频流地址已保存。', 4000)
        if was_running:
            self.start_camera()

    def device_title(self, source):
        key = str(source)
        return self.device_names.get(key, '手机 Wi-Fi 摄像头' if source == 'phone' else f'摄像头 {source}')

    def rename_camera(self):
        source = self.camera_combo.currentData()
        if source is None:
            return
        current = self.device_title(source)
        name, ok = QInputDialog.getText(self, '重命名监控设备', '设备名称：', text=current)
        if not ok:
            return
        name = name.strip()
        if not name or len(name) > 40:
            QMessageBox.warning(self, '名称无效', '请输入 1～40 个字符的设备名称。')
            return
        self.device_names[str(source)] = name
        try:
            save_device_names(self.device_names)
        except OSError as exc:
            QMessageBox.warning(self, '保存失败', str(exc))
            return
        index = self.camera_combo.findData(source)
        if index >= 0:
            self.camera_combo.setItemText(index, name)
        self.statusBar().showMessage('设备名称已保存。', 4000)

    def reconnect_camera(self):
        """重新打开当前设备；断线时无需重新启动整个程序。"""
        self.stop_camera()
        self.start_camera()
        self.statusBar().showMessage('正在重新连接当前摄像头...', 5000)

    def refresh_cameras(self):
        """扫描标准 Windows 摄像头编号；扫描期间需释放监控摄像头。"""
        was_running = self.camera_thread is not None and self.camera_thread.isRunning()
        if was_running:
            self.stop_camera()
        self.scan_camera_button.setEnabled(False)
        self.statusBar().showMessage('正在扫描摄像头，请稍候...')
        QApplication.processEvents()
        found = []
        for index in range(CAMERA_SCAN_LIMIT):
            cap = open_camera(index)
            try:
                if cap.isOpened():
                    ok, _ = cap.read()
                    if ok:
                        found.append(index)
            finally:
                cap.release()
        self.available_cameras = found
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        for index in found:
            self.camera_combo.addItem(self.device_title(index), index)
        self.camera_combo.addItem(self.device_title('phone'), 'phone')
        if found or self.camera_index == 'phone':
            if self.camera_index != 'phone' and self.camera_index not in found:
                self.camera_index = found[0] if found else 'phone'
            chosen = self.camera_combo.findData(self.camera_index)
            self.camera_combo.setCurrentIndex(chosen if chosen >= 0 else 0)
            self.camera_combo.setEnabled(True)
            self.statusBar().showMessage(f'扫描完成，发现 {len(found)} 个可用摄像头。', 5000)
        else:
            self.camera_index = 'phone'
            self.camera_combo.setCurrentIndex(self.camera_combo.findData('phone'))
            self.camera_combo.setEnabled(True)
            self.statusBar().showMessage('未发现本地摄像头，仍可选择手机 Wi-Fi 摄像头。', 7000)
        self.camera_combo.blockSignals(False)
        self.scan_camera_button.setEnabled(True)
        if was_running:
            self.start_camera()

    def change_camera(self, combo_index):
        if combo_index < 0:
            return
        new_index = self.camera_combo.itemData(combo_index)
        if new_index is None or (isinstance(new_index, int) and new_index < 0) or new_index == self.camera_index:
            return
        was_running = self.camera_thread is not None and self.camera_thread.isRunning()
        if was_running:
            self.stop_camera()
        self.camera_index = new_index
        try:
            save_settings(self.monitor_area, self.min_area, self.camera_index, self.phone_url)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '保存失败', f'摄像头已切换，但配置保存失败：{exc}')
        self.statusBar().showMessage('已选择手机 Wi-Fi 摄像头。' if new_index == 'phone' else f'已选择摄像头 {new_index}。', 4000)
        if was_running:
            self.start_camera()

    def start_camera(self):
        if self.camera_thread is not None and self.camera_thread.isRunning():
            return
        self.camera_thread = CameraThread(self.phone_url if self.camera_index == 'phone' else self.camera_index)
        self.camera_thread.set_monitor_area(self.monitor_area)
        self.camera_thread.set_min_area(self.min_area)
        self.camera_thread.frame_ready.connect(self.update_frame)
        self.camera_thread.status_changed.connect(self.update_status)
        self.camera_thread.camera_error.connect(self.show_camera_error)
        self.camera_thread.alarm_saved.connect(self.on_alarm_saved)
        self.camera_thread.video_saved.connect(self.on_video_saved)
        self.camera_thread.finished.connect(self.camera_finished)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.select_area_button.setEnabled(True)
        self.device_error = False
        self.device_status_label.setText(f'设备状态：正在连接 · {self.device_title(self.camera_index)}')
        self.status_label.setText('●  当前状态：正在启动监控...')
        self.dashboard_camera.setText('正在启动')
        self.dashboard_detection.setText('初始化中')
        self.status_label.setStyleSheet('')
        self.camera_thread.start()

    def update_frame(self, image):
        self.current_image = image
        self.video_label.setPixmap(QPixmap.fromImage(image).scaled(
            self.video_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def update_status(self, status):
        self.status_label.setText(f'●  当前状态：{status}')
        self.dashboard_camera.setText('运行中')
        self.device_status_label.setText(f'设备状态：已连接 · {self.device_title(self.camera_index)}')
        self.dashboard_detection.setText(status)
        self.status_label.setStyleSheet(
            'color: #f87171; font-weight: bold; background: #3a1b2b; border: 1px solid #9f3444; border-radius: 8px; padding: 12px;' if '警告' in status
            else 'color: #4ade80; font-weight: bold; background: #142238; border: 1px solid #263b55; border-radius: 8px; padding: 12px;')

    def on_alarm_saved(self, record_id):
        self.statusBar().showMessage(f'报警截图与数据库记录已保存，编号：{record_id}', 5000)
        self.load_records()

    def on_video_saved(self, message):
        self.statusBar().showMessage(message, 8000)
        self.load_records()

    def stop_camera(self):
        if self.camera_thread is not None and self.camera_thread.isRunning():
            self.camera_thread.stop()
            self.camera_thread.wait()
        self.current_image = None
        self.video_label.cancel_selection()
        self.select_area_button.setEnabled(False)
        self.video_label.clear()
        self.video_label.setText('摄像头已停止')
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText('●  当前状态：已停止')
        self.dashboard_camera.setText('已停止')
        self.device_status_label.setText('设备状态：已停止')
        self.dashboard_detection.setText('待启动')
        self.status_label.setStyleSheet('')

    def camera_finished(self):
        self.video_label.cancel_selection()
        self.select_area_button.setEnabled(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText('●  当前状态：连接异常' if self.device_error else '●  当前状态：摄像头已停止')
        self.dashboard_camera.setText('连接异常' if self.device_error else '已停止')
        self.device_status_label.setText('设备状态：连接异常，请检查设备并点击重新连接' if self.device_error else '设备状态：已停止')
        self.dashboard_detection.setText('待启动')
        self.status_label.setStyleSheet('')

    def show_camera_error(self, message):
        self.device_error = True
        self.device_status_label.setText('设备状态：连接异常，请检查设备并点击重新连接')
        QMessageBox.warning(self, '监控提示', message)

    def load_records(self):
        try:
            with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
                records = conn.execute('''SELECT id, alarm_type, alarm_time,
                    image_path, status, video_path FROM alarm_records ORDER BY id DESC''').fetchall()
        except sqlite3.Error as exc:
            QMessageBox.critical(self, '数据库错误', str(exc))
            return
        self.all_records = records
        self.filter_records()
        self.total_value.setText(str(len(records)))
        today = date.today().isoformat()
        self.today_value.setText(str(sum(str(r[2]).startswith(today) for r in records)))
        self.pending_value.setText(str(sum(r[4] == '未处理' for r in records)))
        self.dashboard_total.setText(str(len(records)))
        self.dashboard_today.setText(str(sum(str(r[2]).startswith(today) for r in records)))
        pending = sum(r[4] == '未处理' for r in records)
        self.dashboard_pending.setText(str(pending))
        self.dashboard_processed.setText(str(len(records) - pending))
        # 使用全部报警记录更新图表，和历史页面的筛选条件相互独立。
        day_list = [date.today() - timedelta(days=offset) for offset in range(6, -1, -1)]
        day_counts = {day: 0 for day in day_list}
        hour_counts = [0] * 6
        for record in records:
            try:
                moment = datetime.strptime(str(record[2])[:19], '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                continue
            if moment.date() in day_counts:
                day_counts[moment.date()] += 1
            hour_counts[moment.hour // 4] += 1
        self.week_chart.labels = [day.strftime('%m/%d') for day in day_list]
        self.week_chart.set_values([day_counts[day] for day in day_list])
        self.hour_chart.set_values(hour_counts)
        processed_count = len(records) - pending
        total_count = len(records)
        processed_percent = processed_count / total_count * 100 if total_count else 0
        self.process_summary.setText(
            f'报警处理情况：已处理 {processed_count}（{processed_percent:.1f}%）  /  '
            f'未处理 {pending}（{100-processed_percent if total_count else 0:.1f}%）')
        self.recent_table.setRowCount(min(len(records), 5))
        for row, record in enumerate(records[:5]):
            for col, value in enumerate((record[2], record[1], record[4])):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                if col == 2:
                    from PySide6.QtGui import QColor
                    item.setForeground(QColor('#4ade80' if value == '已处理' else '#fbbf24'))
                self.recent_table.setItem(row, col, item)
        self.statusBar().showMessage(f'已加载 {len(records)} 条报警记录', 3000)

    def filter_records(self, *args):
        """仅筛选历史表格，概览统计仍使用数据库全部记录。"""
        keyword = self.search_input.text().strip().casefold()
        status_choice = self.status_filter.currentText()
        date_choice = self.date_filter.currentText()
        today = date.today()
        week_start = today - timedelta(days=6)
        matched = []
        for record in self.all_records:
            record_id, alarm_type, alarm_time, _, status, _video = record
            if keyword and keyword not in str(record_id).casefold() and keyword not in str(alarm_type).casefold():
                continue
            if status_choice != '全部状态' and status != status_choice:
                continue
            if date_choice != '全部日期':
                try:
                    record_date = date.fromisoformat(str(alarm_time)[:10])
                except ValueError:
                    continue
                if date_choice == '今天' and record_date != today:
                    continue
                if date_choice == '最近7天' and not (week_start <= record_date <= today):
                    continue
            matched.append(record)

        self.filtered_records = matched
        self.table.setRowCount(len(matched))
        for row, record in enumerate(matched):
            for col, value in enumerate(record):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                if col == 3:
                    item.setToolTip(str(value))
                if col == 5:
                    item.setText('可查看' if value else '录像生成中/无录像')
                    item.setToolTip(str(value or ''))
                if col == 4:
                    item.setForeground(QColor('#4ade80' if str(value) == '已处理' else '#fbbf24'))
                self.table.setItem(row, col, item)
        self.filter_count.setText(f'显示 {len(matched)} 条记录 / 共 {len(self.all_records)} 条')

    def export_records(self):
        """将当前筛选结果或全部报警记录导出为 Excel/WPS 可打开的 CSV。"""
        choice = QMessageBox.question(
            self, '导出报警记录',
            '选择导出范围：\n\n点击“是”导出当前筛选结果；\n点击“否”导出全部记录。',
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes)
        if choice == QMessageBox.Cancel:
            return
        filtered = choice == QMessageBox.Yes
        records = self.filtered_records if filtered else self.all_records
        if not records:
            QMessageBox.information(self, '没有可导出的记录', '所选范围没有报警记录。')
            return
        scope = '筛选结果' if filtered else '全部记录'
        default_name = f"报警记录_{scope}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        save_path, _ = QFileDialog.getSaveFileName(
            self, '保存报警记录', os.path.join(BASE_DIR, default_name),
            'CSV 文件 (*.csv)')
        if not save_path:
            return
        if not save_path.lower().endswith('.csv'):
            save_path += '.csv'
        try:
            with open(save_path, 'w', newline='', encoding='utf-8-sig') as file:
                writer = csv.writer(file)
                writer.writerow(['报警编号', '报警类型', '报警时间', '截图路径', '处理状态', '事件录像路径'])
                for record in records:
                    # 避免 Excel/WPS 将来自数据库的文本误识别为公式。
                    safe_record = []
                    for value in record:
                        cell = str(value)
                        if cell.lstrip().startswith(('=', '+', '-', '@')):
                            cell = "'" + cell
                        safe_record.append(cell)
                    writer.writerow(safe_record)
        except (OSError, UnicodeError) as exc:
            QMessageBox.critical(self, '导出失败', f'无法保存文件：\n{exc}')
            return
        QMessageBox.information(
            self, '导出成功',
            f'已导出{scope}，共 {len(records)} 条。\n\n保存位置：\n{save_path}')
        self.statusBar().showMessage(f'已导出 {len(records)} 条报警记录', 5000)

    def selected_record(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, '提示', '请先选择一条报警记录。')
            return None
        return (int(self.table.item(row, 0).text()),
                self.table.item(row, 3).text())

    def preview_image(self, *args):
        selected = self.selected_record()
        if selected is None:
            return
        _, relative_path = selected
        path = os.path.abspath(os.path.join(BASE_DIR, relative_path))
        if not os.path.isfile(path):
            QMessageBox.warning(self, '截图不存在', '未找到对应的报警截图。')
            return
        ImagePreview(path, self).exec()

    def preview_video(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, '提示', '请先选择一条报警记录。')
            return
        video_path = self.table.item(row, 5).toolTip()
        if not video_path:
            QMessageBox.information(self, '暂无录像',
                                    '这条报警尚无录像。旧版历史报警不会自动补录；新报警请等待约 10 秒后刷新。')
            return
        absolute_path = os.path.abspath(os.path.join(BASE_DIR, video_path))
        if not os.path.isfile(absolute_path):
            QMessageBox.warning(self, '录像不存在', '找不到对应的 MP4 录像文件。')
            return
        import subprocess
        try:
            os.startfile(absolute_path)  # Windows 默认播放器
        except OSError as exc:
            QMessageBox.warning(self, '无法播放录像', str(exc))

    def mark_processed(self):
        selected = self.selected_record()
        if selected is None:
            return
        record_id, _ = selected
        try:
            with sqlite3.connect(DATABASE_PATH, timeout=10) as conn:
                conn.execute('UPDATE alarm_records SET status=? WHERE id=?',
                             ('已处理', record_id))
        except sqlite3.Error as exc:
            QMessageBox.critical(self, '数据库错误', str(exc))
            return
        self.load_records()
        self.statusBar().showMessage(f'报警记录 {record_id} 已处理', 4000)

    def closeEvent(self, event):
        self.stop_camera()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
