# Helm starts each rendered document with a separator and a source comment.
/^---$/ { next }
/^# Source: / {
    include_document = ($0 != "# Source: cloudnative-pg/templates/crds/crds.yaml")
    if (include_document) print "---"
}
include_document { print }
