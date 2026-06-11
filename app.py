# -*- coding: utf-8 -*-
"""B2B订单主数据 成本/毛利 自动回填云服务 (Zeabur)。
阿华在「B2B订单主数据」录新单 → 每日 cron POST /recompute →
按 SKU 映射领星 cg_price 填 采购成本RMB, 按最后出货月领星汇率折 RMB 算 综合毛利RMB。
口径: 产品金额(原币)=数量×单价; 采购成本RMB=领星cg×数量(cg是RMB);
综合毛利RMB=产品金额×月汇率−采购成本−物流成本(若已填)。币种 USD/EUR/RMB。
幂等(每次全表重算只在变化时写回)。env: FEISHU_APP_ID/SECRET / LX_APP_ID/LX_SECRET / AUTH_TOKEN
"""
import os, json, time, hashlib, base64, datetime
import requests
from Crypto.Cipher import AES
from fastapi import FastAPI, Request, HTTPException

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
LX_APP_ID = os.environ.get("LX_APP_ID", "ak_B1P0qz2mkImfS")
LX_SECRET = os.environ.get("LX_SECRET", "IMJm0f/dwDM7YYR+2FrlEQ==")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
FEISHU = "https://open.feishu.cn/open-apis"
LXHOST = "https://openapi.lingxing.com"
APP = "E1kkbx1tVaJvQGsKf94cJG88nzb"; OT = "tblRbqG4VcTE6qCX"
FRANKIE = "ou_629ce01f4bc31de078e10fcb038dbf78"
SKU_FIX = {}  # B2B SKU 基本=领星; 如有不一致在此补
app = FastAPI()


def tok():
    return requests.post(f"{FEISHU}/auth/v3/tenant_access_token/internal",
                         json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=20).json()["tenant_access_token"]


def num(x):
    try: return float(x)
    except Exception: return 0.0


def ms_to_ym(ms):
    try: return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m")
    except Exception: return None


def gv(f, k):
    v = f.get(k)
    if v is None: return ""
    if isinstance(v, list) and v:
        x = v[0]; return x.get("text", "") if isinstance(x, dict) else str(x)
    if isinstance(v, dict): return v.get("text") or v.get("value") or ""
    return v


def getall(T):
    items = []; pt = None
    while True:
        u = f"{FEISHU}/bitable/v1/apps/{APP}/tables/{OT}/records?page_size=500" + (f"&page_token={pt}" if pt else "")
        d = requests.get(u, headers={"Authorization": f"Bearer {T}"}, timeout=30).json().get("data", {})
        items += d.get("items") or []; pt = d.get("page_token")
        if not d.get("has_more"): break
    return items


# ===== 领星 cg + 月度汇率 =====
def _lx_sign(p):
    ks = sorted(k for k in p if p[k] not in ('', None))
    s = "&".join(f"{k}={p[k]}" for k in ks)
    md5 = hashlib.md5(s.encode()).hexdigest().upper()
    key = LX_APP_ID.encode()[:16].ljust(16, b'\0')
    c = AES.new(key, AES.MODE_ECB); pad = 16 - len(md5) % 16
    return base64.b64encode(c.encrypt(md5.encode() + bytes([pad]) * pad)).decode()


def _lx():
    body = "appId=" + LX_APP_ID + "&appSecret=" + requests.utils.quote(LX_SECRET, safe="")
    access = requests.post(f"{LXHOST}/api/auth-server/oauth/access-token", data=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30).json()["data"]["access_token"]

    def post(path, b):
        ts = int(time.time()); base = {"access_token": access, "app_key": LX_APP_ID, "timestamp": ts}; sp = dict(base)
        for k, v in b.items(): sp[k] = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
        qs = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in {**base, "sign": _lx_sign(sp)}.items())
        return requests.post(f"{LXHOST}{path}?{qs}", data=json.dumps(b), headers={"Content-Type": "application/json"}, timeout=60).json()
    return post


def lx_cg(post):
    cg = {}; off = 0
    while True:
        d = (post("/erp/sc/routing/data/local_inventory/productList", {"offset": off, "length": 200}).get("data") or [])
        for it in d:
            if it.get("sku"): cg[it["sku"]] = num(it.get("cg_price"))
        if len(d) < 200: break
        off += 200; time.sleep(0.2)
    return cg


def do_recompute():
    T = tok(); post = _lx(); cg = lx_cg(post)
    fxc = {}

    def fx(cur, ym):
        if cur == "RMB" or not cur: return 1.0
        k = (cur, ym)
        if k in fxc: return fxc[k]
        rate = 0
        try:
            for x in (post("/erp/sc/routing/finance/currency/currencyMonth", {"date": ym}).get("data") or []):
                if x.get("code") == cur: rate = num(x.get("my_rate"))
        except Exception: pass
        fxc[k] = rate; time.sleep(0.15); return rate

    rows = getall(T)
    updates = []; miss = set(); skip_fx = 0
    for r in rows:
        f = r["fields"]
        qty = num(gv(f, "数量")); price = num(gv(f, "单价(原币)")); sku = str(gv(f, "SKU") or "").strip()
        cur = str(gv(f, "币种") or "").strip()
        ym = ms_to_ym(f.get("最后出货日期")) or ms_to_ym(f.get("订单日期"))
        amt = round(qty * price, 2)
        es = SKU_FIX.get(sku, sku)
        cgv = cg.get(es)
        if sku and cgv is None: miss.add(sku)
        cg_rmb = round((cgv or 0) * qty, 2)
        rate = fx(cur, ym) if cur and cur != "RMB" else 1.0
        if cur and cur != "RMB" and not rate: skip_fx += 1
        wl = num(gv(f, "物流成本RMB(待接)"))
        custom = num(gv(f, "报关费RMB"))  # 报关费(大客户出口) 计入我方成本
        ml = round(amt * rate - cg_rmb - wl - custom, 2)
        cur_amt = num(gv(f, "产品金额(原币)")); cur_cg = num(gv(f, "采购成本RMB(领星自动)")); cur_ml = num(gv(f, "综合毛利RMB(自动)"))
        nf = {}
        if abs(cur_amt - amt) > 0.01 and amt: nf["产品金额(原币)"] = amt
        if abs(cur_cg - cg_rmb) > 0.01: nf["采购成本RMB(领星自动)"] = cg_rmb
        if abs(cur_ml - ml) > 0.01: nf["综合毛利RMB(自动)"] = ml
        if nf: updates.append({"record_id": r["record_id"], "fields": nf})
    nw = 0
    for i in range(0, len(updates), 100):
        rr = requests.post(f"{FEISHU}/bitable/v1/apps/{APP}/tables/{OT}/records/batch_update",
                           headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                           json={"records": updates[i:i + 100]}, timeout=40).json()
        nw += len(rr.get("data", {}).get("records", [])); time.sleep(0.3)
    out = {"rows": len(rows), "updated": nw, "missing_sku": sorted(miss), "fx_missing_rows": skip_fx}
    # 有新 SKU 领星查不到 → 提醒 Frankie(否则成本算0毛利虚高)
    if miss:
        try:
            requests.post(f"{FEISHU}/im/v1/messages?receive_id_type=open_id",
                          headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
                          json={"receive_id": FRANKIE, "msg_type": "text",
                                "content": json.dumps({"text": f"🟡 [FIN·P2] B2B订单成本自动回填\n⚠️ 这些SKU领星查不到cg(成本按0算,毛利虚高),请核对: {sorted(miss)}"}, ensure_ascii=False)}, timeout=20)
        except Exception: pass
    return out


@app.get("/health")
def health(): return {"ok": True}


@app.post("/recompute")
async def recompute(request: Request):
    if AUTH_TOKEN and request.headers.get("Authorization") != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(401, "unauthorized")
    return do_recompute()
