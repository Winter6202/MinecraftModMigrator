import sys
import os
import shutil
import zipfile
import hashlib
import requests
import webbrowser
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QPushButton, QComboBox, QFileDialog, QMessageBox, QListWidget, QDialog, QCheckBox
)
from PySide6.QtCore import QThread, Signal

MODRINTH_API = "https://api.modrinth.com/v2"
MOJANG_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"

LOADER_INSTALLER_URLS = {
    "fabric": "https://fabricmc.net/use/installer/",
    "forge": "https://files.minecraftforge.net/",
    "neoforge": "https://neoforged.net/",
    "quilt": "https://quiltmc.org/en/install/"
}

def get_file_sha1(file_path):
    sha1 = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            sha1.update(chunk)
    return sha1.hexdigest()

def inspect_jar_locally(file_path):
    loaders = set()
    try:
        with zipfile.ZipFile(file_path, 'r') as z:
            filenames = z.namelist()
            if "fabric.mod.json" in filenames:
                loaders.add("fabric")
                loaders.add("quilt")
            if "quilt.mod.json" in filenames:
                loaders.add("quilt")
            if "META-INF/neoforge.mods.toml" in filenames:
                loaders.add("neoforge")
            if "META-INF/mods.toml" in filenames:
                loaders.add("forge")
            if "mcmod.info" in filenames:
                loaders.add("forge")
    except Exception:
        pass
    return list(loaders)

class FetchVersionsWorker(QThread):
    finished = Signal(list)

    def run(self):
        try:
            res = requests.get(MOJANG_MANIFEST_URL, timeout=10).json()
            versions = [v.get("id", "") for v in res.get("versions", []) if v.get("type") == "release"]
            self.finished.emit(versions)
        except Exception:
            self.finished.emit([])

class ScanWorker(QThread):
    finished = Signal(list, list, list, list, dict)
    progress = Signal(str)

    def __init__(self, mods_dir, mod_loader, mc_version):
        super().__init__()
        self.mods_dir = mods_dir
        self.mod_loader = mod_loader.lower()
        self.mc_version = mc_version

    def run(self):
        jar_files = [f for f in os.listdir(self.mods_dir) if f.endswith(".jar")]
        compatible = []
        incompatible_loader = []
        too_old = []
        too_new = []
        mod_data = {}

        for jar in jar_files:
            self.progress.emit(f"Checking {jar}...")
            jar_path = os.path.join(self.mods_dir, jar)
            
            local_loaders = inspect_jar_locally(jar_path)
            file_sha1 = get_file_sha1(jar_path)
            supported_versions = []
            supported_loaders = list(local_loaders)
            project_id = None

            try:
                res = requests.get(f"{MODRINTH_API}/version_file/{file_sha1}", timeout=5)
                if res.status_code == 200:
                    v_data = res.json()
                    supported_versions = v_data.get("game_versions", [])
                    supported_loaders = list(set(supported_loaders + [l.lower() for l in v_data.get("loaders", [])]))
                    project_id = v_data.get("project_id")
            except Exception:
                pass

            if not supported_loaders and "fabric" in jar.lower():
                supported_loaders.append("fabric")

            mod_data[jar] = {
                "versions": supported_versions,
                "loaders": supported_loaders,
                "project_id": project_id
            }

            if supported_loaders and self.mod_loader not in supported_loaders:
                incompatible_loader.append(jar)
                continue

            compatible.append(jar)

        self.finished.emit(compatible, incompatible_loader, too_old, too_new, mod_data)


class DownloaderWorker(QThread):
    progress = Signal(str)
    finished = Signal(int, list)

    def __init__(self, mods_dir, backup_dir, mod_data, target_version, target_loader):
        super().__init__()
        self.mods_dir = mods_dir
        self.backup_dir = backup_dir
        self.mod_data = mod_data
        self.target_version = target_version
        self.target_loader = target_loader.lower()

    def run(self):
        download_count = 0
        skipped_mods = []
        backed_up_files = [f for f in os.listdir(self.backup_dir) if f.endswith(".jar")]

        for jar in backed_up_files:
            self.progress.emit(f"Searching release for {jar}...")
            
            p_id = self.mod_data.get(jar, {}).get("project_id")
            if not p_id:
                clean_name = jar.replace('.jar', '').split('-')[0]
                try:
                    s_res = requests.get(f"{MODRINTH_API}/search?query={clean_name}", timeout=5).json()
                    hits = s_res.get("hits", [])
                    if hits:
                        p_id = hits[0].get("project_id")
                except Exception:
                    pass

            if not p_id:
                skipped_mods.append(jar)
                continue

            success = False
            try:
                url = f"{MODRINTH_API}/project/{p_id}/version?game_versions=[\"{self.target_version}\"]&loaders=[\"{self.target_loader}\"]"
                v_res = requests.get(url, timeout=5).json()
                if v_res:
                    files = v_res[0].get("files", [])
                    if files:
                        dl_url = files[0].get("url")
                        fn = files[0].get("filename")
                        self.progress.emit(f"Downloading {fn}...")
                        mod_data_bin = requests.get(dl_url, timeout=15).content
                        with open(os.path.join(self.mods_dir, fn), "wb") as f:
                            f.write(mod_data_bin)
                        download_count += 1
                        success = True
            except Exception:
                pass

            if not success:
                skipped_mods.append(jar)

        self.finished.emit(download_count, skipped_mods)


class UnknownModsDialog(QDialog):
    def __init__(self, skipped_mods):
        super().__init__()
        self.setWindowTitle("Unknown Mods Detected!")
        self.resize(460, 300)

        layout = QVBoxLayout(self)

        text_label = QLabel(f"We have found {len(skipped_mods)} mod/s which have not been detected on modrinth. These mods will be skipped.")
        text_label.setWordWrap(True)
        layout.addWidget(text_label)

        list_widget = QListWidget()
        list_widget.addItems(skipped_mods)
        layout.addWidget(list_widget)

        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)


class IncompatibleModsDialog(QDialog):
    def __init__(self, incompatible_mods, mod_data):
        super().__init__()
        self.setWindowTitle("Loader Compatibility Issues Found")
        self.resize(500, 340)
        self.mods = incompatible_mods
        self.mod_data = mod_data
        self.action = "cancel"
        self.target_dir = ""

        layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        header_label = QLabel("The following mods do not support your selected Mod Loader:")
        header_label.setWordWrap(True)
        help_btn = QPushButton("?")
        help_btn.setFixedWidth(30)
        help_btn.clicked.connect(self.show_explanation)

        header_row.addWidget(header_label)
        header_row.addWidget(help_btn)
        layout.addLayout(header_row)

        self.list_widget = QListWidget()
        self.list_widget.addItems(self.mods)
        layout.addWidget(self.list_widget)

        btn_box = QHBoxLayout()
        self.btn_delete = QPushButton("Delete Mods")
        self.btn_move = QPushButton("Move Elsewhere")
        self.btn_cancel = QPushButton("Cancel")

        btn_box.addWidget(self.btn_delete)
        btn_box.addWidget(self.btn_move)
        btn_box.addWidget(self.btn_cancel)
        layout.addLayout(btn_box)

        self.btn_delete.clicked.connect(self.select_delete)
        self.btn_move.clicked.connect(self.select_move)
        self.btn_cancel.clicked.connect(self.reject)

    def show_explanation(self):
        detected_summary = []
        for m in self.mods:
            loaders = self.mod_data.get(m, {}).get("loaders", [])
            loader_str = ", ".join([l.capitalize() for l in loaders]) if loaders else "Unknown"
            detected_summary.append(f"• {m}: {loader_str}")

        msg = (
            "These mods require a different mod loader.\n\n"
            "Detected loader compatibility:\n" + "\n".join(detected_summary) + "\n\n"
            "Please switch your selected Mod Loader dropdown or remove/move these files."
        )
        QMessageBox.information(self, "Loader Details", msg)

    def select_delete(self):
        self.action = "delete"
        self.accept()

    def select_move(self):
        path = QFileDialog.getExistingDirectory(self, "Select Directory to Move Incompatible Mods")
        if path:
            self.target_dir = path
            self.action = "move"
            self.accept()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Minecraft Mod Loader & Migrator")
        self.resize(520, 520)

        self.mods_dir = ""
        self.backup_dir = ""

        self.layout = QVBoxLayout(self)

        row1 = QHBoxLayout()
        self.mods_btn = QPushButton("Select Minecraft Mods Directory")
        self.help_mods_btn = QPushButton("?")
        self.help_mods_btn.setFixedWidth(30)
        row1.addWidget(self.mods_btn)
        row1.addWidget(self.help_mods_btn)
        self.layout.addLayout(row1)
        self.mods_label = QLabel("No directory selected")
        self.layout.addWidget(self.mods_label)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Select Mod Loader:"))
        self.help_loader_btn = QPushButton("?")
        self.help_loader_btn.setFixedWidth(30)
        row2.addWidget(self.help_loader_btn)
        self.layout.addLayout(row2)

        self.loader_combo = QComboBox()
        self.loader_combo.addItems(["-- Select --", "Fabric", "Forge", "Quilt", "NeoForge"])
        self.layout.addWidget(self.loader_combo)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Select Minecraft Version:"))
        self.help_version_btn = QPushButton("?")
        self.help_version_btn.setFixedWidth(30)
        row3.addWidget(self.help_version_btn)
        self.layout.addLayout(row3)

        self.version_combo = QComboBox()
        self.version_combo.addItem("Loading versions...")
        self.layout.addWidget(self.version_combo)

        row4 = QHBoxLayout()
        self.backup_btn = QPushButton("Select Directory for Previous Mods")
        self.help_backup_btn = QPushButton("?")
        self.help_backup_btn.setFixedWidth(30)
        row4.addWidget(self.backup_btn)
        row4.addWidget(self.help_backup_btn)
        self.layout.addLayout(row4)
        self.backup_label = QLabel("No directory selected")
        self.layout.addWidget(self.backup_label)

        # Checkbox: Use regular launcher
        row5 = QHBoxLayout()
        self.launcher_checkbox = QCheckBox("Use regular minecraft launcher")
        self.help_launcher_btn = QPushButton("?")
        self.help_launcher_btn.setFixedWidth(30)
        row5.addWidget(self.launcher_checkbox)
        row5.addWidget(self.help_launcher_btn)
        self.layout.addLayout(row5)

        self.status_label = QLabel("")
        self.layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        self.reset_btn = QPushButton("Reset All")
        self.process_btn = QPushButton("Process and Install")
        self.process_btn.setEnabled(False)
        self.process_btn.setStyleSheet("background-color: gray; color: white;")
        
        btn_row.addWidget(self.reset_btn)
        btn_row.addWidget(self.process_btn)
        self.layout.addLayout(btn_row)

        self.mods_btn.clicked.connect(self.browse_mods_dir)
        self.backup_btn.clicked.connect(self.browse_backup_dir)
        self.loader_combo.currentIndexChanged.connect(self.validate_form)
        self.version_combo.currentIndexChanged.connect(self.validate_form)
        self.reset_btn.clicked.connect(self.reset_form)
        self.process_btn.clicked.connect(self.start_processing)

        self.help_mods_btn.clicked.connect(lambda: QMessageBox.information(self, "Help", "Select your '.minecraft/mods' directory."))
        self.help_loader_btn.clicked.connect(lambda: QMessageBox.information(self, "Help", "Choose your mod loader (Fabric, Forge, etc.)."))
        self.help_version_btn.clicked.connect(lambda: QMessageBox.information(self, "Help", "Select your targeted Minecraft version."))
        self.help_backup_btn.clicked.connect(lambda: QMessageBox.information(self, "Help", "Select target backup directory for current mods."))
        self.help_launcher_btn.clicked.connect(lambda: QMessageBox.information(self, "Help", "Only select this if you use the regular minecraft launcher and not something else like Lunar client"))

        self.load_minecraft_versions()

    def load_minecraft_versions(self):
        self.version_worker = FetchVersionsWorker()
        self.version_worker.finished.connect(self.populate_versions)
        self.version_worker.start()

    def populate_versions(self, versions):
        self.version_combo.clear()
        self.version_combo.addItem("-- Select --")
        if versions:
            self.version_combo.addItems(versions)
        else:
            self.version_combo.addItems(["1.21.11", "1.21.4", "1.21.1", "1.20.1"])
        self.validate_form()

    def browse_mods_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Select Minecraft Mods Directory")
        if path:
            self.mods_dir = path
            self.mods_label.setText(path)
            self.validate_form()

    def browse_backup_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Select Backup Directory")
        if path:
            self.backup_dir = path
            self.backup_label.setText(path)
            self.validate_form()

    def reset_form(self):
        self.mods_dir = ""
        self.backup_dir = ""
        self.mods_label.setText("No directory selected")
        self.backup_label.setText("No directory selected")
        self.loader_combo.setCurrentIndex(0)
        self.version_combo.setCurrentIndex(0)
        self.launcher_checkbox.setChecked(False)
        self.status_label.setText("")
        self.validate_form()

    def validate_form(self):
        mods_dir = getattr(self, "mods_dir", "")
        backup_dir = getattr(self, "backup_dir", "")
        valid = (
            bool(mods_dir) and 
            bool(backup_dir) and 
            self.loader_combo.currentIndex() > 0 and 
            self.version_combo.currentIndex() > 0
        )
        self.process_btn.setEnabled(valid)
        if valid:
            self.process_btn.setStyleSheet("background-color: green; color: white; font-weight: bold;")
        else:
            self.process_btn.setStyleSheet("background-color: gray; color: white;")

    def start_processing(self):
        if not os.path.exists(self.mods_dir):
            QMessageBox.critical(self, "Error", "Selected Mods directory does not exist!")
            return

        self.process_btn.setEnabled(False)
        self.status_label.setText("Scanning mods for compatibility...")
        
        self.worker = ScanWorker(self.mods_dir, self.loader_combo.currentText(), self.version_combo.currentText())
        self.worker.progress.connect(lambda msg: self.status_label.setText(msg))
        self.worker.finished.connect(self.handle_scan_results)
        self.worker.start()

    def handle_scan_results(self, compatible, incompatible_loader, too_old, too_new, mod_data):
        if incompatible_loader:
            dialog = IncompatibleModsDialog(incompatible_loader, mod_data)
            if dialog.exec():
                if dialog.action == "delete":
                    for mod in incompatible_loader:
                        os.remove(os.path.join(self.mods_dir, mod))
                elif dialog.action == "move" and dialog.target_dir:
                    for mod in incompatible_loader:
                        shutil.move(os.path.join(self.mods_dir, mod), os.path.join(dialog.target_dir, mod))
            else:
                self.status_label.setText("Cancelled by user.")
                self.validate_form()
                return

        for item in os.listdir(self.mods_dir):
            src_path = os.path.join(self.mods_dir, item)
            if os.path.isfile(src_path):
                shutil.move(src_path, os.path.join(self.backup_dir, item))

        self.dl_worker = DownloaderWorker(
            self.mods_dir, 
            self.backup_dir, 
            mod_data, 
            self.version_combo.currentText(), 
            self.loader_combo.currentText()
        )
        self.dl_worker.progress.connect(lambda msg: self.status_label.setText(msg))
        self.dl_worker.finished.connect(self.handle_download_complete)
        self.dl_worker.start()

    def handle_download_complete(self, count, skipped_mods):
        if skipped_mods:
            dialog = UnknownModsDialog(skipped_mods)
            dialog.exec()

        self.status_label.setText("Complete!")
        QMessageBox.information(self, "Success", f"Migration complete! Downloaded {count} updated mod files for your selected version.")

        # Prompt for Mod Loader Installer if Option Checked
        if self.launcher_checkbox.isChecked():
            selected_loader = self.loader_combo.currentText().lower()
            selected_version = self.version_combo.currentText()
            
            reply = QMessageBox.question(
                self, 
                "Download Mod Loader", 
                f"Would you like to open the official installer page for {self.loader_combo.currentText()} ({selected_version})?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                url = LOADER_INSTALLER_URLS.get(selected_loader, "https://fabricmc.net/use/installer/")
                webbrowser.open(url)

        self.validate_form()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())