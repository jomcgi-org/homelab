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

# A hazard retained by another lens cannot satisfy a security UCA reference.
model='{
  "structure":{"control_actions":[{"key":"action"}]},
  "lenses":[
    {"lens":"logic","hazards":[{"key":"retired"}]},
    {"lens":"security","hazards":[{"key":"retained"}],"ucas":[{
      "key":"action.providing","control_action":"action","guideword":"providing",
      "status":"enforced-prod","hazards":["retained","retired"]
    }]}
  ]
}'
result=$("$jq_bin" -r "$validator" <<<"$model")
[[ $result == "VALIDATION_ERROR: security: action.providing references unknown hazard retired" ]]
pruned=$("$jq_bin" '.lenses[1].ucas[0].hazards -= ["retired"]' <<<"$model")
[[ $("$jq_bin" -r "$validator" <<<"$pruned") == "VALIDATION_OK" ]]

# Check the published models with the same jq programs used by Block A.
renderer=$(sed -n "/<< 'JQEOF'/,/^JQEOF$/{ /JQEOF/!p; }" "$guide")
for project in projects/embervm projects/monolith; do
	fragments=()
	for lens in logic security governance; do
		fragment="$project/stpa/$lens.json"
		if [[ $lens == security || -f $fragment ]]; then
			fragments+=("$fragment")
		fi
	done
	model=$("$jq_bin" -n --slurpfile structure "$project/stpa/structure.json" \
		'{structure: $structure[0], lenses: [inputs]}' "${fragments[@]}")
	result=$("$jq_bin" -r "$validator" <<<"$model")
	if [[ $result != VALIDATION_OK ]]; then
		echo "$project: $result" >&2
		exit 1
	fi
	"$jq_bin" -r "$renderer" <<<"$model" | diff -u "$project/STPA.md" -
done
