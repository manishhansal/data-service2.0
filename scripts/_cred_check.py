"""Credential readiness check — prints status only, NEVER prints secret values."""
import os, sys, datetime, base64, json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from dotenv import dotenv_values
    env = dotenv_values(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.local"))
    for k, v in env.items():
        if v:
            os.environ.setdefault(k, v)
except Exception as e:
    print("dotenv load error:", e)


def C(v):
    return "CONFIGURED" if v else "MISSING"


ao_key  = os.environ.get("ANGEL_ONE_API_KEY", "")
ao_cid  = os.environ.get("ANGEL_ONE_CLIENT_ID", "")
ao_totp = os.environ.get("ANGEL_ONE_TOTP_SECRET", "")
ao_mpin = os.environ.get("ANGEL_ONE_MPIN", "")

up_key    = os.environ.get("UPSTOX_API_KEY", "")
up_secret = os.environ.get("UPSTOX_API_SECRET", "")
up_redir  = os.environ.get("UPSTOX_REDIRECT_URI", "")
up_akey   = os.environ.get("UPSTOX_ANALYTICS_KEY", "")
up_atok   = os.environ.get("UPSTOX_ACCESS_TOKEN", "")

print("=== ANGEL ONE CREDENTIALS ===")
print("SMARTAPI_API_KEY  :", C(ao_key))
print("CLIENT_CODE       :", C(ao_cid))
print("TOTP_SECRET       :", C(ao_totp))
print("PIN (MPIN)        :", C(ao_mpin))
all_angel = all([ao_key, ao_cid, ao_totp, ao_mpin])
print("credential_complete  :", all_angel)
print("authentication_possible:", all_angel)

print()
print("=== UPSTOX CREDENTIALS ===")
print("CLIENT_ID (API_KEY)  :", C(up_key))
print("CLIENT_SECRET        :", C(up_secret))
print("REDIRECT_URI         :", C(up_redir))
print("UPSTOX_ANALYTICS_KEY :", C(up_akey))
print("UPSTOX_ACCESS_TOKEN  :", C(up_atok))
all_upstox = all([up_key, up_secret, up_redir])
print("credential_complete  :", all_upstox)
print("auth_possible        :", all_upstox)


def decode_jwt_meta(tok, label):
    try:
        payload = tok.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        d = json.loads(base64.b64decode(payload))
        exp_dt = datetime.datetime.fromtimestamp(d["exp"], tz=datetime.timezone.utc)
        iat_dt = datetime.datetime.fromtimestamp(d["iat"], tz=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        status = "VALID" if exp_dt > now else "EXPIRED"
        days = (exp_dt - now).days
        print(f"  expiry_date     : {exp_dt.date().isoformat()}")
        print(f"  issued_date     : {iat_dt.date().isoformat()}")
        print(f"  status          : {status}")
        print(f"  days_remaining  : {days}")
        print(f"  subject         : {d.get('sub', 'UNKNOWN')}")
        for k2 in ["isPlusPlan", "isExtended", "isMultiClient"]:
            if k2 in d:
                print(f"  {k2}   : {d[k2]}")
        return status
    except Exception as e:
        print(f"  decode_error    : {e}")
        return "UNKNOWN"


print()
print("=== TOKEN EXPIRY ANALYSIS ===")
if up_akey:
    print("[UPSTOX ANALYTICS TOKEN]")
    decode_jwt_meta(up_akey, "analytics")
if up_atok:
    print("[UPSTOX ACCESS TOKEN]")
    decode_jwt_meta(up_atok, "access")

print()
print("=== PROVIDER ACCOUNT DETERMINATION ===")
if up_akey:
    try:
        payload = up_akey.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        d = json.loads(base64.b64decode(payload))
        print("Upstox account sub  :", d.get("sub", "UNKNOWN"))
        print("isPlusPlan          :", d.get("isPlusPlan", False))
    except Exception:
        pass
print("Angel One account   :", ao_cid if ao_cid else "UNKNOWN")
