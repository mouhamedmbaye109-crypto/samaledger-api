import os, time
import bcrypt, jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer

SECRET = os.environ["JWT_SECRET"]
if len(SECRET) < 32:
    raise RuntimeError("JWT_SECRET doit contenir au moins 32 caractères")
bearer = HTTPBearer()


def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def check_pw(p: str, h: str) -> bool:
    return bcrypt.checkpw(p.encode(), h.encode())


def make_token(u) -> str:
    return jwt.encode({"sub": str(u["id"]), "cid": str(u["company_id"]), "role": u["role"],
                       "exp": int(time.time()) + 8 * 3600}, SECRET, "HS256")


def current_user(cred=Depends(bearer)):
    try:
        return jwt.decode(cred.credentials, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Jeton invalide ou expiré")


def require(*roles):
    def dep(u=Depends(current_user)):
        if u["role"] not in roles:
            raise HTTPException(403, "Droits insuffisants")
        return u
    return dep
