import sys; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
ex, feed, clock = make_exchange(XYZ="50", ABC="100"); a = acct(ex)
c = TestClient(create_app(ex, run_loop=False), raise_server_exceptions=False)
L=[leg(C100,"buy"),leg(C105,"sell")]
cases = {
 "nan qty": {"legs":L,"qty":"NaN"}, "huge qty": {"legs":L,"qty":"1e400"}, "zero qty":{"legs":L,"qty":0},
 "neg qty": {"legs":L,"qty":-1}, "frac qty":{"legs":L,"qty":1.5}, "ratio nan":{"legs":[dict(L[0],ratio="NaN"),L[1]]},
 "ratio 0":{"legs":[dict(L[0],ratio=0),L[1]]}, "ratio list":{"legs":[dict(L[0],ratio=[1]),L[1]]},
 "ratio 1.0":{"legs":[dict(L[0],ratio=1.0),L[1]]},
 "legs str":{"legs":"abc"}, "legs dict":{"legs":{"a":1}}, "5 legs":{"legs":[leg(s,"buy") for s in (C95,C100,C105,C110,P90)]},
 "1 leg":{"legs":[L[0]]}, "dup":{"legs":[L[0],dict(L[0],side="sell")]}, "diff und":{"legs":[L[0],leg(opt("C",50,underlying="XYZ"),"sell")]},
 "unknown":{"legs":[L[0],leg("ZZZ261127C00100000","sell")]}, "symbol int":{"legs":[L[0],{"symbol":5,"side":"sell"}]},
 "symbol None":{"legs":[L[0],{"symbol":None,"side":"sell"}]},
 "side bad":{"legs":[L[0],{"symbol":C105,"side":"short"}]}, "limit nan":{"legs":L,"type":"limit","limit_price":"nan","price_effect":"debit"},
 "limit huge":{"legs":L,"type":"limit","limit_price":"1e30","price_effect":"debit"}, "limit no effect":{"legs":L,"type":"limit","limit_price":"1"},
 "limit inf":{"legs":L,"type":"limit","limit_price":"Infinity","price_effect":"debit"},
 "gtd bad":{"legs":L,"tif":"gtd","good_till_date":20261231}, "gtd list":{"legs":L,"tif":"gtd","good_till_date":[1]},
 "gtd compact":{"legs":L,"tif":"gtd","good_till_date":"20261211"},
 "tif list":{"legs":L,"tif":["day"]}, "type dict":{"legs":L,"type":{}}, "effect weird":{"legs":L,"type":"limit","limit_price":"1","price_effect":"both"},
 "two stock legs":{"legs":[leg("ABC","buy",100),leg("ABC","sell",100)]}, "stock only + opt":{"legs":[leg("ABC","buy",100),leg(C105,"sell")]},
 "client_id dict":{"legs":L,"client_id":{"x":1}},
 "extended":{"legs":L,"extended_hours":True},
}
for name, body in cases.items():
    for path in ("multileg/preview","multileg"):
        r = c.post(f"/api/accounts/{a}/{path}", json=body)
        if r.status_code >= 500 or (r.status_code<300 and name not in ("ratio 1.0","stock only + opt","gtd compact","client_id dict")):
            print(name, path, r.status_code, r.text[:150])
    r = c.post(f"/api/accounts/{a}/orders", json=body)
    if r.status_code>=500: print("orders route", name, r.status_code, r.text[:120])
# oco/oto invalid
for name, body in {"oco str":{"oco":"x"}, "oco 1":{"oco":[{"symbol":"ABC","side":"buy","qty":1}]}, "oco 6":{"oco":[{"symbol":"ABC","side":"buy","qty":1,"type":"limit","limit_price":"1","tif":"gtc"}]*6},
  "triggers dict":{"symbol":"ABC","side":"buy","qty":1,"triggers":{"a":1}}, "oco nested":{"oco":[{"oco":[]},{"symbol":"ABC","side":"buy","qty":1}]},
  "oto child legs":{"symbol":"ABC","side":"buy","qty":1,"triggers":[{"legs":L}]}, "oco legs":{"oco":[{"legs":L},{"legs":L}]},
  "body list":[1,2], "moo opt":{"symbol":C100,"side":"buy","qty":1,"type":"market_on_open"},
  "loo no limit":{"symbol":"ABC","side":"buy","qty":1,"type":"limit_on_open"}, "gtd past":{"symbol":"ABC","side":"buy","qty":1,"tif":"gtd","good_till_date":"2020-01-01"},
  "oco multileg child":{"oco":[{"symbol":"ABC","side":"buy","qty":1,"type":"limit","limit_price":"1","tif":"gtc"},{"symbol":"ABC","side":"buy","qty":1,"type":"market_on_close"}]},
  }.items():
    r = c.post(f"/api/accounts/{a}/orders", json=body); print(name, r.status_code, r.text[:140])
