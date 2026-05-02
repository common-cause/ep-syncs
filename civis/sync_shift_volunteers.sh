#!/bin/bash
# Civis Container Script body for the shift volunteers sync.
# Paste the body of this file (without the shebang) into the Civis
# command field. See civis/SCHEDULED_SCRIPTS.md for the full job
# setup spec (docker image, credentials, etc.).
pip install git+https://github.com/common-cause/ccef_connections.git
python app/sync_shift_volunteers.py
