How to Use This for Your Video
Create a New Folder: Make a new folder on your computer (e.g., netbox-sync-demo).

Save the Files: Save the 3 code blocks above into that folder with their correct names.

Create Your .env: Rename .env.example to .env and fill it with your actual NetBox and switch credentials.

Install Libraries: Open your terminal in that folder and run:

Bash

# Create a virtual environment (Good Practice!)
python -m venv venv
source venv/bin/activate  # (On Windows: venv\Scripts\activate)

# Install requirements
pip install -r requirements.txt
Run the Demo: Edit video_sync_demo.py and change the DEMO_SWITCH_NAME and NETBOX_SITE_SLUG variables at the top to match your environment.

Run it!

Bash

python video_sync_demo.py
