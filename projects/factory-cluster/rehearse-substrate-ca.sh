#!/usr/bin/env bash
# Disposable actor-id pool. Keep all private material inside the temp directory.
set -euo pipefail
umask 077
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
for id in old new; do
	openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$id.key"
	openssl req -new -x509 -days 10 -key "$id.key" -subj "/CN=rehearsal-$id" \
		-addext basicConstraints=critical,CA:TRUE -out "$id.pem"
	openssl pkcs8 -topk8 -nocrypt -in "$id.key" -outform DER | openssl base64 -A >key.b64
	openssl x509 -in "$id.pem" -outform DER | openssl base64 -A >cert.b64
	jq -n --arg id "$id" --rawfile key key.b64 --rawfile cert cert.b64 \
		'{ID:$id, SigningKeyPKCS8:$key, RootCertificateDER:$cert}' >"$id.json"
done
publish() {
	jq -r '.CAs[].RootCertificateDER' pool | while read -r der; do
		openssl base64 -d -A <<<"$der" | openssl x509 -inform DER
	done >roots.pem
}
issue() {
	local selector='.ActiveForSigning as $id | .CAs[] | select(.ID==$id)'
	jq -r "$selector | .SigningKeyPKCS8" pool | openssl base64 -d -A | openssl pkey -inform DER >active.key
	jq -r "$selector | .RootCertificateDER" pool | openssl base64 -d -A | openssl x509 -inform DER >active.pem
	openssl x509 -req -in leaf.csr -CA active.pem -CAkey active.key -days "$2" -out "$1"
}
verify() {
	openssl verify -CAfile roots.pem -no-CApath -no-CAstore "$@"
}
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj /CN=fixture -keyout leaf.key -out leaf.csr
jq -s '{CAs:.,ActiveForSigning:"old"}' old.json >pool
publish
issue old-leaf.pem 1
verify old-leaf.pem
jq '.CAs += $new' --slurpfile new new.json pool >next
mv next pool
publish
verify old-leaf.pem
jq '.ActiveForSigning="new"' pool >next
mv next pool
issue new-leaf.pem 5
verify old-leaf.pem new-leaf.pem
# invariant: CLUS-6
# Advance verifier time past the old leaf's expiry, still inside the new leaf's lifetime.
later=$(($(date +%s) + 172800))
if verify -attime "$later" old-leaf.pem; then exit 1; fi
verify -attime "$later" new-leaf.pem
jq '.CAs |= map(select(.ID=="new"))' pool >next
mv next pool
publish
verify -attime "$later" new-leaf.pem
# Isolate loss of issuer trust from expiry by also checking at issuance time.
if verify old-leaf.pem; then exit 1; fi
