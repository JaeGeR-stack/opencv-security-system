
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import os
import time
import sqlite3
from datetime import datetime

# ==================================
# 1. 基础配置
# ==================================

CAMERA_INDEX = 0

FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# 最小运动区域面积
MIN_AREA = 1800

# 连续帧确认
CONFIRM_FRAMES = 3
CLEAR_FRAMES = 8

# 背景初始化时间
WARMUP_SECONDS = 5

# 中央监控区域
MONITOR_AREA = (160, 120, 480, 360)

# 截图提示显示时间
SAVE_MESSAGE_SECONDS = 3

# 项目根目录
BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

# 截图目录
CAPTURE_DIR = os.path.join(
    BASE_DIR,
    "captures"
)

# 数据库文件
DATABASE_PATH = os.path.join(
    BASE_DIR,
    "alarms.db"
)

os.makedirs(CAPTURE_DIR, exist_ok=True)


# ==================================
# 2. 初始化 SQLite 数据库
# ==================================

def init_database():
    """初始化数据库及报警记录表"""

    with sqlite3.connect(DATABASE_PATH) as conn:

        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alarm_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alarm_type TEXT NOT NULL,
                alarm_time TEXT NOT NULL,
                image_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '未处理'
            )
        """)

        conn.commit()

    print("SQLite 数据库初始化成功！")


def save_alarm_record(alarm_type, image_path):
    """将报警信息保存到数据库"""

    alarm_time = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # 存储相对路径，便于项目移动
    relative_path = os.path.relpath(
        image_path,
        BASE_DIR
    )

    with sqlite3.connect(DATABASE_PATH) as conn:

        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO alarm_records (
                alarm_type,
                alarm_time,
                image_path,
                status
            )
            VALUES (?, ?, ?, ?)
        """, (
            alarm_type,
            alarm_time,
            relative_path,
            "未处理"
        ))

        record_id = cursor.lastrowid
        conn.commit()

    print(
        f"报警记录已写入数据库，"
        f"编号：{record_id}"
    )

    return record_id


# ==================================
# 3. 中文字体设置
# ==================================

font_path = "C:/Windows/Fonts/msyh.ttc"

if not os.path.exists(font_path):
    font_path = "C:/Windows/Fonts/simhei.ttf"

if not os.path.exists(font_path):
    print("错误：找不到中文字体文件！")
    raise SystemExit(1)

font = ImageFont.truetype(
    font_path,
    24
)


def draw_chinese_text(frame, text, position, color):
    """在 OpenCV 图像中绘制中文"""

    image = Image.fromarray(
        cv2.cvtColor(
            frame,
            cv2.COLOR_BGR2RGB
        )
    )

    draw = ImageDraw.Draw(image)

    draw.text(
        position,
        text,
        font=font,
        fill=color
    )

    return cv2.cvtColor(
        np.array(image),
        cv2.COLOR_RGB2BGR
    )


# ==================================
# 4. 判断目标是否进入监控区域
# ==================================

def is_inside_monitor_area(box, area):
    """判断目标框是否与监控区域相交"""

    x, y, w, h = box

    ax1, ay1, ax2, ay2 = area

    bx2 = x + w
    by2 = y + h

    return (
        bx2 > ax1
        and x < ax2
        and by2 > ay1
        and y < ay2
    )


# ==================================
# 5. 保存报警截图
# ==================================

def save_alarm_screenshot(frame):
    """保存报警画面"""

    now = datetime.now()

    filename = now.strftime(
        "alarm_%Y%m%d_%H%M%S_%f.jpg"
    )

    filepath = os.path.join(
        CAPTURE_DIR,
        filename
    )

    success, encoded_image = cv2.imencode(
        ".jpg",
        frame
    )

    if not success:
        print("截图编码失败！")
        return None

    try:

        encoded_image.tofile(filepath)

        print(
            f"报警截图已保存：{filepath}"
        )

        return filepath

    except OSError as error:

        print(
            f"截图保存失败：{error}"
        )

        return None


# ==================================
# 6. 初始化系统
# ==================================

init_database()

cap = cv2.VideoCapture(
    CAMERA_INDEX
)

if not cap.isOpened():
    print("摄像头打开失败！")
    raise SystemExit(1)

background_subtractor = (
    cv2.createBackgroundSubtractorMOG2(
        history=500,
        varThreshold=50,
        detectShadows=True
    )
)

# 去除噪声
small_kernel = cv2.getStructuringElement(
    cv2.MORPH_ELLIPSE,
    (3, 3)
)

# 合并运动区域
large_kernel = cv2.getStructuringElement(
    cv2.MORPH_RECT,
    (25, 25)
)

# 全画面运动状态
motion_frames = 0
clear_frames = 0
motion_confirmed = False

# 中央区域入侵状态
intrusion_frames = 0
safe_frames = 0
intrusion_confirmed = False

# 上一次报警状态
previous_intrusion_state = False

# 截图提示结束时间
save_message_until = 0

# 背景初始化开始时间
start_time = time.monotonic()

print("================================")
print("OpenCV 智能视觉监控系统")
print("================================")
print("摄像头启动成功！")
print("中央区域入侵检测已开启！")
print("SQLite 报警记录功能已开启！")
print(f"数据库位置：{DATABASE_PATH}")
print("按 Q 键退出程序。")


# ==================================
# 7. 主循环
# ==================================

try:

    while True:

        ret, frame = cap.read()

        if not ret:
            print("摄像头读取失败！")
            break

        # 统一画面尺寸
        frame = cv2.resize(
            frame,
            (FRAME_WIDTH, FRAME_HEIGHT)
        )

        # 左右镜像
        frame = cv2.flip(
            frame,
            1
        )

        # ==================================
        # 8. 运动检测
        # ==================================

        mask = background_subtractor.apply(
            frame
        )

        # 去除阴影
        _, mask = cv2.threshold(
            mask,
            200,
            255,
            cv2.THRESH_BINARY
        )

        # 开运算去除噪声
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_OPEN,
            small_kernel
        )

        # 闭运算连接区域
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            large_kernel
        )

        # 适度膨胀
        mask = cv2.dilate(
            mask,
            small_kernel,
            iterations=2
        )

        # ==================================
        # 9. 查找运动目标
        # ==================================

        contours, _ = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )

        motion_boxes = []

        for contour in contours:

            area = cv2.contourArea(
                contour
            )

            if area < MIN_AREA:
                continue

            x, y, w, h = (
                cv2.boundingRect(contour)
            )

            motion_boxes.append(
                (x, y, w, h)
            )

        # ==================================
        # 10. 区域入侵判断
        # ==================================

        intrusion_detected = False

        for box in motion_boxes:

            if is_inside_monitor_area(
                box,
                MONITOR_AREA
            ):

                intrusion_detected = True
                break

        # ==================================
        # 11. 连续帧状态判断
        # ==================================

        warming_up = (
            time.monotonic() - start_time
            < WARMUP_SECONDS
        )

        if warming_up:

            motion_frames = 0
            clear_frames = 0
            motion_confirmed = False

            intrusion_frames = 0
            safe_frames = 0
            intrusion_confirmed = False

        else:

            # 全画面运动判断
            if motion_boxes:

                motion_frames += 1
                clear_frames = 0

                if motion_frames >= CONFIRM_FRAMES:
                    motion_confirmed = True

            else:

                clear_frames += 1
                motion_frames = 0

                if clear_frames >= CLEAR_FRAMES:
                    motion_confirmed = False

            # 中央区域入侵判断
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

        # ==================================
        # 12. 绘制中央监控区域
        # ==================================

        ax1, ay1, ax2, ay2 = MONITOR_AREA

        if intrusion_confirmed:

            area_color = (0, 0, 255)
            area_thickness = 4

        else:

            area_color = (0, 165, 255)
            area_thickness = 2

        cv2.rectangle(
            frame,
            (ax1, ay1),
            (ax2, ay2),
            area_color,
            area_thickness
        )

        # ==================================
        # 13. 绘制运动目标框
        # ==================================

        for box in motion_boxes:

            x, y, w, h = box

            inside = is_inside_monitor_area(
                box,
                MONITOR_AREA
            )

            if inside:
                box_color = (0, 0, 255)
            else:
                box_color = (0, 255, 0)

            cv2.rectangle(
                frame,
                (x, y),
                (x + w, y + h),
                box_color,
                2
            )

        # ==================================
        # 14. 中文状态显示
        # ==================================

        if warming_up:

            status = "正在初始化背景..."
            color = (255, 220, 80)

        elif intrusion_confirmed:

            status = "警告：检测到区域入侵！"
            color = (255, 60, 60)

        elif motion_confirmed:

            status = "检测到运动目标（区域外）"
            color = (255, 200, 50)

        else:

            status = "监控区域正常"
            color = (0, 255, 100)

        frame = draw_chinese_text(
            frame,
            status,
            (20, 20),
            color
        )

        frame = draw_chinese_text(
            frame,
            "中央监控区域",
            (ax1 + 5, ay1 + 5),
            (255, 220, 100)
        )

        # ==================================
        # 15. 自动截图与数据库记录
        # ==================================

        new_alarm = (
            intrusion_confirmed
            and not previous_intrusion_state
        )

        if new_alarm:

            # 保存当前报警截图
            saved_path = save_alarm_screenshot(
                frame.copy()
            )

            if saved_path is not None:

                # 写入 SQLite 数据库
                try:

                    record_id = save_alarm_record(
                        "区域入侵",
                        saved_path
                    )

                    print(
                        f"本次报警编号：{record_id}"
                    )

                except sqlite3.Error as error:

                    print(
                        f"数据库记录失败：{error}"
                    )

                save_message_until = (
                    time.monotonic()
                    + SAVE_MESSAGE_SECONDS
                )

        # 更新报警状态
        previous_intrusion_state = (
            intrusion_confirmed
        )

        # 显示保存成功提示
        if time.monotonic() < save_message_until:

            frame = draw_chinese_text(
                frame,
                "报警截图已保存",
                (20, 60),
                (255, 220, 80)
            )

        # ==================================
        # 16. 显示监控画面
        # ==================================

        cv2.imshow(
            "Smart Security Monitoring",
            frame
        )

        cv2.imshow(
            "Motion Mask",
            mask
        )

        # 按 Q 退出
        if cv2.waitKey(30) & 0xFF == ord("q"):
            break

finally:

    cap.release()
    cv2.destroyAllWindows()

    print("监控系统已安全退出。")
