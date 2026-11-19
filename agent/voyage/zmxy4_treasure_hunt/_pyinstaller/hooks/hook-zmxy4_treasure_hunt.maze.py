"""将每周地图与遮罩保留在 Python 包内的对应位置。"""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("zmxy4_treasure_hunt.maze", includes=["maps/*.json", "assets/*.png"])
