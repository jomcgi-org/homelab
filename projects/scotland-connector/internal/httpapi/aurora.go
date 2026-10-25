package httpapi

import (
	"net/http"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
)

func (h *Handler) aurora(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	var p connector.AuroraParams
	var err error
	q := r.URL.Query()
	if len(r.URL.RawQuery) > 1024 {
		err = &connector.ParamError{Param: "query", Message: "query too large"}
	}
	for k, v := range q {
		if (k != "lat" && k != "lon" && k != "at") || len(v) != 1 {
			err = &connector.ParamError{Param: k, Message: "unknown or repeated parameter"}
		}
	}
	p.Lat, err = floatParam(q.Get("lat"), "lat", err)
	p.Lon, err = floatParam(q.Get("lon"), "lon", err)
	p.At = q.Get("at")
	var env *connector.Envelope
	if err == nil {
		env, err = h.svc.Aurora(p)
	}
	noStore.set(w.Header())
	h.respond(w, r, "aurora", start, env, err)
}
