
import os
import sys
import sqlite3
from datetime import date, datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QDialog,
    QScrollArea,
    QAbstractItemView,
    QFrame
)

# ==================================
# 1. 数据库路径
# ==================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DATABASE_PATH = os.path.join(
    BASE_DIR,
    "alarms.db"
)


# ==================================
# 2. 报警截图预览窗口
# ==================================

class ImagePreview(QDialog):

    def __init__(self, image_path, parent=None):
        super().__init__(parent)

        self.setWindowTitle("报警截图预览")
        self.resize(850, 600)

        layout = QVBoxLayout(self)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)

        image_label = QLabel()
        image_label.setAlignment(Qt.AlignCenter)

        pixmap = QPixmap(image_path)

        if pixmap.isNull():
            image_label.setText("无法加载报警截图")
        else:
            image_label.setPixmap(pixmap)

        scroll_area.setWidget(image_label)
        layout.addWidget(scroll_area)


# ==================================
# 3. 历史记录主窗口
# ==================================

class HistoryWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "OpenCV智能视觉监控系统 - 报警历史管理"
        )

        self.resize(1100, 720)

        self.init_ui()
        self.load_records()

    # ==================================
    # 4. 初始化界面
    # ==================================

    def init_ui(self):

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(18)
        main_layout.setContentsMargins(25, 25, 25, 25)

        # 页面标题
        title = QLabel("智能视觉监控系统")
        title.setObjectName("mainTitle")

        subtitle = QLabel("报警历史记录管理中心")
        subtitle.setObjectName("subtitle")

        main_layout.addWidget(title)
        main_layout.addWidget(subtitle)

        # ==================================
        # 5. 统计卡片
        # ==================================

        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(15)

        self.total_label = self.create_stat_card(
            stats_layout,
            "累计报警"
        )

        self.today_label = self.create_stat_card(
            stats_layout,
            "今日报警"
        )

        self.pending_label = self.create_stat_card(
            stats_layout,
            "未处理报警"
        )

        main_layout.addLayout(stats_layout)

        # ==================================
        # 6. 操作按钮
        # ==================================

        button_layout = QHBoxLayout()

        refresh_button = QPushButton("刷新记录")
        refresh_button.clicked.connect(
            self.load_records
        )

        preview_button = QPushButton("查看报警截图")
        preview_button.clicked.connect(
            self.preview_image
        )

        process_button = QPushButton("标记为已处理")
        process_button.clicked.connect(
            self.mark_processed
        )

        button_layout.addWidget(refresh_button)
        button_layout.addWidget(preview_button)
        button_layout.addWidget(process_button)
        button_layout.addStretch()

        main_layout.addLayout(button_layout)

        # ==================================
        # 7. 报警记录表格
        # ==================================

        self.table = QTableWidget()

        self.table.setColumnCount(5)

        self.table.setHorizontalHeaderLabels([
            "报警编号",
            "报警类型",
            "报警时间",
            "截图路径",
            "处理状态"
        ])

        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )

        self.table.setSelectionBehavior(
            QAbstractItemView.SelectRows
        )

        self.table.setSelectionMode(
            QAbstractItemView.SingleSelection
        )

        self.table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )

        self.table.cellDoubleClicked.connect(
            self.preview_image
        )

        main_layout.addWidget(self.table)

        self.setStyleSheet("""
            QMainWindow {
                background-color: #f4f7fb;
            }

            QLabel#mainTitle {
                font-size: 28px;
                font-weight: bold;
                color: #172b4d;
            }

            QLabel#subtitle {
                font-size: 15px;
                color: #64748b;
            }

            QFrame#statCard {
                background-color: white;
                border: 1px solid #e2e8f0;
                border-radius: 10px;
            }

            QLabel#statTitle {
                font-size: 14px;
                color: #64748b;
            }

            QLabel#statValue {
                font-size: 30px;
                font-weight: bold;
                color: #2563eb;
            }

            QPushButton {
                background-color: #2563eb;
                color: white;
                padding: 10px 18px;
                border-radius: 6px;
                font-size: 14px;
            }

            QPushButton:hover {
                background-color: #1d4ed8;
            }

            QTableWidget {
                background-color: white;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                gridline-color: #e2e8f0;
                font-size: 13px;
                color: #1e293b;
            }

            QHeaderView::section {
                background-color: #eaf0f8;
                color: #334155;
                padding: 10px;
                border: none;
                font-weight: bold;
            }
        """)

    # ==================================
    # 8. 创建统计卡片
    # ==================================

    def create_stat_card(self, parent_layout, title):

        card = QFrame()
        card.setObjectName("statCard")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 15, 20, 15)

        title_label = QLabel(title)
        title_label.setObjectName("statTitle")

        value_label = QLabel("0")
        value_label.setObjectName("statValue")

        layout.addWidget(title_label)
        layout.addWidget(value_label)

        parent_layout.addWidget(card)

        return value_label

    # ==================================
    # 9. 加载数据库记录
    # ==================================

    def load_records(self):

        if not os.path.exists(DATABASE_PATH):
            QMessageBox.warning(
                self,
                "数据库不存在",
                "没有找到 alarms.db，请先运行 main.py。"
            )
            return

        try:
            with sqlite3.connect(DATABASE_PATH) as conn:

                cursor = conn.cursor()

                cursor.execute("""
                    SELECT
                        id,
                        alarm_type,
                        alarm_time,
                        image_path,
                        status
                    FROM alarm_records
                    ORDER BY id DESC
                """)

                records = cursor.fetchall()

        except sqlite3.Error as error:
            QMessageBox.critical(
                self,
                "数据库错误",
                str(error)
            )
            return

        self.table.setRowCount(len(records))

        for row_index, record in enumerate(records):

            for col_index, value in enumerate(record):

                item = QTableWidgetItem(str(value))

                item.setTextAlignment(
                    Qt.AlignCenter
                )

                self.table.setItem(
                    row_index,
                    col_index,
                    item
                )

        # 更新统计
        total = len(records)

        today = date.today().isoformat()

        today_count = sum(
            1 for record in records
            if str(record[2]).startswith(today)
        )

        pending_count = sum(
            1 for record in records
            if record[4] == "未处理"
        )

        self.total_label.setText(str(total))
        self.today_label.setText(str(today_count))
        self.pending_label.setText(str(pending_count))

        self.statusBar().showMessage(
            f"已加载 {total} 条报警记录"
        )

    # ==================================
    # 10. 获取当前选中记录
    # ==================================

    def get_selected_row(self):

        row = self.table.currentRow()

        if row < 0:
            QMessageBox.information(
                self,
                "提示",
                "请先选择一条报警记录。"
            )
            return None

        return row

    # ==================================
    # 11. 查看报警截图
    # ==================================

    def preview_image(self, *args):

        row = self.get_selected_row()

        if row is None:
            return

        image_path = self.table.item(
            row, 3
        ).text()

        full_path = os.path.join(
            BASE_DIR,
            image_path
        )

        if not os.path.exists(full_path):
            QMessageBox.warning(
                self,
                "截图不存在",
                "找不到对应的报警截图文件。"
            )
            return

        dialog = ImagePreview(
            full_path,
            self
        )

        dialog.exec()

    # ==================================
    # 12. 标记报警已处理
    # ==================================

    def mark_processed(self):

        row = self.get_selected_row()

        if row is None:
            return

        record_id = int(
            self.table.item(row, 0).text()
        )

        try:
            with sqlite3.connect(DATABASE_PATH) as conn:

                conn.execute("""
                    UPDATE alarm_records
                    SET status = ?
                    WHERE id = ?
                """, (
                    "已处理",
                    record_id
                ))

                conn.commit()

            QMessageBox.information(
                self,
                "操作成功",
                "报警记录已标记为已处理。"
            )

            self.load_records()

        except sqlite3.Error as error:

            QMessageBox.critical(
                self,
                "数据库错误",
                str(error)
            )


# ==================================
# 13. 启动程序
# ==================================

if __name__ == "__main__":

    app = QApplication(sys.argv)

    window = HistoryWindow()
    window.show()

    sys.exit(app.exec())
