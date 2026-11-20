"""Start the VDF QA API with `python main.py`."""
import os

import uvicorn

from vdf_qa.api import create_app
from vdf_qa.config import Settings


if __name__ == "__main__":
    settings = Settings.from_env()
    print(f"MongoDB connection string: {settings.mongo_uri}", flush=True)
    uvicorn.run(
        create_app(settings),
        host=os.getenv("VDF_HOST", "0.0.0.0"),
        port=int(os.getenv("VDF_PORT", "8080")),
    )
