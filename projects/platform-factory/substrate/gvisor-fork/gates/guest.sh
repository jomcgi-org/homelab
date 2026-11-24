# The guest of the isolation-gate template (gates.py puts it in the template's
# command: /bin/sh -c "<this file>"). It runs in a busybox image. The seed asks
# gVisor for a checkpoint and blocks; each fork resumes after the read, learns its
# mode from its actor name (<mode>-<n>), answers the wakeup probe, runs the mode
# and prints lines that start with "GATE <actor> ". Nothing here reads a secret.
if [ -z "$GATE_LOCAL" ]; then
	exec 3<>/proc/gvisor/checkpoint || exit 2
	echo 1 >&3
	read -r _ <&3
	N=$(tr '\0' '\n' </proc/gvisor/spec_environ | sed -n 's/^ATE_ACTOR_NAME=//p')
else
	N=$GATE_NAME
fi
M=${N%%-*}
say() { echo "GATE $N $*"; }
nc -lk -p 8080 -e sh -c 'printf "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"' &
P=$!
sleep 1
case $M in
ident)
	say boot_id=$(cat /proc/sys/kernel/random/boot_id) uuid=$(cat /proc/sys/kernel/random/uuid)
	say urandom=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n') hostname=$(hostname) uid=$(id -u) pid=$$
	say maps=$(sed -n '/\[stack\]/p;/\[heap\]/p;/\[vdso\]/p' /proc/$$/maps | cut -d' ' -f1 | tr '\n' ,)
	say auxv=$(od -An -tx8 -w16 /proc/$$/auxv | tr -s ' ' | tr '\n' ';')
	;;
canary)
	say canary=$GATE_CANARY
	say host_env_names=$(tr '\0' '\n' </proc/$$/environ | cut -d= -f1 | tr '\n' ' ')
	say host_paths=$(ls -d /var/lib/ate /var/run/secrets /run/podidentity.podcert.ate.dev /sys/fs/cgroup/forkd 2>&1 | tr '\n' ' ')
	say all_environ_hits=$(cat /proc/[0-9]*/environ 2>/dev/null | tr '\0' '\n' | grep -c -E 'KUBERNETES|POD_UID|WORKER_POOL|NODE_NAME|OTEL_|SSL_CERT')
	;;
net)
	say addr="$(ip -o addr 2>&1 | tr '\n' ';')" route="$(ip route 2>&1 | tr '\n' ';')"
	say resolv="$(cat /etc/resolv.conf 2>&1 | tr '\n' ';')"
	for c in "nslookup example.com" "nslookup example.com 1.1.1.1" "wget -T 3 -q -O /dev/null http://1.1.1.1/" "wget -T 3 -q -O /dev/null http://example.com/" "nc -w 3 1.1.1.1 443 -e true"; do
		r=$(timeout 8 $c 2>&1 </dev/null)
		say net rc=$? cmd="$c" out="$(echo "$r" | head -2 | tr '\n' ' ')"
	done
	;;
mem)
	say mem start
	x=$(head -c 200000000 /dev/zero | tr '\0' a)
	say mem survived len=${#x}
	;;
log)
	say log start
	head -c 3000000 /dev/zero | tr '\0' x | fold -w 200
	say log survived
	;;
pids)
	n=0
	while [ $n -lt 400 ]; do
		sleep 100 &
		n=$((n + 1))
	done
	say pids spawned=$n
	;;
hold)
	say hold start
	sleep 250
	;;
lifetime)
	say lifetime start
	sleep 3000
	;;
esac
say done
kill $P 2>/dev/null
