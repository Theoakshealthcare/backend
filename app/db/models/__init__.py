import os
import importlib
import pkgutil
from app.db.session import Base

# Automatically import all models in this directory
models_dir = os.path.dirname(__file__)
for module_info in pkgutil.iter_modules([models_dir]):
    module_name = module_info.name
    importlib.import_module(f"app.db.models.{module_name}")