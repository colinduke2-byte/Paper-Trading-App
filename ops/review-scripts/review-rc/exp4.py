import sys, os, json
sys.path.insert(0, "/home/user/wt/review-rc/backend")
d = sys.argv[1]
os.environ.update(PAPERDESK_DATA=d, PAPERDESK_FEED="synthetic")
from app.api.server import default_exchange
print("files before:", sorted(os.listdir(d)), "state bytes", os.path.getsize(d + "/state.json"))
ex = default_exchange()
print("accounts:", [(a.id, str(a.cash), len(a.fills)) for a in ex.accounts.values()], "orders", len(ex.engine.orders))
ex.tick(); ex.save()
print("files after first tick+save:", sorted(os.listdir(d)), "state bytes", os.path.getsize(d + "/state.json"))
