# Project commands

- Install Python dependencies in a virtual environment with `pip install -r requirements.txt`.
- Run the API with `API_PORT=8003 python3 main.py`; port 8000 may be used by another service on this server.
- Run isolated regression tests with `python3 -m unittest test_multi_reader -v`. These tests mock dotenv loading and do not contact physical readers.
- `test_reader.py` is a hardware/Excel utility, not a unit-test suite; do not run it as an automated verification step.
- Check whitespace with `git diff --check`.
