import os
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# La importación de main.py inicializa la base de datos; aislarla del entorno real.
os.environ.setdefault("DB_ENCRYPTION_KEY", "pytest-key-32-bytes-minimum-length!")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), f"memoricorde-pytest-bootstrap-{os.getpid()}.db"))
os.environ.setdefault("ENTORNO", "desarrollo")
