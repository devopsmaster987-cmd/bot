import argparse, os, sys, time
import requests

def check(url):
    os.makedirs("reports", exist_ok=True)
    t0 = time.time()
    try:
        r = requests.get(url, timeout=20)
        ms = int((time.time() - t0) * 1000)
        line = f"{url} -> {r.status_code} in {ms}ms"
        print(line)
        with open("reports/http_check.txt", "a") as f:
            f.write(line + "\n")
        return 0 if r.status_code < 400 else 1
    except Exception as e:
        line = f"{url} -> FAILED ({e})"
        print(line)
        with open("reports/http_check.txt", "a") as f:
            f.write(line + "\n")
        return 1

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.abdulsamad.shop")
    a = ap.parse_args()
    sys.exit(check(a.url))