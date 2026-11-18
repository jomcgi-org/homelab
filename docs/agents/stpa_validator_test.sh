#!/usr/bin/env bash
set -euo pipefail

jq_bin=$1
guide=$2
validator=$(sed -n "/<< 'VJQEOF'/,/^VJQEOF$/{ /VJQEOF/!p; }" "$guide")
for section in hazards ucas unsafe_feedback; do
	for status in none designed; do
		input=$("$jq_bin" -n --arg section "$section" --arg status "$status" '{
			structure: {control_actions: [{key:"action"}], nodes: [{key:"a"}]},
			lenses: [{lens:"security", ($section): [{
				key:"action.providing", status:$status, issue:"HOME-1",
				control_action:"action", guideword:"providing", from:"a", to:"a"
			}]}]
		}')
		result=$("$jq_bin" -r "$validator" <<<"$input")
		[[ $result == "VALIDATION_ERROR: security: action.providing requires an enforced or staged-off control" ]] || {
			echo "$section with $status returned $result" >&2
			exit 1
		}
	done
done
