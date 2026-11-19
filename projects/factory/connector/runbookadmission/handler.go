// runbook-deadline is the read-only admission service for worker recovery.
// The API server calls it independently of the identity requesting deletion.
package runbookadmission

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	admission "k8s.io/api/admission/v1"
	v1 "k8s.io/api/core/v1"
	meta "k8s.io/apimachinery/pkg/apis/meta/v1"
	core "k8s.io/client-go/kubernetes/typed/core/v1"
)

type Handler struct{ Journals core.ConfigMapInterface }

func (d Handler) validate(ctx context.Context, r *admission.AdmissionRequest) error {
	var cm *v1.ConfigMap
	switch r.Operation {
	case admission.Update:
		var old, next v1.ConfigMap
		if json.Unmarshal(r.OldObject.Raw, &old) != nil || json.Unmarshal(r.Object.Raw, &next) != nil || next.Data["phase"] != "deleting" || old.Data["deadline"] != next.Data["deadline"] {
			return errors.New("invalid deletion intent")
		}
		cm = &old
	case admission.Delete:
		var pod v1.Pod
		var opts meta.DeleteOptions
		if json.Unmarshal(r.OldObject.Raw, &pod) != nil || json.Unmarshal(r.Options.Raw, &opts) != nil || opts.Preconditions == nil || opts.Preconditions.UID == nil || *opts.Preconditions.UID != pod.UID {
			return errors.New("UID precondition required")
		}
		owner := map[string]string{"factory-r-homelab": "factory-connector", "factory-r-homelab-review": "factory-reviewer"}[r.Namespace]
		if owner == "" {
			return errors.New("unknown worker namespace")
		}
		var err error
		cm, err = d.Journals.Get(ctx, "runbook-revert-"+owner, meta.GetOptions{})
		if err != nil {
			return errors.New("cannot verify authorization")
		}
		if cm.Data["phase"] != "deleting" || cm.Data["namespace"] != r.Namespace || cm.Data["pod"] != r.Name || cm.Data["uid"] != string(pod.UID) {
			return errors.New("worker authorization mismatch")
		}
	default:
		return errors.New("unexpected admission operation")
	}
	deadline, err := time.Parse(time.RFC3339Nano, cm.Data["deadline"])
	if err != nil || !time.Now().Before(deadline) {
		return errors.New("worker authorization expired")
	}
	return nil
}

func (d Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var review admission.AdmissionReview
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20)).Decode(&review) != nil || review.Request == nil {
		http.Error(w, "invalid AdmissionReview", 400)
		return
	}
	response := &admission.AdmissionResponse{UID: review.Request.UID, Allowed: true}
	if err := d.validate(r.Context(), review.Request); err != nil {
		response.Allowed = false
		response.Result = &meta.Status{Message: err.Error()}
	}
	review.Response = response
	review.Request = nil
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(review)
}
