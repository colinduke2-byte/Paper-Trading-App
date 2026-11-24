"""Open the default browser once the PaperDesk server answers. Used by run.bat. Usage: open_when_ready.py URL"""
import sys
import time
import urllib.error
import urllib.request
import webbrowser

url = sys.argv[1].rstrip("/")
for _ in range(240):  # up to two minutes
    try:
        urllib.request.urlopen(url + "/api/status", timeout=2).close()
        webbrowser.open(url)
        break
    except urllib.error.HTTPError:  # 401: the server is up and asks for the passcode
        webbrowser.open(url)
        break
    except Exception:
        time.sleep(0.5)
