from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pymongo import MongoClient
import firebase_admin
from firebase_admin import credentials, auth
import os, json, datetime

app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # luego lo restringimos a tu dominio/app
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# Variables de entorno:
# MONGO_URI: cadena de conexión de MongoDB Atlas
# FIREBASE_SERVICE_ACCOUNT: contenido JSON de la cuenta de servicio Firebase
MONGO_URI = os.environ["MONGO_URI"]
SA_JSON = os.environ["FIREBASE_SERVICE_ACCOUNT"]

# Firebase Admin para verificar ID tokens
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(SA_JSON))
    firebase_admin.initialize_app(cred)

# MongoDB
client = MongoClient(MONGO_URI)
db = client["moneyzen"]
col_accounts = db["accounts"]
col_transactions = db["transactions"]
col_summaries = db["summaries"]

def month_key(d: datetime.datetime) -> str:
    return f"{d.year}-{str(d.month).zfill(2)}"

def now_utc() -> datetime.datetime:
    return datetime.datetime.utcnow()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/tx-create")
async def tx_create(req: Request):
    # 1) Verificar ID token
    auth_header = req.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta token")
    id_token = auth_header.split("Bearer ")[1]
    try:
        decoded = auth.verify_id_token(id_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")
    uid = decoded["uid"]

    # 2) Datos
    body = await req.json()
    account_id = body.get("accountId")
    amount = body.get("amount")
    tx_type = body.get("type")   # "in" | "out"
    category = body.get("category")
    date_iso = body.get("date")  # opcional ISO
    if not account_id or not isinstance(amount, (int,float)) or tx_type not in ("in","out"):
        raise HTTPException(status_code=400, detail="Datos inválidos")

    when = datetime.datetime.fromisoformat(date_iso) if date_iso else now_utc()

    # 3) Insert transacción
    tx_doc = {
        "uid": uid,
        "accountId": account_id,
        "amount": float(amount),
        "type": tx_type,
        "category": category or None,
        "date": when,
        "createdAt": now_utc(),
    }
    result = col_transactions.insert_one(tx_doc)
    tx_id = str(result.inserted_id)

    # 4) Actualizar saldo
    delta = amount if tx_type == "in" else -amount
    col_accounts.update_one(
        {"_id": account_id, "uid": uid},
        {"$inc": {"balance": delta}, "$set": {"updatedAt": now_utc()}},
        upsert=True,
    )

    # 5) Actualizar resumen mensual
    key = month_key(when)
    summary = col_summaries.find_one({"uid": uid, "month": key}) or {
        "uid": uid, "month": key, "income": 0, "expense": 0, "txCount": 0,
        "byCategoryIn": {}, "byCategoryOut": {}
    }
    if tx_type == "in":
        summary["income"] += amount
        if category:
            summary["byCategoryIn"][category] = summary["byCategoryIn"].get(category, 0) + amount
    else:
        summary["expense"] += amount
        if category:
            summary["byCategoryOut"][category] = summary["byCategoryOut"].get(category, 0) + amount
    summary["txCount"] += 1
    summary["updatedAt"] = now_utc()
    col_summaries.update_one({"uid": uid, "month": key}, {"$set": summary}, upsert=True)

    return JSONResponse({"ok": True, "txId": tx_id})


# --- TEST: conexión a MongoDB ---
import os
from pymongo import MongoClient

@app.get("/health-db")
def health_db():
    try:
        mongo_uri = os.environ.get("MONGO_URI")
        if not mongo_uri:
            return {"ok": False, "error": "MONGO_URI no está definida"}

        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        # Fuerza un ping al cluster
        client.admin.command("ping")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
