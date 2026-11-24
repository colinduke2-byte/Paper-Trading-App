import sys, tempfile, json; sys.path[:0]=["/home/user/wt/review-orders/backend","/home/user/wt/review-orders/backend/tests"]
from test_multileg import *
from app.core.clock import SimClock
from app.marketdata.feed import SimFeed
from datetime import date, datetime, time
def us(tmp=None):
    clock=SimClock(datetime(2026,11,24,8,0,tzinfo=cal.NY).timestamp()); feed=SimFeed()
    ex=Exchange(feed, clock, data_dir=tmp, session_mode="us"); feed.set_quote("ABC",D("99.99"),D("100"),D("100")); ex.tick()
    return ex,feed,clock
def at(clock, h, m, d=24): clock.set(datetime(2026,11,d,h,m,tzinfo=cal.NY).timestamp())
ex,feed,clock=us(); a=acct(ex)
moo=ex.submit(a,{"symbol":"ABC","side":"buy","qty":5,"type":"market_on_open"})[0]
moc=ex.submit(a,{"symbol":"ABC","side":"buy","qty":5,"type":"market_on_close"})[0]
at(clock,9,30); feed.set_quote("ABC",D("100.99"),D("101"),D("101")); ex.tick()
print("MOO at open:", moo.status, moo.avg_fill_price, ex.session())
at(clock,15,59); feed.set_quote("ABC",D("104.99"),D("105"),D("105")); ex.tick()
at(clock,16,0); feed.set_quote("ABC",D("109.99"),D("110"),D("110")); ex.tick()
print("MOC at close:", moc.status, moc.avg_fill_price, ex.session())
# Missed auction across restart
tmp=tempfile.mkdtemp()
ex,feed,clock=us(tmp); a=acct(ex)
moc=ex.submit(a,{"symbol":"ABC","side":"buy","qty":5,"type":"market_on_close"})[0]
at(clock,15,0); ex.tick(); ex.save(force=True)
ex2=Exchange(feed, clock, data_dir=tmp, session_mode="us"); ex2.load()
at(clock,16,30); ex2.tick()
print("MOC after restart over the close:", ex2.engine.orders[moc.id].status)
at(clock,15,0,25); ex2.tick(); at(clock,16,1,25); ex2.tick()
print("next day:", ex2.engine.orders[moc.id].status, ex2.engine.orders[moc.id].updated_at)
# persistence of new fields
ex,feed,clock=make_exchange(tmp=tempfile.mkdtemp()); a=acct(ex)
o=ex.submit(a,{"legs":BULL_CALL,"qty":1,"type":"limit","limit_price":"0.05","price_effect":"debit","tif":"gtd","good_till_date":str(FAR)})[0]
oo=ex.submit(a,{"symbol":"ABC","side":"buy","qty":1,"type":"limit","limit_price":"50","tif":"gtc","triggers":[{"symbol":"ABC","side":"buy","qty":3,"type":"limit","limit_price":"40"}]})
ex.save(force=True)
ex2=Exchange(feed, clock, data_dir=ex.data_dir); ex2.load()
r=ex2.engine.orders[o.id]; print("restored", r.legs, r.limit_price, type(r.limit_price), r.expire_at, r.good_till, r.status)
rc=ex2.engine.orders[oo[1].id]; print("child", rc.role, rc.status, rc.parent_id, type(rc.qty))
print("legs ratio type", type(r.legs[0]["ratio"]))
# old state file without fields
p=ex.data_dir+"/state.json"; d=json.load(open(p))
for od in d["orders"]:
    for k in ("legs","expire_at","good_till"): od.pop(k,None)
json.dump(d,open(p,"w")); ex3=Exchange(feed,clock,data_dir=ex.data_dir); print("old load", ex3.load())
