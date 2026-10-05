#!/bin/sh
# 取得網站憑證鏈中缺少的簽發者憑證（中繼 / 非 Mozilla 根），存成 PEM 供 sources.yaml 的 extra_ca_files 使用。
# 沿著憑證內的 AIA「CA Issuers」網址往上抓，直到自簽根憑證或最多 3 層。不會關閉任何 TLS 驗證。
# 用法：sh scripts/fetch_issuer_cert.sh [host] [輸出檔]
set -eu
HOST="${1:-law.tii.org.tw}"
OUT="${2:-config/certs/$HOST.pem}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$(dirname "$OUT")"

echo "== 伺服器送出的憑證鏈 =="
openssl s_client -connect "$HOST:443" -servername "$HOST" -showcerts </dev/null 2>/dev/null \
  | grep -E '^ *[0-9]+ s:|^ *i:' || true
openssl s_client -connect "$HOST:443" -servername "$HOST" </dev/null 2>/dev/null \
  | openssl x509 > "$TMP/cur.pem"

: > "$TMP/chain.pem"
for hop in 1 2 3; do
  SUBJ=$(openssl x509 -in "$TMP/cur.pem" -noout -subject)
  ISS=$(openssl x509 -in "$TMP/cur.pem" -noout -issuer)
  if [ "${SUBJ#subject=}" = "${ISS#issuer=}" ]; then echo "已到自簽根憑證，停止"; break; fi
  URL=$(openssl x509 -in "$TMP/cur.pem" -noout -text | awk '/CA Issuers - URI:/{sub(/.*URI:/,""); print; exit}')
  if [ -z "$URL" ]; then echo "此憑證沒有 AIA 網址，停止"; break; fi
  echo "第 $hop 層：下載 $URL"
  curl -fsSL "$URL" -o "$TMP/issuer.bin"
  if openssl x509 -inform DER -in "$TMP/issuer.bin" -out "$TMP/next.pem" 2>/dev/null \
     || openssl x509 -in "$TMP/issuer.bin" -out "$TMP/next.pem" 2>/dev/null; then :
  else  # .p7c (PKCS#7) 格式
    openssl pkcs7 -inform DER -in "$TMP/issuer.bin" -print_certs 2>/dev/null \
      | awk '/BEGIN CERT/{p=1} p{print} /END CERT/{exit}' > "$TMP/next.pem"
  fi
  openssl x509 -in "$TMP/next.pem" -noout -subject -issuer
  cat "$TMP/next.pem" >> "$TMP/chain.pem"
  cp "$TMP/next.pem" "$TMP/cur.pem"
done

if [ ! -s "$TMP/chain.pem" ]; then echo "沒有取得任何簽發者憑證" >&2; exit 1; fi
cp "$TMP/chain.pem" "$OUT"
echo "== 已寫入 $OUT =="
