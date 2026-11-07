#!/usr/bin/env python3
"""Read-only acceptance panel for eight IR inputs on an MCP23017 GPB port.

The CH341 is the USB-to-I2C bridge, not the GPB GPIO expander.  This program
assumes that the device at 0x27 is an MCP23017 using its default BANK=0 register
layout.  It only reads GPIOB (0x13); it never sets pin directions or outputs.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from smbus2 import SMBus


def find_ch341_bus() -> int:
    for entry in sorted(Path("/sys/class/i2c-dev").glob("i2c-*")):
        try:
            if "CH341 I2C USB" in (entry / "name").read_text():
                return int(entry.name.split("-")[1])
        except (OSError, ValueError):
            continue
    raise RuntimeError("未找到 CH341 USB I²C 总线；请检查 USB 和驱动")


class Reader(QThread):
    sample = pyqtSignal(int, int)
    failed = pyqtSignal(str)

    def __init__(self, bus: int | None, address: int, interval_ms: int):
        super().__init__()
        self.bus = bus
        self.address = address
        self.interval_ms = interval_ms
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        last_error = ""
        i2c: SMBus | None = None
        bus_number = -1
        period = self.interval_ms / 1000
        next_poll = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    if i2c is None:
                        bus_number = self.bus if self.bus is not None else find_ch341_bus()
                        i2c = SMBus(bus_number)
                    value = i2c.read_byte_data(self.address, 0x13)
                    self.sample.emit(bus_number, value)
                    last_error = ""
                    next_poll += period
                    remaining = next_poll - time.monotonic()
                    if remaining < 0:
                        next_poll = time.monotonic()
                        remaining = 0
                    self._stop.wait(remaining)
                except (OSError, RuntimeError) as exc:
                    if i2c is not None:
                        i2c.close()
                        i2c = None
                    message = str(exc)
                    if message != last_error:
                        self.failed.emit(message)
                        last_error = message
                    next_poll = time.monotonic()
                    self._stop.wait(0.2)
        finally:
            if i2c is not None:
                i2c.close()


class IRWindow(QMainWindow):
    def __init__(self, bus: int | None, address: int, interval_ms: int):
        super().__init__()
        self.bus = bus
        self.interval_ms = interval_ms
        self.reader: Reader | None = None
        self.previous: int | None = None
        self.transitions = [0] * 8
        self.samples = 0

        self.setWindowTitle("CH341 · 红外输入验收")
        self.resize(900, 870)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        title = QLabel("红外输入 GPB0–GPB7")
        title.setFont(QFont("Sans Serif", 20, QFont.Weight.Bold))
        outer.addWidget(title)

        note = QLabel(
            "按 MCP23017（BANK=0）读取 GPIOB/0x13；0x27 的芯片型号尚待实物确认。"
            "仅显示电平，不设置方向或输出。"
        )
        note.setWordWrap(True)
        outer.addWidget(note)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("I²C 地址："))
        self.address_box = QSpinBox()
        self.address_box.setRange(0x20, 0x27)
        self.address_box.setDisplayIntegerBase(16)
        self.address_box.setPrefix("0x")
        self.address_box.setValue(address)
        controls.addWidget(self.address_box)
        self.toggle_button = QPushButton("暂停")
        self.toggle_button.clicked.connect(self.toggle)
        controls.addWidget(self.toggle_button)
        clear_button = QPushButton("清零翻转次数")
        clear_button.clicked.connect(self.clear_counts)
        controls.addWidget(clear_button)
        controls.addStretch()
        outer.addLayout(controls)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        cards = QWidget()
        grid = QGridLayout(cards)
        self.value_labels: list[QLabel] = []
        self.count_labels: list[QLabel] = []
        for bit in range(8):
            card = QWidget()
            card.setObjectName("irCard")
            card.setStyleSheet(
                "QWidget#irCard { border: 1px solid #aab2bc; "
                "border-radius: 8px; padding: 6px; }"
            )
            column = QVBoxLayout(card)
            name = QLabel(f"GPB{bit}  ·  接口第 {8 - bit} 脚")
            name.setFont(QFont("Sans Serif", 13, QFont.Weight.Bold))
            value = QLabel("等待数据")
            value.setFont(QFont("Sans Serif", 24, QFont.Weight.Bold))
            count = QLabel("翻转 0 次")
            column.addWidget(name)
            column.addWidget(value)
            column.addWidget(count)
            grid.addWidget(card, bit // 2, bit % 2)
            self.value_labels.append(value)
            self.count_labels.append(count)
        scroll.setWidget(cards)
        outer.addWidget(scroll)

        self.summary = QLabel("正在读取……")
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)
        self.start_reader()

    def start_reader(self) -> None:
        self.previous = None
        self.address_box.setEnabled(False)
        self.toggle_button.setText("暂停")
        self.reader = Reader(self.bus, self.address_box.value(), self.interval_ms)
        self.reader.sample.connect(self.on_sample)
        self.reader.failed.connect(self.on_error)
        self.reader.start()

    def stop_reader(self) -> bool:
        if self.reader is not None:
            self.reader.stop()
            if not self.reader.wait(3000):
                self.summary.setText("正在等待 I²C 读取结束，请稍候再关闭")
                return False
            self.reader = None
        return True

    def toggle(self) -> None:
        if self.reader is not None:
            if not self.stop_reader():
                return
            self.address_box.setEnabled(True)
            self.toggle_button.setText("开始")
            self.summary.setText("已暂停")
        else:
            self.start_reader()

    def clear_counts(self) -> None:
        self.transitions = [0] * 8
        for label in self.count_labels:
            label.setText("翻转 0 次")

    def on_sample(self, bus: int, value: int) -> None:
        self.samples += 1
        changed = 0xFF if self.previous is None else self.previous ^ value
        for bit in range(8):
            if not changed & (1 << bit):
                continue
            state = (value >> bit) & 1
            if self.previous is not None and state != ((self.previous >> bit) & 1):
                self.transitions[bit] += 1
            label = self.value_labels[bit]
            label.setText("高电平  1" if state else "低电平  0")
            label.setStyleSheet(
                "color: #166534; border: none" if state else "color: #b91c1c; border: none"
            )
            self.count_labels[bit].setText(f"翻转 {self.transitions[bit]} 次")
        self.previous = value
        if changed or self.samples % 10 == 0:
            self.summary.setText(
                f"CH341 /dev/i2c-{bus}  ·  地址 0x{self.address_box.value():02X}  ·  "
                f"GPIOB=0x{value:02X}  ·  已读取 {self.samples} 次  ·  "
                f"{datetime.now():%H:%M:%S.%f}"[:-3]
            )

    def on_error(self, message: str) -> None:
        self.previous = None
        for label in self.value_labels:
            label.setText("读取失败")
            label.setStyleSheet("color: #6b7280; border: none")
        self.summary.setText(f"读取失败：{message}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.stop_reader():
            event.accept()
        else:
            event.ignore()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bus", type=int, help="I²C bus number; default: auto-detect CH341")
    parser.add_argument("--address", type=lambda x: int(x, 0), default=0x27)
    parser.add_argument("--interval-ms", type=int, default=20)
    args = parser.parse_args()
    if not 0x20 <= args.address <= 0x27:
        parser.error("MCP23017 address must be in 0x20..0x27")
    if not 10 <= args.interval_ms <= 1000:
        parser.error("--interval-ms must be in 10..1000")
    app = QApplication(sys.argv[:1])
    window = IRWindow(args.bus, args.address, args.interval_ms)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
