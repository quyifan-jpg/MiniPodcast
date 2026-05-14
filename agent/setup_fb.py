import sys
import os

# Ensure the current directory is in the python path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from tools.social.browser import setup_session  # noqa: E402

if __name__ == "__main__":
    print("Starting Facebook session setup...")
    print("This will open a browser window. Please log in to Facebook manually.")
    print("Once logged in, close the browser or press Ctrl+C here to save the session.")
    setup_session("https://facebook.com")
