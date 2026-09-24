import requests

ENV_PATH = ".env"

with open(ENV_PATH) as f:
    lines = f.readlines()

env = {}
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        continue
    k, v = stripped.split("=", 1)
    env[k.strip()] = v.strip()

def find_key(*needles):
    for k in env:
        if all(n.lower() in k.lower() for n in needles):
            return k
    return None

app_key_var = find_key("app", "key")
app_secret_var = find_key("app", "secret")
refresh_var = "TIKTOK_REFRESH_TOKEN"
access_var = "TIKTOK_ACCESS_TOKEN"

if not app_key_var or not app_secret_var:
    print(f"Could not auto-detect app_key/app_secret vars in .env. Found keys: {list(env.keys())}")
    raise SystemExit(1)

print(f"Using {app_key_var} and {app_secret_var} for auth.")

resp = requests.get(
    "https://auth.tiktok-shops.com/api/v2/token/refresh",
    params={
        "app_key": env[app_key_var],
        "app_secret": env[app_secret_var],
        "refresh_token": env[refresh_var],
        "grant_type": "refresh_token",
    },
)
data = resp.json()

if data.get("code") != 0:
    print("FAILED:", data)
    raise SystemExit(1)

new_access = data["data"]["access_token"]
new_refresh = data["data"]["refresh_token"]
print(f"SUCCESS. New access_token starts with: {new_access[:10]}...")
print(f"New refresh_token starts with: {new_refresh[:10]}...")

new_lines = []
for line in lines:
    stripped = line.strip()
    if stripped.startswith(f"{access_var}="):
        new_lines.append(f"{access_var}={new_access}\n")
    elif stripped.startswith(f"{refresh_var}="):
        new_lines.append(f"{refresh_var}={new_refresh}\n")
    else:
        new_lines.append(line)

with open(ENV_PATH, "w") as f:
    f.writelines(new_lines)

print(".env updated.")
