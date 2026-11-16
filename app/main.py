import sys

from PySide6.QtWidgets import QApplication

from app import config
from app.ui.main_window import MainWindow


def main() -> None:
    config.setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("RFID EPC 采集上传工具")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
